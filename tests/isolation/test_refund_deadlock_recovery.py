"""Refund transaction retries against guarded MySQL, with injected driver failures."""
from contextlib import contextmanager
from decimal import Decimal
import uuid

import pytest
from pymysql.err import OperationalError


@pytest.fixture
def refund_case(db, users):
    order_no = 'DL' + uuid.uuid4().hex
    refund_no = 'RF' + uuid.uuid4().hex
    transaction = 'TX' + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET member_points=1 WHERE id=%s', (users[1],))
            cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,pending_points,status,transaction_id,refund_no) VALUES(%s,%s,%s,1,2,'refunding',%s,%s)",
                        (order_no, users[1], users[2], transaction, refund_no))
            order_id = cur.lastrowid
            cur.execute("INSERT INTO payment_refund_intents(order_number,logical_key,out_refund_no,transaction_id,total_fee,refund_fee,previous_status,state) VALUES(%s,%s,%s,%s,100,100,'pending_ship','change')",
                        (order_no, 'direct:' + order_no, refund_no, transaction))
            cur.execute("UPDATE finance_accounts SET balance=balance+1 WHERE account_type='maintain_pool'")
            cur.execute("INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark) SELECT 'maintain_pool',%s,1,balance,'income',%s FROM finance_accounts WHERE account_type='maintain_pool'", (users[1], '订单分账: ' + order_no))
            cur.execute("INSERT INTO points_log(user_id,change_amount,balance_after,type,reason,related_order,created_at) VALUES(%s,5,5,'member','购买普通商品获得积分',%s,NOW())", (users[1], order_id))
        conn.commit()
    return {'order': order_no, 'id': order_id, 'user': users[1], 'refund': refund_no,
            'result': {'out_refund_no': refund_no, 'transaction_id': transaction,
                       'refund_status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}}


@pytest.fixture
def referral_case(db, users, refund_case):
    """Two actual grants: one precisely recoverable, one requiring a manual case."""
    from services.finance_service import FinanceService

    with db() as conn:
        with conn.cursor() as cur:
            grants = []
            for recipient, amount, referral, available in (
                    (users[0], Decimal('0.123456'), Decimal('0.876545'), Decimal('1.876546')),
                    (users[3], Decimal('0.654321'), Decimal('0'), Decimal('4.345679'))):
                cur.execute('UPDATE users SET referral_points=%s,true_total_points=%s WHERE id=%s',
                            (referral, available, recipient))
                FinanceService(session=conn)._grant_referral_points(cur, recipient, amount, refund_case['order'])
                cur.execute('SELECT flow_id FROM referral_reward_grants WHERE order_id=%s AND recipient_id=%s',
                            (refund_case['id'], recipient))
                flow_id = cur.fetchone()['flow_id']
                grants.append((flow_id, recipient))
            # Simulate spending only part of the second grant after it was credited.
            cur.execute('UPDATE users SET referral_points=%s WHERE id=%s',
                        (Decimal('0.100000'), users[3]))
        conn.commit()
    refund_case['grants'] = grants
    return refund_case


def referral_state(conn, case):
    with conn.cursor() as cur:
        cur.execute("""SELECT g.flow_id,g.recipient_id,g.amount,g.state,g.recovered_refund_no,
                              u.referral_points,u.true_total_points
                       FROM referral_reward_grants g JOIN users u ON u.id=g.recipient_id
                       WHERE g.order_id=%s ORDER BY g.flow_id""", (case['id'],))
        grants = cur.fetchall()
        cur.execute("""SELECT r.flow_id,r.recipient_id,r.refund_no,r.change_amount,
                              r.referral_before,r.referral_after,r.available_before,r.available_after,
                              r.referral_flow_id,r.available_flow_id,
                              rf.account_type AS referral_account,rf.change_amount AS referral_delta,
                              af.account_type AS available_account,af.change_amount AS available_delta
                       FROM referral_reward_reversals r
                       LEFT JOIN account_flow rf ON rf.id=r.referral_flow_id
                       LEFT JOIN account_flow af ON af.id=r.available_flow_id
                       WHERE r.order_id=%s ORDER BY r.flow_id""", (case['id'],))
        reversals = cur.fetchall()
        cur.execute("""SELECT case_key,refund_no,reason,state FROM payment_reconciliation_cases
                       WHERE order_number=%s AND reason='referral_insufficient_balance'
                       ORDER BY case_key""", (case['order'],))
        cases = cur.fetchall()
        cur.execute("""SELECT id,related_user,account_type,change_amount FROM account_flow
                       WHERE remark IN (%s,%s) ORDER BY id""",
                    tuple(f"退款收回推荐奖励: {case['order']} 原流水#{flow_id}"
                          for flow_id, _ in case['grants']))
        reversal_flows = cur.fetchall()
    return {'grants': grants, 'reversals': reversals, 'cases': cases,
            'reversal_flows': reversal_flows}


def state(conn, case):
    with conn.cursor() as cur:
        cur.execute('SELECT status FROM orders WHERE id=%s', (case['id'],))
        order_status = cur.fetchone()['status']
        cur.execute('SELECT state FROM payment_refund_intents WHERE out_refund_no=%s', (case['refund'],))
        intent_state = cur.fetchone()['state']
        cur.execute('SELECT member_points FROM users WHERE id=%s', (case['user'],))
        points = cur.fetchone()['member_points']
        cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
        pool = cur.fetchone()['balance']
        cur.execute('SELECT event_kind FROM finance_events WHERE order_number=%s ORDER BY event_kind', (case['order'],))
        events = [row['event_kind'] for row in cur.fetchall()]
        cur.execute('SELECT reason FROM payment_reconciliation_cases WHERE order_number=%s ORDER BY case_key', (case['order'],))
        cases = [row['reason'] for row in cur.fetchall()]
        cur.execute('SELECT granted_amount,recovered_amount,shortfall_amount FROM order_member_reward_refunds WHERE order_id=%s', (case['id'],))
        recovery = cur.fetchone()
        cur.execute('SELECT COUNT(*) n FROM account_flow WHERE account_type=%s AND remark=%s', ('maintain_pool', '退款回冲: ' + case['order']))
        pool_flows = cur.fetchone()['n']
        cur.execute('SELECT COUNT(*) n FROM points_log WHERE related_order=%s AND reason IN (%s,%s)',
                    (case['id'], '订单退款退回积分: ' + case['order'], '订单退款收回奖励积分: ' + case['order']))
        point_flows = cur.fetchone()['n']
    result = {'order': order_status, 'intent': intent_state, 'points': points, 'pool': pool,
              'events': events, 'cases': cases, 'recovery': recovery,
              'pool_flows': pool_flows, 'point_flows': point_flows}
    if 'grants' in case:
        result['referral'] = referral_state(conn, case)
    return result


def install_fault(monkeypatch, db, case, code, failures):
    from core.database import get_conn
    from services import refund_service

    observed = {'in_transaction': [], 'before_retry': [], 'connections': 0}

    class Cursor:
        def __init__(self, cur, conn):
            self.cur, self.conn = cur, conn

        def __getattr__(self, name):
            return getattr(self.cur, name)

        def execute(self, sql, args=None):
            result = self.cur.execute(sql, args)
            if sql.startswith('UPDATE payment_refund_intents SET state=%s,provider_refund_id='):
                observed['in_transaction'].append(state(self.conn, case))
                if len(observed['in_transaction']) <= failures:
                    raise OperationalError(code, 'injected after refund writes')
            return result

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        @contextmanager
        def cursor(self):
            with self.conn.cursor() as cur:
                yield Cursor(cur, self.conn)

    @contextmanager
    def instrumented():
        observed['connections'] += 1
        if observed['in_transaction']:
            with db() as check:
                observed['before_retry'].append(state(check, case))
        with get_conn() as conn:
            yield Connection(conn)

    monkeypatch.setattr(refund_service, 'get_conn', instrumented)
    return observed


def assert_applied(current, baseline, referral_case=False):
    assert current['order'] == 'refunded'
    assert current['intent'] == 'success'
    assert current['points'] == 0
    assert current['pool'] == baseline['pool'] - 1
    assert current['events'] == ['discount_refund', 'referral_refund', 'refund_split']
    assert current['cases'] == (['member_reward_shortfall', 'referral_insufficient_balance']
                                if referral_case else ['member_reward_shortfall'])
    assert current['recovery'] == {'granted_amount': Decimal('5'), 'recovered_amount': Decimal('3'), 'shortfall_amount': Decimal('2')}
    assert current['pool_flows'] == 1
    assert current['point_flows'] == 2


def assert_referral_applied(snapshot, case):
    exact_id, exact_user = case['grants'][0]
    manual_id, manual_user = case['grants'][1]
    assert snapshot['grants'] == [
        {'flow_id': exact_id, 'recipient_id': exact_user, 'amount': Decimal('0.123456'),
         'state': 'recovered', 'recovered_refund_no': case['refund'],
         'referral_points': Decimal('0.876545'), 'true_total_points': Decimal('1.876546')},
        {'flow_id': manual_id, 'recipient_id': manual_user, 'amount': Decimal('0.654321'),
         'state': 'manual', 'recovered_refund_no': None,
         'referral_points': Decimal('0.100000'), 'true_total_points': Decimal('5.000000')},
    ]
    assert snapshot['cases'] == [
        {'case_key': f'referral-grant:{manual_id}', 'refund_no': case['refund'],
         'reason': 'referral_insufficient_balance', 'state': 'open'}]
    assert len(snapshot['reversals']) == 1
    reversal = snapshot['reversals'][0]
    assert {key: reversal[key] for key in (
        'flow_id', 'recipient_id', 'refund_no', 'change_amount', 'referral_before',
        'referral_after', 'available_before', 'available_after',
        'referral_account', 'referral_delta', 'available_account', 'available_delta'
    )} == {
        'flow_id': exact_id, 'recipient_id': exact_user, 'refund_no': case['refund'],
        'change_amount': Decimal('-0.123456'), 'referral_before': Decimal('1.000001'),
        'referral_after': Decimal('0.876545'), 'available_before': Decimal('2.000002'),
        'available_after': Decimal('1.876546'), 'referral_account': 'referral_points',
        'referral_delta': Decimal('-0.1235'), 'available_account': 'true_total_points',
        'available_delta': Decimal('-0.1235')}
    assert snapshot['reversal_flows'] == [
        {'id': reversal['referral_flow_id'], 'related_user': exact_user,
         'account_type': 'referral_points', 'change_amount': Decimal('-0.1235')},
        {'id': reversal['available_flow_id'], 'related_user': exact_user,
         'account_type': 'true_total_points', 'change_amount': Decimal('-0.1235')}]


@pytest.mark.parametrize('exhausted', [False, True])
def test_referral_grants_retry_atomicity(db, referral_case, monkeypatch, exhausted):
    from services import refund_service

    with db() as conn:
        baseline = state(conn, referral_case)
    before = baseline['referral']
    assert [grant['state'] for grant in before['grants']] == ['granted', 'granted']
    assert not before['reversals'] and not before['reversal_flows'] and not before['cases']
    observed = install_fault(monkeypatch, db, referral_case, 1213, 3 if exhausted else 1)
    if exhausted:
        with pytest.raises(OperationalError) as error:
            refund_service.RefundService.apply_result(referral_case['result'])
        assert error.value.args[0] == 1213
    else:
        refund_service.RefundService.apply_result(referral_case['result'])

    attempts = 3 if exhausted else 2
    assert observed['connections'] == attempts
    assert len(observed['in_transaction']) == attempts
    assert observed['before_retry'] == [baseline] * (attempts - 1)
    for written in observed['in_transaction']:
        assert_applied(written, baseline, referral_case=True)
        assert_referral_applied(written['referral'], referral_case)
    with db() as conn:
        final = state(conn, referral_case)
    if exhausted:
        assert final == baseline
    else:
        assert_applied(final, baseline, referral_case=True)
        assert_referral_applied(final['referral'], referral_case)
        refund_service.RefundService.apply_result(referral_case['result'])
        with db() as conn:
            assert state(conn, referral_case) == final


def test_change_query_once_then_rollback_and_retry(db, refund_case, monkeypatch):
    from services import refund_service
    with db() as conn:
        baseline = state(conn, refund_case)
    calls = []

    class Provider:
        def query_refund(self, number, **kwargs):
            calls.append(('query', number))
            return refund_case['result']

        def refund(self, **kwargs):
            pytest.fail('change recovery must not issue a provider refund')

    monkeypatch.setattr(refund_service, 'provider', Provider())
    observed = install_fault(monkeypatch, db, refund_case, 1213, 1)
    response = refund_service.RefundService.recover(refund_case['refund'])
    assert response['status'] == 'success'
    assert calls == [('query', refund_case['refund'])]
    assert observed['connections'] == 3  # lease, failed transaction, new transaction
    assert len(observed['in_transaction']) == 2
    assert_applied(observed['in_transaction'][0], baseline)
    assert observed['before_retry'] == [baseline]
    with db() as conn:
        assert_applied(state(conn, refund_case), baseline)
    # Replaying the same trusted result is idempotent, without another provider call.
    refund_service.RefundService.apply_result(refund_case['result'])
    with db() as conn:
        assert_applied(state(conn, refund_case), baseline)
    assert calls == [('query', refund_case['refund'])]


@pytest.mark.parametrize(('code', 'failures', 'attempts'), [(1205, 1, 1), (1213, 3, 3), (2013, 1, 1)])
def test_non_deadlock_and_exhaustion_propagate_and_rollback(db, refund_case, monkeypatch, code, failures, attempts):
    from services import refund_service
    with db() as conn:
        baseline = state(conn, refund_case)
    observed = install_fault(monkeypatch, db, refund_case, code, failures)
    with pytest.raises(OperationalError) as error:
        refund_service.RefundService.apply_result(refund_case['result'])
    assert error.value.args[0] == code
    assert observed['connections'] == attempts
    assert len(observed['in_transaction']) == attempts
    assert all(item == baseline for item in observed['before_retry'])
    with db() as conn:
        assert state(conn, refund_case) == baseline
    # The original change identity is still available for an explicitly requested recovery.
    assert baseline['intent'] == 'change'


def test_exhausted_change_can_be_manually_recovered_by_same_number(db, refund_case, monkeypatch):
    from core.database import get_conn
    from services import refund_service

    with db() as conn:
        baseline = state(conn, refund_case)
    calls = []

    class Provider:
        def query_refund(self, number, **kwargs):
            calls.append(number)
            return refund_case['result']

        def refund(self, **kwargs):
            pytest.fail('change recovery must not issue a provider refund')

    monkeypatch.setattr(refund_service, 'provider', Provider())
    observed = install_fault(monkeypatch, db, refund_case, 1213, 3)
    with pytest.raises(OperationalError) as error:
        refund_service.RefundService.recover(refund_case['refund'])
    assert error.value.args[0] == 1213
    assert len(observed['in_transaction']) == 3
    assert calls == [refund_case['refund']]
    with db() as conn:
        assert state(conn, refund_case) == baseline
        with conn.cursor() as cur:
            cur.execute('UPDATE payment_refund_intents SET next_attempt_at=NOW() WHERE out_refund_no=%s',
                        (refund_case['refund'],))
        conn.commit()
    monkeypatch.setattr(refund_service, 'get_conn', get_conn)
    assert refund_service.RefundService.recover(refund_case['refund'])['status'] == 'success'
    assert calls == [refund_case['refund'], refund_case['refund']]
    with db() as conn:
        assert_applied(state(conn, refund_case), baseline)



@pytest.mark.parametrize('code', [1213, 2013])
def test_unknown_commit_outcome_is_never_retried(db, refund_case, monkeypatch, code):
    from core.database import get_conn
    from services import refund_service
    with db() as conn:
        baseline = state(conn, refund_case)
    connections = []

    class LostCommit:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            raise OperationalError(code, 'injected lost commit response')

    @contextmanager
    def failing():
        connections.append(1)
        with get_conn() as conn:
            yield LostCommit(conn)

    monkeypatch.setattr(refund_service, 'get_conn', failing)
    with pytest.raises(OperationalError) as error:
        refund_service.RefundService.apply_result(refund_case['result'])
    assert error.value.args[0] == code
    assert len(connections) == 1
    with db() as conn:
        assert_applied(state(conn, refund_case), baseline)

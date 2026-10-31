"""Referral refunds through real application transactions and a controlled provider."""
from decimal import Decimal

from test_payment_business import paid_order


def _seed_grants(db, users, number):
    from services.finance_service import FinanceService

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM orders WHERE order_number=%s', (number,))
            order_id = cur.fetchone()['id']
            cur.execute('UPDATE users SET referral_points=0,true_total_points=0 WHERE id IN (%s,%s,%s)',
                        (users[0], users[2], users[3]))
            finance = FinanceService()
            finance._grant_referral_points(cur, users[0], Decimal('0.123456'), number,
                                          remark=f'推荐奖励 - 订单#{order_id}')
            finance._grant_referral_points(cur, users[3], Decimal('0.654321'), number)
            cur.execute('SELECT flow_id,recipient_id,amount FROM referral_reward_grants WHERE order_id=%s ORDER BY flow_id',
                        (order_id,))
            grants = cur.fetchall()
            cur.execute('SELECT change_amount FROM account_flow WHERE id IN (%s,%s) ORDER BY id',
                        (grants[0]['flow_id'], grants[1]['flow_id']))
            assert [r['change_amount'] for r in cur.fetchall()] == [Decimal('0.1235'), Decimal('0.6543')]
            # The currently configured referrer is not either original recipient.
            cur.execute('INSERT INTO user_referrals(user_id,referrer_id) VALUES(%s,%s)', (users[1], users[2]))
        conn.commit()
    return order_id, grants


def _refund(http, token, merchant, number, fee, key):
    return http.post('/wechat-pay/refund',
                     json={'order_number': number, 'refund_fee': fee, 'refund_request_id': key},
                     headers={'Authorization': 'Bearer ' + token(merchant)})


def test_full_refund_recovers_original_six_decimal_grants_once_after_partial(
        db, http, users, token, paid_order, monkeypatch):
    from services import refund_service
    order_id, grants = _seed_grants(db, users, paid_order)
    assert [(r['recipient_id'], r['amount']) for r in grants] == [
        (users[0], Decimal('0.123456')), (users[3], Decimal('0.654321'))]
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'SUCCESS', 'amount': {'refund': kwargs['refund_fee'],
                    'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    first = _refund(http, token, users[2], paid_order, 40, 'first')
    assert first.status_code == 200
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,referral_points,true_total_points FROM users WHERE id IN (%s,%s,%s)',
                        (users[0], users[2], users[3]))
            balances = {r['id']: (r['referral_points'], r['true_total_points']) for r in cur.fetchall()}
            assert balances == {users[0]: (Decimal('0.123456'), Decimal('0.123456')),
                                users[3]: (Decimal('0.654321'), Decimal('0.654321')),
                                users[2]: (Decimal('0'), Decimal('0'))}
            cur.execute('SELECT state FROM referral_reward_grants WHERE order_id=%s ORDER BY flow_id', (order_id,))
            assert [r['state'] for r in cur.fetchall()] == ['granted', 'granted']
    final = _refund(http, token, users[2], paid_order, 60, 'second')
    assert final.status_code == 200
    replay = _refund(http, token, users[2], paid_order, 60, 'second')
    assert replay.status_code == 200
    assert replay.json()['refund_no'] == final.json()['refund_no']
    assert calls == [first.json()['refund_no'], final.json()['refund_no']]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,referral_points,true_total_points FROM users WHERE id IN (%s,%s,%s)',
                        (users[0], users[2], users[3]))
            assert all(r['referral_points'] == 0 and r['true_total_points'] == 0 for r in cur.fetchall())
            cur.execute('SELECT state,recovered_refund_no FROM referral_reward_grants WHERE order_id=%s ORDER BY flow_id', (order_id,))
            assert cur.fetchall() == [{'state': 'recovered', 'recovered_refund_no': final.json()['refund_no']}] * 2
            cur.execute('SELECT flow_id,order_id,recipient_id,refund_no,change_amount,referral_before,referral_after,available_before,available_after FROM referral_reward_reversals WHERE order_id=%s ORDER BY flow_id', (order_id,))
            reversals = cur.fetchall()
            assert reversals == [
                {'flow_id': grant['flow_id'], 'order_id': order_id, 'recipient_id': grant['recipient_id'],
                 'refund_no': final.json()['refund_no'], 'change_amount': -grant['amount'],
                 'referral_before': grant['amount'], 'referral_after': Decimal('0'),
                 'available_before': grant['amount'], 'available_after': Decimal('0')}
                for grant in grants
            ]
            cur.execute("SELECT COUNT(*) n FROM finance_events WHERE event_kind='referral_refund' AND event_key=%s", (paid_order,))
            assert cur.fetchone()['n'] == 1
            cur.execute('SELECT COUNT(*) n FROM payment_reconciliation_cases WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['n'] == 0


def test_full_refund_quarantines_each_unrecoverable_grant_or_legacy_flow(
        db, http, users, token, paid_order, monkeypatch):
    from services import refund_service
    order_id, grants = _seed_grants(db, users, paid_order)
    legacy_ids = []
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET referral_points=0.010000,true_total_points=0.010000 WHERE id IN (%s,%s)',
                        (users[0], users[3]))
            for recipient, remark in ((users[0], f'推荐奖励 - 订单#{order_id}'),
                                      (users[3], f'普通商品推荐奖励 - 订单{paid_order}'),
                                      (users[0], f'推荐奖励自动发放（订单#{order_id}）')):
                cur.execute("INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark) VALUES('referral_points',%s,0.1235,0.1235,'income',%s)",
                            (recipient, remark))
                legacy_ids.append(cur.lastrowid)
        conn.commit()
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    result = _refund(http, token, users[2], paid_order, 100, 'full')
    assert result.status_code == 200
    replay = _refund(http, token, users[2], paid_order, 100, 'full')
    assert replay.status_code == 200
    assert replay.json()['refund_no'] == result.json()['refund_no']
    assert calls == [result.json()['refund_no']]
    expected = {('referral-grant:' + str(g['flow_id']), 'referral_insufficient_balance') for g in grants}
    expected |= {('referral-legacy:' + str(flow_id), 'referral_legacy_precision') for flow_id in legacy_ids}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT state FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['state'] == 'success'
            cur.execute('SELECT referral_points,true_total_points FROM users WHERE id IN (%s,%s)', (users[0], users[3]))
            assert all(r['referral_points'] == r['true_total_points'] == Decimal('0.010000') for r in cur.fetchall())
            cur.execute('SELECT case_key,reason FROM payment_reconciliation_cases WHERE order_number=%s', (paid_order,))
            assert {(r['case_key'], r['reason']) for r in cur.fetchall()} == expected
            cur.execute("SELECT COUNT(*) n FROM finance_events WHERE event_kind='referral_refund' AND event_key=%s", (paid_order,))
            assert cur.fetchone()['n'] == 1


def test_mismatched_four_decimal_flow_requires_reconciliation(
        db, http, users, token, paid_order, monkeypatch):
    from services import refund_service
    order_id, grants = _seed_grants(db, users, paid_order)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE account_flow SET change_amount=0.9999 WHERE id=%s', (grants[0]['flow_id'],))
        conn.commit()
    class Provider:
        def refund(self, **kwargs):
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    result = _refund(http, token, users[2], paid_order, 100, 'mismatch')
    assert result.status_code == 200
    replay = _refund(http, token, users[2], paid_order, 100, 'mismatch')
    assert replay.status_code == 200
    assert replay.json()['refund_no'] == result.json()['refund_no']
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT state FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['state'] == 'success'
            cur.execute('SELECT referral_points,true_total_points FROM users WHERE id=%s', (users[0],))
            assert cur.fetchone() == {'referral_points': Decimal('0.123456'),
                                      'true_total_points': Decimal('0.123456')}
            cur.execute('SELECT state FROM referral_reward_grants WHERE flow_id=%s', (grants[0]['flow_id'],))
            assert cur.fetchone()['state'] == 'manual'
            cur.execute('SELECT case_key,reason FROM payment_reconciliation_cases WHERE order_number=%s', (paid_order,))
            assert cur.fetchall() == [{'case_key': 'referral-grant:' + str(grants[0]['flow_id']),
                                       'reason': 'referral_record_mismatch'}]
            cur.execute('SELECT flow_id FROM referral_reward_reversals WHERE order_id=%s', (order_id,))
            assert cur.fetchall() == [{'flow_id': grants[1]['flow_id']}]


def test_referral_reversal_reports_preserve_precision_pagination_and_scope(
        db, http, users, token, paid_order, monkeypatch):
    from core.config import settings
    from services import refund_service
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[2]))
    order_id, grants = _seed_grants(db, users, paid_order)
    recipient = users[0]
    with db() as conn:
        with conn.cursor() as cur:
            # The residual balance is below the old account_flow four-place precision.
            cur.execute('UPDATE users SET referral_points=referral_points+0.000001, true_total_points=true_total_points+0.000001 WHERE id=%s',
                        (recipient,))
            cur.execute("""INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark)
                           VALUES('true_total_points',%s,-0.2500,2.1234,'expense','synthetic historical expense')""",
                        (recipient,))
            legacy_flow_id = cur.lastrowid
        conn.commit()
    class Provider:
        def refund(self, **kwargs):
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    refund = _refund(http, token, users[2], paid_order, 100, 'report')
    assert refund.status_code == 200
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT referral_flow_id,available_flow_id FROM referral_reward_reversals WHERE flow_id=%s AND order_id=%s',
                        (grants[0]['flow_id'], order_id))
            links = cur.fetchone()
            assert links['referral_flow_id'] != links['available_flow_id']
            cur.execute('SELECT id,account_type,flow_type FROM account_flow WHERE id IN (%s,%s)',
                        (links['referral_flow_id'], links['available_flow_id']))
            assert {(row['id'], row['account_type'], row['flow_type']) for row in cur.fetchall()} == {
                (links['referral_flow_id'], 'referral_points', 'expense'),
                (links['available_flow_id'], 'true_total_points', 'expense')}
    path = '/api/reports/points/all-flows'
    headers = {'Authorization': 'Bearer ' + token(recipient)}
    assert http.get(path).status_code == 401
    assert http.get(path, params={'user_id': recipient},
                    headers={'Authorization': 'Bearer ' + token(users[1])}).status_code == 403
    response = http.get(path, params={'user_id': recipient, 'page_size': 100}, headers=headers)
    assert response.status_code == 200
    report = response.json()['data']
    records = {row['flow_id']: row for row in report['records']}
    for flow_id in links.values():
        row = records['af_' + str(flow_id)]
        assert Decimal(str(row['change_amount'])) == Decimal('-0.123456')
        assert Decimal(str(row['balance_after'])) == Decimal('0.000001')
        assert row['flow_category'] == '支出'
    legacy = records['af_' + str(legacy_flow_id)]
    assert Decimal(str(legacy['change_amount'])) == Decimal('-0.2500')
    assert Decimal(str(legacy['balance_after'])) == Decimal('2.1234')
    assert Decimal(str(report['summary']['total_expense'])) == Decimal('-0.496912')
    paged = []
    for page in range(1, report['pagination']['total'] + 1):
        result = http.get(path, params={'user_id': recipient, 'page': page, 'page_size': 1}, headers=headers)
        assert result.status_code == 200
        page_data = result.json()['data']
        assert page_data['summary'] == report['summary']
        paged.extend(row['flow_id'] for row in page_data['records'])
    assert len(paged) == len(set(paged)) == report['pagination']['total']
    assert set(paged) == set(records)
    overview = http.get('/api/reports/points/all', params={'user_id': recipient}, headers=headers)
    assert overview.status_code == 200
    user_report = overview.json()['data']['users']
    assert len(user_report) == 1 and user_report[0]['user_id'] == recipient
    deducted = user_report[0]['points_summary']['true_total_points']['total_deducted']
    assert Decimal(str(deducted)) == Decimal('0.373456')


def test_referral_grant_precision_across_personal_reports(
        db, http, users, token, paid_order, monkeypatch):
    from core.config import settings
    from services import refund_service
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[2]))
    _, grants = _seed_grants(db, users, paid_order)
    recipient = users[0]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark,created_at)
                           VALUES('referral_points',%s,0.2500,0.2500,'income','synthetic prior income',DATE_SUB(NOW(),INTERVAL 1 DAY))""",
                        (recipient,))
            legacy_id = cur.lastrowid
        conn.commit()
    class Provider:
        def refund(self, **kwargs):
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    assert _refund(http, token, users[2], paid_order, 100, 'precision').status_code == 200
    headers = {'Authorization': 'Bearer ' + token(recipient)}
    query = {'user_id': recipient}
    flows_path = '/api/reports/points/all-flows'
    flows = http.get(flows_path, params=query, headers=headers)
    assert flows.status_code == 200
    data = flows.json()['data']
    records = {row['flow_id']: row for row in data['records']}
    assert Decimal(str(records['af_' + str(grants[0]['flow_id'])]['change_amount'])) == Decimal('0.123456')
    assert Decimal(str(records['af_' + str(legacy_id)]['change_amount'])) == Decimal('0.2500')
    assert Decimal(str(data['summary']['total_income'])) == Decimal('0.373456')
    assert Decimal(str(data['summary']['total_expense'])) == Decimal('-0.246912')
    assert Decimal(str(data['summary']['net_flow'])) == Decimal('0.126544')
    for path, key in (('/api/rewards/referral', 'reward_id'), ('/api/rewards/flow', 'flow_id')):
        assert http.get(path).status_code == 401
        assert http.get(path, params=query, headers={'Authorization': 'Bearer ' + token(users[1])}).status_code == 403
        full = http.get(path, params={**query, 'page_size': 100}, headers=headers)
        assert full.status_code == 200
        result = full.json()['data']
        issued = {row[key]: Decimal(str(row['points_issued'])) for row in result['records']}
        assert issued == {grants[0]['flow_id']: Decimal('0.123456'), legacy_id: Decimal('0.2500')}
        if path.endswith('/flow'):
            assert Decimal(str(result['summary']['total_referral_points'])) == Decimal('0.373456')
        page_ids = []
        for page in (1, 2):
            response = http.get(path, params={**query, 'page': page, 'page_size': 1}, headers=headers)
            assert response.status_code == 200
            page_ids.extend(row[key] for row in response.json()['data']['records'])
        assert len(page_ids) == len(set(page_ids)) == 2
        assert set(page_ids) == set(issued)
    overview = http.get('/api/reports/points/all', params=query, headers=headers)
    assert overview.status_code == 200
    user = overview.json()['data']['users'][0]
    assert Decimal(str(user['points_summary']['referral_points']['total_earned'])) == Decimal('0.373456')
    combined = http.get('/api/reports/points/referral-team', params=query, headers=headers)
    assert combined.status_code == 200
    user = combined.json()['data']['users'][0]
    assert Decimal(str(user['referral_points']['total_earned'])) == Decimal('0.373456')

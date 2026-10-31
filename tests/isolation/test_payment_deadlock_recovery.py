"""Real isolated MySQL transactions with injected statement errors; no provider I/O."""
import asyncio
import uuid
from contextlib import contextmanager
from decimal import Decimal

import pymysql
import pytest

from test_payment_business import signed_callback


def seed_payment(db, users, kind):
    number = ('OFF' if kind == 'offline' else 'DEADLOCK') + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET member_points=10 WHERE id=%s', (users[1],))
            if kind == 'online':
                cur.execute("INSERT INTO products(name,user_id,status) VALUES('Synthetic recovery product',%s,1)", (users[2],))
                product = cur.lastrowid
                cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,original_amount,pending_points,status) VALUES(%s,%s,%s,10,12,2,'pending_pay')", (number, users[1], users[2]))
                order_id = cur.lastrowid
                cur.execute('INSERT INTO order_items(order_id,product_id,quantity,unit_price,total_price) VALUES(%s,%s,1,12,12)', (order_id, product))
                coupon = None
            else:
                cur.execute("INSERT INTO coupons(user_id,coupon_type,amount,status,valid_from,valid_to) VALUES(%s,'user',1,'unused',CURDATE(),DATE_ADD(CURDATE(),INTERVAL 7 DAY))", (users[1],))
                coupon = cur.lastrowid
                cur.execute("INSERT INTO offline_order(order_no,merchant_id,user_id,store_name,amount,paid_amount,coupon_discount,coupon_ids,status) VALUES(%s,%s,%s,'Synthetic',1100,1000,100,%s,1)", (number, users[2], users[1], '[' + str(coupon) + ']'))
                order_id = None
        conn.commit()
    data = {'out_trade_no': number, 'transaction_id': 'TX' + uuid.uuid4().hex,
            'trade_state': 'SUCCESS', 'amount': {'total': 1000, 'currency': 'CNY'}}
    return data, order_id, coupon


def snapshot(db, users, kind, data, order_id, coupon):
    number = data['out_trade_no']
    with db() as conn:
        with conn.cursor() as cur:
            if kind == 'online':
                cur.execute('SELECT status,transaction_id FROM orders WHERE id=%s', (order_id,))
            else:
                cur.execute('SELECT status,transaction_id FROM offline_order WHERE order_no=%s', (number,))
            order = cur.fetchone()
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            points = cur.fetchone()['member_points']
            cur.execute('SELECT COUNT(*) n FROM payment_events WHERE order_number=%s', (number,))
            payments = cur.fetchone()['n']
            cur.execute('SELECT COUNT(*) n FROM finance_events WHERE order_number=%s', (number,))
            finance_events = cur.fetchone()['n']
            cur.execute('SELECT COUNT(*) n FROM account_flow WHERE remark LIKE %s', ('%' + number + '%',))
            flows = cur.fetchone()['n']
            cur.execute('SELECT COUNT(*) n FROM payment_merchant_transfers WHERE order_number=%s', (number,))
            transfers = cur.fetchone()['n']
            cur.execute('SELECT COUNT(*) n FROM orders WHERE order_number=%s', (number,))
            orders = cur.fetchone()['n']
            if kind == 'online':
                cur.execute('SELECT COUNT(*) n FROM points_log WHERE related_order=%s', (order_id,))
            else:
                cur.execute('SELECT COUNT(*) n FROM points_log WHERE user_id=%s AND reason=%s', (users[1], '线下订单支付获得积分: ' + number))
            logs = cur.fetchone()['n']
            coupon_status = None
            if coupon:
                cur.execute('SELECT status FROM coupons WHERE id=%s', (coupon,))
                coupon_status = cur.fetchone()['status']
    return {'order': order, 'points': points, 'payments': payments, 'finance_events': finance_events,
            'flows': flows, 'transfers': transfers, 'orders': orders, 'logs': logs, 'coupon': coupon_status}


def inject_after_settlement(monkeypatch, kind, error, failures, before_retry):
    from services.finance_service import FinanceService
    from services.offline_service import OfflineService
    calls = []
    if kind == 'online':
        original = FinanceService.settle_order

        def wrapped(self, *args, **kwargs):
            calls.append((kwargs['order_no'],))
            if len(calls) > 1:
                before_retry()
            result = original(self, *args, **kwargs)
            if len(calls) <= failures:
                with kwargs['external_conn'].cursor() as cur:
                    cur.execute('SELECT member_points FROM users WHERE id=%s', (kwargs['user_id'],))
                    assert cur.fetchone()['member_points'] != 10
                raise error
            return result
        monkeypatch.setattr(FinanceService, 'settle_order', wrapped)
    else:
        original = OfflineService.on_paid

        async def wrapped(*args, **kwargs):
            calls.append((args[0], args[3]))
            if len(calls) > 1:
                before_retry()
            result = await original(*args, **kwargs)
            if len(calls) <= failures:
                with kwargs['external_conn'].cursor() as cur:
                    cur.execute('SELECT status FROM offline_order WHERE order_no=%s', (args[0],))
                    assert cur.fetchone()['status'] == 2
                raise error
            return result
        monkeypatch.setattr(OfflineService, 'on_paid', wrapped)
    return calls


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_callback_deadlock_replays_rolled_back_transaction_once(db, http, users, monkeypatch, signed_callback, kind):
    data, order_id, coupon = seed_payment(db, users, kind)
    initial = snapshot(db, users, kind, data, order_id, coupon)
    checked = []

    def before_retry():
        assert snapshot(db, users, kind, data, order_id, coupon) == initial
        checked.append(True)

    calls = inject_after_settlement(monkeypatch, kind, pymysql.err.OperationalError(1213, 'injected deadlock'), 1, before_retry)
    body, headers = signed_callback(data, 'TRANSACTION.SUCCESS')
    path = '/api/offline/zhifu/notify' if kind == 'offline' else '/api/wechat-pay/notify'
    assert http.post(path, content=body, headers=headers).status_code == 204
    assert checked == [True]
    assert len(calls) == 2 and calls[0] == calls[1]
    # Durable event makes a duplicate notification a no-op.
    assert http.post(path, content=body, headers=headers).status_code == 204
    assert len(calls) == 2
    result = snapshot(db, users, kind, data, order_id, coupon)
    assert result['points'] == (Decimal('18') if kind == 'online' else Decimal('20'))
    assert result['payments'] == 1
    assert result['orders'] == 1
    assert result['order']['transaction_id'] == data['transaction_id']
    if kind == 'online':
        assert result['finance_events'] == 1
        assert result['order']['status'] == 'pending_ship'
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n,SUM(change_amount) delta FROM points_log WHERE related_order=%s AND reason='积分抵扣支付'", (order_id,))
                assert cur.fetchone() == {'n': 1, 'delta': Decimal('-2')}
    else:
        assert result['coupon'] == 'used'
        assert result['order']['status'] == 2
        assert result['logs'] == 1
        assert result['transfers'] == 1


@pytest.mark.parametrize('kind', ['online', 'offline'])
@pytest.mark.parametrize('code,failures,expected_calls', [(1213, 3, 3), (1205, 3, 1), (2006, 3, 1)])
def test_exhaustion_and_other_db_errors_propagate_without_partial_commit(db, users, monkeypatch, kind, code, failures, expected_calls):
    from api.wechat_pay.routes import handle_transaction_success
    data, order_id, coupon = seed_payment(db, users, kind)
    initial = snapshot(db, users, kind, data, order_id, coupon)
    error = pymysql.err.OperationalError(code, 'injected database error')
    calls = inject_after_settlement(monkeypatch, kind, error, failures,
        lambda: assert_unchanged(db, users, kind, data, order_id, coupon, initial))
    with pytest.raises(pymysql.err.OperationalError) as raised:
        asyncio.run(handle_transaction_success(data))
    assert raised.value is error
    assert len(calls) == expected_calls
    assert snapshot(db, users, kind, data, order_id, coupon) == initial


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_exhausted_deadlock_callback_is_not_acknowledged(db, http, users, monkeypatch, signed_callback, kind):
    data, order_id, coupon = seed_payment(db, users, kind)
    initial = snapshot(db, users, kind, data, order_id, coupon)
    calls = inject_after_settlement(monkeypatch, kind,
        pymysql.err.OperationalError(1213, 'injected deadlock'), 3,
        lambda: assert_unchanged(db, users, kind, data, order_id, coupon, initial))
    body, headers = signed_callback(data, 'TRANSACTION.SUCCESS')
    path = '/api/offline/zhifu/notify' if kind == 'offline' else '/api/wechat-pay/notify'
    assert http.post(path, content=body, headers=headers).status_code == 503
    assert len(calls) == 3
    assert snapshot(db, users, kind, data, order_id, coupon) == initial


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_non_operational_error_with_deadlock_number_is_not_retried(db, users, monkeypatch, kind):
    from api.wechat_pay.routes import handle_transaction_success
    data, order_id, coupon = seed_payment(db, users, kind)
    initial = snapshot(db, users, kind, data, order_id, coupon)
    error = pymysql.err.IntegrityError(1213, 'not an OperationalError')
    calls = inject_after_settlement(monkeypatch, kind, error, 3, lambda: pytest.fail('unexpected retry'))
    with pytest.raises(pymysql.err.IntegrityError) as raised:
        asyncio.run(handle_transaction_success(data))
    assert raised.value is error
    assert len(calls) == 1
    assert snapshot(db, users, kind, data, order_id, coupon) == initial


def assert_unchanged(db, users, kind, data, order_id, coupon, initial):
    assert snapshot(db, users, kind, data, order_id, coupon) == initial


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_commit_error_even_1213_is_not_replayed(db, users, monkeypatch, kind):
    from api.wechat_pay import routes
    from core.database import get_conn
    data, order_id, coupon = seed_payment(db, users, kind)
    initial = snapshot(db, users, kind, data, order_id, coupon)
    error = pymysql.err.OperationalError(1213, 'injected uncertain commit')
    calls = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def commit(self):
            calls.append(True)
            raise error

    @contextmanager
    def failed_commit():
        with get_conn() as conn:
            yield Connection(conn)

    monkeypatch.setattr(routes, 'get_conn', failed_commit)
    with pytest.raises(pymysql.err.OperationalError) as raised:
        asyncio.run(routes.handle_transaction_success(data))
    assert raised.value is error
    assert calls == [True]
    assert snapshot(db, users, kind, data, order_id, coupon) == initial


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_business_rejection_is_not_retried(db, users, monkeypatch, kind):
    from api.wechat_pay import routes
    from core.database import get_conn
    data, order_id, coupon = seed_payment(db, users, kind)
    initial = snapshot(db, users, kind, data, order_id, coupon)
    data['amount']['total'] = 999
    calls = []

    @contextmanager
    def counted_connection():
        calls.append(True)
        with get_conn() as conn:
            yield conn

    monkeypatch.setattr(routes, 'get_conn', counted_connection)
    with pytest.raises(ValueError, match='amount mismatch'):
        asyncio.run(routes.handle_transaction_success(data))
    assert calls == [True]
    assert snapshot(db, users, kind, data, order_id, coupon) == initial

"""Signed payment settlement with real MySQL and corrupt/unavailable finance config."""
import json
import uuid
from contextlib import contextmanager

import pymysql
import pytest
from test_payment_business import signed_callback


@pytest.fixture
def finance_config(db):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,config_params FROM finance_accounts')
            saved = cur.fetchall()
            cur.execute('UPDATE finance_accounts SET config_params=NULL')
        conn.commit()
    yield
    with db() as conn:
        with conn.cursor() as cur:
            for row in saved:
                value = row['config_params']
                if value is not None and not isinstance(value, str):
                    value = json.dumps(value)
                cur.execute('UPDATE finance_accounts SET config_params=%s WHERE id=%s', (value, row['id']))
        conn.commit()


def _seed_payment(db, http, users, token, kind):
    if kind == 'offline':
        number = 'OFF' + uuid.uuid4().hex
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO offline_order(order_no,merchant_id,user_id,store_name,amount,paid_amount,status) VALUES(%s,%s,%s,'Synthetic',1000,1000,1)",
                            (number, users[2], users[1]))
            conn.commit()
    else:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO products(name,user_id,status) VALUES('Synthetic config item',%s,1)", (users[2],))
                product = cur.lastrowid
                cur.execute('INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,10,2)',
                            (product, uuid.uuid4().hex))
                sku = cur.lastrowid
            conn.commit()
        created = http.post('/order/create', json={'user_id': users[1], 'delivery_way': 'pickup', 'buy_now': True,
                            'buy_now_items': [{'product_id': product, 'sku_id': sku, 'quantity': 1}]},
                            headers={'Authorization': 'Bearer ' + token(users[1])})
        assert created.status_code == 200
        number = created.json()['order_number']
    return number


def _snapshot(db, user):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT account_type,balance FROM finance_accounts ORDER BY account_type')
            pools = cur.fetchall()
            cur.execute('SELECT member_points,referral_points,true_total_points FROM users WHERE id=%s', (user,))
            balance = cur.fetchone()
            counts = {}
            for table in ('account_flow', 'points_log', 'finance_events', 'payment_events', 'payment_merchant_transfers'):
                cur.execute(f'SELECT COUNT(*) n FROM {table}')
                counts[table] = cur.fetchone()['n']
            return pools, balance, counts


@pytest.mark.parametrize('kind', ['online', 'offline'])
@pytest.mark.parametrize('setting', ['platform_rate', 'pool_allocations'])
@pytest.mark.parametrize('fault', ['db_error', 'corrupt_json_shape', 'invalid_ratio'])
def test_payment_fails_closed_on_unavailable_or_invalid_finance_config(
        db, http, users, token, monkeypatch, signed_callback, finance_config, kind, setting, fault):
    from core.database import get_conn
    from services import finance_service
    from api.wechat_pay import routes
    number = _seed_payment(db, http, users, token, kind)
    account = 'platform_revenue_pool' if setting == 'platform_rate' else 'maintain_pool'
    if fault != 'db_error':
        # MySQL JSON rejects malformed JSON bytes; a valid JSON scalar is a corrupt config shape.
        value = '"corrupt-json"' if fault == 'corrupt_json_shape' else json.dumps(
            {'distribution_platform_rate': 2} if setting == 'platform_rate' else {'allocation': 2})
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE finance_accounts SET config_params=%s WHERE account_type=%s', (value, account))
                assert cur.rowcount == 1
            conn.commit()
    before = _snapshot(db, users[1])
    reached = []
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            normalized = ' '.join(sql.lower().split())
            parameters = args if isinstance(args, (tuple, list)) else ()
            if (fault == 'db_error' and normalized.startswith('select') and 'config_params' in normalized
                    and 'finance_accounts' in normalized and account in parameters):
                reached.append(True)
                raise pymysql.err.OperationalError(1205, 'synthetic config read failure')
            return self.cursor.execute(sql, args)
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs): return Cursor(self.conn.cursor(*args, **kwargs))
    @contextmanager
    def instrumented():
        with get_conn() as conn:
            yield Connection(conn)
    if fault == 'db_error':
        monkeypatch.setattr(finance_service, 'get_conn', instrumented)
        monkeypatch.setattr(routes, 'get_conn', instrumented)
    body, headers = signed_callback({'out_trade_no': number, 'transaction_id': 'TX' + uuid.uuid4().hex,
        'trade_state': 'SUCCESS', 'amount': {'total': 1000, 'currency': 'CNY'}}, 'TRANSACTION.SUCCESS')
    path = '/api/offline/zhifu/notify' if kind == 'offline' else '/api/wechat-pay/notify'
    response = http.post(path, content=body, headers=headers)
    assert response.status_code == 503
    if fault == 'db_error':
        assert reached
    assert _snapshot(db, users[1]) == before
    with db() as conn:
        with conn.cursor() as cur:
            if kind == 'offline':
                cur.execute('SELECT status FROM offline_order WHERE order_no=%s', (number,))
                assert cur.fetchone()['status'] == 1
            else:
                cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
                assert cur.fetchone()['status'] == 'pending_pay'


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_missing_finance_config_fields_use_defaults(
        db, http, users, token, signed_callback, finance_config, kind):
    number = _seed_payment(db, http, users, token, kind)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE finance_accounts SET config_params='{}' WHERE account_type IN ('platform_revenue_pool','maintain_pool')")
        conn.commit()
    body, headers = signed_callback({'out_trade_no': number, 'transaction_id': 'TX' + uuid.uuid4().hex,
        'trade_state': 'SUCCESS', 'amount': {'total': 1000, 'currency': 'CNY'}}, 'TRANSACTION.SUCCESS')
    path = '/api/offline/zhifu/notify' if kind == 'offline' else '/api/wechat-pay/notify'
    assert http.post(path, content=body, headers=headers).status_code == 204
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT total_fee FROM payment_events WHERE order_number=%s', (number,))
            assert cur.fetchall() == [{'total_fee': 1000}]


def test_allocation_partial_save_validates_merged_config_and_is_atomic(db, finance_config):
    from services.finance_service import FinanceService
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT account_type,config_params FROM finance_accounts ORDER BY account_type')
            before = cur.fetchall()
    with pytest.raises(ValueError):
        FinanceService().set_pool_allocations({'fund_pool': '0.02'})
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT account_type,config_params FROM finance_accounts ORDER BY account_type')
            assert cur.fetchall() == before


def test_concurrent_partial_allocation_updates_serialize_against_full_config(
        db, monkeypatch, finance_config):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal
    from services import finance_service

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE finance_accounts SET config_params=%s WHERE account_type='fund_pool'",
                        (json.dumps({'allocation': '0.005'}),))
        conn.commit()
    locked, attempted, release = threading.Event(), threading.Event(), threading.Event()
    connections = []
    class Cursor:
        def __init__(self, cursor, index):
            self.cursor, self.index = cursor, index
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            normalized = ' '.join(sql.lower().split())
            target = normalized.startswith('select get_lock(')
            if target and self.index == 1:
                attempted.set()
            result = self.cursor.execute(sql, args)
            if target and self.index == 0:
                locked.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release allocation lock')
            return result
    class Connection:
        def __init__(self, conn, index):
            self.conn, self.index = conn, index
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs): return Cursor(self.conn.cursor(*args, **kwargs), self.index)
    original_connect = finance_service.pymysql.connect
    def instrumented(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        if kwargs.get('autocommit') is not False:
            return conn
        with conn.cursor() as cur:
            cur.execute('SELECT CONNECTION_ID() id')
            connection_id = cur.fetchone()['id']
        index = len(connections)
        connections.append(connection_id)
        return Connection(conn, index)
    monkeypatch.setattr(finance_service.pymysql, 'connect', instrumented)
    def update(key):
        return finance_service.FinanceService().set_pool_allocations({key: '0.016'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(update, 'maintain_pool')
        try:
            assert locked.wait(15)
            second = pool.submit(update, 'public_welfare')
            assert attempted.wait(15)
            assert len(set(connections[:2])) == 2
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=15)
        with pytest.raises(ValueError):
            second.result(timeout=15)
    final = finance_service.FinanceService().get_pool_allocations()
    assert final['fund_pool'] == Decimal('0.005')
    assert final['maintain_pool'] == Decimal('0.016')
    assert final['public_welfare'] == Decimal('0.01')
    assert sum(value for key, value in final.items() if key != 'merchant_balance') <= Decimal('0.20')


@pytest.mark.parametrize('kind', ['online', 'offline'])
def test_overallocated_full_config_is_payment_storage_failure(
        db, http, users, token, signed_callback, finance_config, kind):
    number = _seed_payment(db, http, users, token, kind)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE finance_accounts SET config_params=%s WHERE account_type='fund_pool'",
                        (json.dumps({'allocation': '0.02'}),))
        conn.commit()
    before = _snapshot(db, users[1])
    body, headers = signed_callback({'out_trade_no': number, 'transaction_id': 'TX' + uuid.uuid4().hex,
        'trade_state': 'SUCCESS', 'amount': {'total': 1000, 'currency': 'CNY'}}, 'TRANSACTION.SUCCESS')
    path = '/api/offline/zhifu/notify' if kind == 'offline' else '/api/wechat-pay/notify'
    assert http.post(path, content=body, headers=headers).status_code == 503
    assert _snapshot(db, users[1]) == before


def test_allocation_save_with_missing_pool_rejects_without_writing(db, finance_config):
    from core.exceptions import FinanceException
    from services.finance_service import FinanceService
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,account_name,account_type,balance,config_params,created_at FROM finance_accounts WHERE account_type='branch_pool'")
            missing = cur.fetchone()
            assert missing is not None
            cur.execute('DELETE FROM finance_accounts WHERE id=%s', (missing['id'],))
        conn.commit()
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id,account_type,balance,config_params FROM finance_accounts ORDER BY id')
                before = cur.fetchall()
        with pytest.raises(FinanceException):
            FinanceService().set_pool_allocations({'maintain_pool': '0.005'})
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id,account_type,balance,config_params FROM finance_accounts ORDER BY id')
                assert cur.fetchall() == before
                cur.execute("SELECT COUNT(*) n FROM finance_accounts WHERE account_type='branch_pool'")
                assert cur.fetchone()['n'] == 0
    finally:
        with db() as conn:
            with conn.cursor() as cur:
                config = missing['config_params']
                if config is not None and not isinstance(config, str):
                    config = json.dumps(config)
                cur.execute('INSERT INTO finance_accounts(id,account_name,account_type,balance,config_params,created_at) VALUES(%s,%s,%s,%s,%s,%s)',
                            (missing['id'], missing['account_name'], missing['account_type'],
                             missing['balance'], config, missing['created_at']))
            conn.commit()


@pytest.mark.parametrize('refund_first', [True, False])
def test_allocation_save_and_refund_share_target_pool_lock(
        db, users, token, monkeypatch, finance_config, refund_first):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal
    from fastapi.testclient import TestClient
    from core.database import get_conn
    from main import app
    from services import finance_service, refund_service

    number = 'ISO' + uuid.uuid4().hex
    accounts = ('branch_pool', 'maintain_pool', 'platform_revenue_pool')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,status,transaction_id) VALUES(%s,%s,%s,1,'pending_ship',%s)",
                        (number, users[1], users[2], 'TX' + uuid.uuid4().hex))
            for account in accounts:
                cur.execute('UPDATE finance_accounts SET balance=balance+0.2 WHERE account_type=%s', (account,))
                cur.execute('SELECT balance FROM finance_accounts WHERE account_type=%s', (account,))
                balance = cur.fetchone()['balance']
                cur.execute("INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark) VALUES(%s,%s,0.2,%s,'income',%s)",
                            (account, users[1], balance, '订单分账: ' + number))
            cur.execute('SELECT account_type,balance FROM finance_accounts WHERE account_type IN (%s,%s,%s)', accounts)
            before = {row['account_type']: row['balance'] for row in cur.fetchall()}
        conn.commit()
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    first_actor = 'refund' if refund_first else 'config'
    ids = {}
    class Cursor:
        def __init__(self, cursor, actor, connection_id):
            self.cursor, self.actor, self.connection_id = cursor, actor, connection_id
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            normalized = ' '.join(sql.lower().split())
            params = args if isinstance(args, (tuple, list)) else ()
            target = ('maintain_pool' in params and (
                (self.actor == 'refund' and normalized.startswith('select')
                 and 'from finance_accounts' in normalized and 'for update' in normalized)
                or (self.actor == 'config' and normalized.startswith('update finance_accounts'))))
            if target:
                ids[self.actor] = self.connection_id
                if self.actor != first_actor:
                    attempted.set()
            result = self.cursor.execute(sql, args)
            if target and self.actor == first_actor and not held.is_set():
                held.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release target pool lock')
            return result
    class Connection:
        def __init__(self, conn, actor, connection_id):
            self.conn, self.actor, self.connection_id = conn, actor, connection_id
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs):
            return Cursor(self.conn.cursor(*args, **kwargs), self.actor, self.connection_id)
    def connection(actor):
        @contextmanager
        def instrumented():
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT CONNECTION_ID() id')
                    connection_id = cur.fetchone()['id']
                yield Connection(conn, actor, connection_id)
        return instrumented
    original_connect = finance_service.pymysql.connect
    def config_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        if kwargs.get('autocommit') is not False:
            return conn
        with conn.cursor() as cur:
            cur.execute('SELECT CONNECTION_ID() id')
            connection_id = cur.fetchone()['id']
        return Connection(conn, 'config', connection_id)
    monkeypatch.setattr(finance_service.pymysql, 'connect', config_connect)
    monkeypatch.setattr(refund_service, 'get_conn', connection('refund'))
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    with TestClient(app, raise_server_exceptions=False) as client:
        def refund():
            return client.post('/wechat-pay/refund', json={'order_number': number, 'refund_fee': 100},
                               headers={'Authorization': 'Bearer ' + token(users[2])})
        def configure():
            return finance_service.FinanceService().set_pool_allocations({'maintain_pool': '0.005'})
        actions = (refund, configure) if refund_first else (configure, refund)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(actions[0])
            try:
                assert held.wait(15)
                second = pool.submit(actions[1])
                assert attempted.wait(15)
                assert ids['refund'] != ids['config']
                assert not second.done()
            finally:
                release.set()
            results = [first.result(timeout=15), second.result(timeout=15)]
        response = results[0] if refund_first else results[1]
        assert response.status_code == 200
        replay = refund()
        assert replay.status_code == 200
        assert replay.json()['refund_no'] == response.json()['refund_no']
    assert calls == [response.json()['refund_no']]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT account_type,balance FROM finance_accounts WHERE account_type IN (%s,%s,%s)', accounts)
            assert {row['account_type']: row['balance'] for row in cur.fetchall()} == {
                account: before[account] - Decimal('0.2') for account in accounts}
            cur.execute('SELECT account_type,COUNT(*) n FROM account_flow WHERE remark=%s GROUP BY account_type',
                        ('退款回冲: ' + number,))
            assert {row['account_type']: row['n'] for row in cur.fetchall()} == {account: 1 for account in accounts}
    assert finance_service.FinanceService().get_pool_allocations()['maintain_pool'] == Decimal('0.005')

"""Actual HTTP and InnoDB state transitions; controlled provider, no money."""
import uuid
import pytest


def create_order(db, users, status):
    number = 'RACE' + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,status,transaction_id,delivery_way) VALUES(%s,%s,%s,1,%s,%s,'pickup')",
                (number, users[1], users[2], status, 'TX' + uuid.uuid4().hex))
        conn.commit()
    return number

def test_order_idempotency_concurrency_scope_conflict_and_rollback(db, users, token, monkeypatch, race):
    from contextlib import contextmanager
    from fastapi.testclient import TestClient
    from main import app
    from api.order import order
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO products(name,user_id,status) VALUES('Idempotency synthetic',%s,1)", (users[2],))
            product = cur.lastrowid
            cur.execute("INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,1,10)",
                        (product, uuid.uuid4().hex))
            sku = cur.lastrowid
        conn.commit()
    key = uuid.uuid4().hex
    body = {'user_id': users[1], 'delivery_way': 'pickup', 'buy_now': True,
            'buy_now_items': [{'product_id': product, 'sku_id': sku, 'quantity': 1}],
            'idempotency_key': key}

    def submit(payload, actor):
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.post('/order/create', json=payload, headers={'Authorization': 'Bearer ' + token(actor)})

    first, second = race(lambda: submit(body, users[1]), lambda: submit(body, users[1]))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    changed = {**body, 'buy_now_items': [{**body['buy_now_items'][0], 'quantity': 2}]}
    assert submit(changed, users[1]).status_code == 409
    other = submit({**body, 'user_id': users[0]}, users[0])
    assert other.status_code == 200
    assert other.json()['order_number'] != first.json()['order_number']

    original = order.get_conn
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            if 'INSERT INTO order_creation_idempotency' in sql:
                raise RuntimeError('synthetic failure before idempotency insert')
            return self.cursor.execute(sql, args)
    class Connection:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        def cursor(self): return Cursor(self.connection.cursor())
    @contextmanager
    def failing_connection():
        with original() as conn:
            yield Connection(conn)
    failed = {**body, 'idempotency_key': uuid.uuid4().hex}
    monkeypatch.setattr(order, 'get_conn', failing_connection)
    assert submit(failed, users[1]).status_code == 500
    monkeypatch.setattr(order, 'get_conn', original)
    retried = submit(failed, users[1])
    assert retried.status_code == 200
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (sku,))
            assert cur.fetchone()['stock'] == 7
            cur.execute('SELECT COUNT(*) AS n FROM order_creation_idempotency WHERE order_number IN (%s,%s,%s)',
                        (first.json()['order_number'], other.json()['order_number'], retried.json()['order_number']))
            assert cur.fetchone()['n'] == 3


def test_cart_snapshot_change_and_locked_checkout_replay(db, users, token, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from fastapi.testclient import TestClient
    from main import app
    from api.order import cart, order

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO products(name,user_id,status) VALUES('Cart snapshot synthetic',%s,1)", (users[2],))
            product = cur.lastrowid
            cur.execute("INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,5,10)",
                        (product, uuid.uuid4().hex))
            sku = cur.lastrowid
            cur.execute("INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,7,10)",
                        (product, uuid.uuid4().hex))
            other_sku = cur.lastrowid
        conn.commit()
    cart.CartManager.add(users[1], product, 1, sku_id=sku)
    cart.CartManager.add(users[1], product, 1, sku_id=other_sku)
    snapshot = [{'product_id': str(product), 'sku_id': str(sku), 'quantity': 1}]
    body = {'user_id': str(users[1]), 'delivery_way': 'pickup', 'cart_snapshot': snapshot,
            'idempotency_key': uuid.uuid4().hex}
    headers = {'Authorization': 'Bearer ' + token(users[1])}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE cart SET quantity=2,selected=0 WHERE user_id=%s AND sku_id=%s', (users[1], sku))
        conn.commit()
    with TestClient(app, raise_server_exceptions=False) as client:
        conflict = client.post('/order/create', json=body, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json() == {'detail': {'code': 'CART_SNAPSHOT_CHANGED', 'message': '购物车已变化，请重新确认'}}
        assert client.post('/order/create', json={**body, 'cart_snapshot': snapshot * 2}, headers=headers).status_code == 422
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (sku,))
            assert cur.fetchone()['stock'] == 10
            cur.execute('UPDATE cart SET quantity=1 WHERE user_id=%s AND sku_id=%s', (users[1], sku))
        conn.commit()

    locked, release, add_started, add_done = (threading.Event() for _ in range(4))
    original = order.get_conn
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            result = self.cursor.execute(sql, args)
            if 'FROM cart c' in sql and 'FOR UPDATE' in sql:
                locked.set()
                assert release.wait(15)
            return result
    class Connection:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        def cursor(self): return Cursor(self.connection.cursor())
    @contextmanager
    def observed_connection():
        with original() as conn:
            yield Connection(conn)
    monkeypatch.setattr(order, 'get_conn', observed_connection)
    def checkout():
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.post('/order/create', json=body, headers=headers)
    def add():
        add_started.set()
        try:
            return cart.CartManager.add(users[1], product, 1, sku_id=sku)
        finally:
            add_done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(checkout)
        assert locked.wait(15)
        added = pool.submit(add)
        assert add_started.wait(15)
        try:
            assert not add_done.wait(0.2)
        finally:
            release.set()
        response = future.result(timeout=15)
        assert added.result(timeout=15)
    assert response.status_code == 200
    monkeypatch.setattr(order, 'get_conn', original)
    with TestClient(app, raise_server_exceptions=False) as client:
        replay = client.post('/order/create', json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == response.json()
        changed = {**body, 'cart_snapshot': [{**snapshot[0], 'quantity': 2}]}
        assert client.post('/order/create', json=changed, headers=headers).status_code == 409
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT quantity FROM cart WHERE user_id=%s AND sku_id=%s', (users[1], sku))
            assert cur.fetchone()['quantity'] == 1
            cur.execute('SELECT quantity FROM cart WHERE user_id=%s AND sku_id=%s', (users[1], other_sku))
            assert cur.fetchone()['quantity'] == 1
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (other_sku,))
            assert cur.fetchone()['stock'] == 10
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (sku,))
            assert cur.fetchone()['stock'] == 9


@pytest.mark.parametrize('delivery_way', ['pickup', 'platform'])
def test_zero_cash_virtual_settlement_and_same_key_replay(db, http, users, token, delivery_way):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO products(name,user_id,status,is_virtual) VALUES('Virtual synthetic',%s,1,1)",
                        (users[2],))
            product = cur.lastrowid
            cur.execute("INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,0,2)",
                        (product, uuid.uuid4().hex))
            sku = cur.lastrowid
        conn.commit()
    body = {'user_id': users[1], 'delivery_way': delivery_way, 'buy_now': True,
            'buy_now_items': [{'product_id': product, 'sku_id': sku, 'quantity': 1}],
            'idempotency_key': uuid.uuid4().hex}
    if delivery_way == 'platform':
        body['custom_address'] = {'consignee_name': 'Synthetic', 'consignee_phone': '13800000000',
                                  'detail': 'Synthetic address'}
    headers = {'Authorization': 'Bearer ' + token(users[1])}
    first = http.post('/order/create', json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()['need_pay'] is False
    second = http.post('/order/create', json=body, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,status,completed_at FROM orders WHERE order_number=%s',
                        (first.json()['order_number'],))
            row = cur.fetchone()
            assert row['status'] == 'completed'
            assert row['completed_at'] is not None
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (sku,))
            assert cur.fetchone()['stock'] == 1
            cur.execute("SELECT COUNT(*) AS n FROM finance_events WHERE event_kind='settlement' AND order_number=%s",
                        (first.json()['order_number'],))
            assert cur.fetchone()['n'] == 1
            cur.execute('SELECT COUNT(*) AS n FROM order_items WHERE order_id=%s', (row['id'],))
            assert cur.fetchone()['n'] == 1


def test_status_cannot_confirm_receipt_for_buyer(db, http, users, token, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[3]))
    number = create_order(db, users, 'pending_recv')
    for actor in (users[2], users[3]):
        response = http.post('/order/status', json={'order_number': number, 'new_status': 'completed'},
                             headers={'Authorization': 'Bearer ' + token(actor)})
        assert response.status_code == 409
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'pending_recv'
    response = http.post('/order/confirm-receive', json={'order_number': number},
                         headers={'Authorization': 'Bearer ' + token(users[1])})
    assert response.status_code == 200
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'completed'



@pytest.mark.parametrize('initial,action', [('pending_recv', 'receive'), ('pending_ship', 'ship')])
@pytest.mark.parametrize('refund_first', [True, False])
def test_refund_fulfillment_serial_orders(db, http, users, token, monkeypatch, initial, action, refund_first):
    from services import refund_service
    class Provider:
        def refund(self, **kwargs):
            return {'status': 'PROCESSING', 'amount': {'refund': kwargs['refund_fee'], 'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    number = create_order(db, users, initial)
    def fulfillment():
        if action == 'receive':
            return http.post('/order/confirm-receive', json={'order_number': number}, headers={'Authorization': 'Bearer ' + token(users[1])})
        return http.post('/merchant/ship', json={'order_number': number, 'logistics_type': 4}, headers={'Authorization': 'Bearer ' + token(users[2])})
    def refund():
        return http.post('/wechat-pay/refund', json={'order_number': number}, headers={'Authorization': 'Bearer ' + token(users[2])})
    if refund_first:
        assert refund().status_code == 200
        result = fulfillment()
        assert result.status_code >= 400 or not result.json().get('ok')
    else:
        assert fulfillment().status_code == 200
        assert refund().status_code == 200
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'refunding'


def test_expiration_prevents_late_payment_mutation(db, users):
    import asyncio
    from api.order.order import _cancel_expire_orders_once
    from api.wechat_pay.routes import handle_transaction_success
    number = create_order(db, users, 'pending_pay')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE orders SET expire_at=DATE_SUB(NOW(),INTERVAL 1 SECOND) WHERE order_number=%s', (number,))
        conn.commit()
    _cancel_expire_orders_once()
    with pytest.raises(ValueError):
        asyncio.run(handle_transaction_success({'out_trade_no': number, 'transaction_id': 'LATE' + uuid.uuid4().hex,
            'amount': {'total': 100, 'currency': 'CNY'}}))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'cancelled'
            cur.execute('SELECT COUNT(*) AS n FROM payment_events WHERE order_number=%s', (number,))
            assert cur.fetchone()['n'] == 0

def test_refund_lock_blocks_receive_until_intent_commit(db, http, users, token, monkeypatch, evidence):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from services import refund_service
    from core.database import get_conn
    number = create_order(db, users, 'pending_recv')
    locked = threading.Event()
    release = threading.Event()
    receive_attempted = threading.Event()
    ids = []
    class Cursor:
        def __init__(self, cur): self.cur = cur
        def __enter__(self): return self
        def __exit__(self, *args): self.cur.close()
        def __getattr__(self, name): return getattr(self.cur, name)
        def execute(self, sql, args=None):
            if 'FROM orders WHERE order_number=%s FOR UPDATE' in sql and sql.startswith('SELECT id, user_id'):
                receive_attempted.set()
            result = self.cur.execute(sql, args)
            if sql == 'SELECT * FROM orders WHERE order_number=%s FOR UPDATE':
                locked.set()
                assert release.wait(15)
            return result
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self): return Cursor(self.conn.cursor())
    @contextmanager
    def instrumented():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT CONNECTION_ID() AS id')
                ids.append(cur.fetchone()['id'])
            yield Connection(conn)
    class Provider:
        def refund(self, **kwargs):
            return {'status': 'PROCESSING', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'get_conn', instrumented)
    from api.order import order as order_routes
    monkeypatch.setattr(order_routes, 'get_conn', instrumented)
    monkeypatch.setattr(refund_service, 'provider', Provider())
    buyer_headers = {'Authorization': 'Bearer ' + token(users[1])}
    merchant_headers = {'Authorization': 'Bearer ' + token(users[2])}
    from fastapi.testclient import TestClient
    from main import app
    # Independent portals ensure async refund lock instrumentation cannot block receipt dispatch.
    with TestClient(app, raise_server_exceptions=False) as refund_http, TestClient(app, raise_server_exceptions=False) as receive_http:
        def refund():
            return refund_http.post('/wechat-pay/refund', json={'order_number': number}, headers=merchant_headers)
        def receive():
            return receive_http.post('/order/confirm-receive', json={'order_number': number}, headers=buyer_headers)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(refund)
            assert locked.wait(15)
            second = pool.submit(receive)
            assert receive_attempted.wait(15)
            assert len(set(ids)) >= 2
            release.set()
            assert first.result(20).status_code == 200
            result = second.result(20)
            assert result.status_code >= 400 or not result.json().get('ok')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'refunding'
    evidence({'scenario': 'refund-lock-before-receive', 'refund_connections': ids, 'final': 'refunding'})

def test_full_refund_restores_coupon_points_once(db, http, users, token, monkeypatch, evidence):
    from decimal import Decimal
    from services import refund_service
    number = create_order(db, users, 'pending_ship')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET member_points=10 WHERE id=%s', (users[1],))
            cur.execute("INSERT INTO coupons(user_id,coupon_type,amount,status,valid_from,valid_to,used_at) VALUES(%s,'user',1,'used',CURDATE(),DATE_ADD(CURDATE(),INTERVAL 7 DAY),NOW())", (users[1],))
            coupon = cur.lastrowid
            cur.execute('UPDATE orders SET pending_points=2,pending_coupon_ids=%s WHERE order_number=%s', ('[' + str(coupon) + ']', number))
        conn.commit()
    class Provider:
        def refund(self, **kwargs):
            return {'out_refund_no': kwargs['out_refund_no'], 'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    response = http.post('/wechat-pay/refund', json={'order_number': number}, headers={'Authorization': 'Bearer ' + token(users[2])})
    assert response.status_code == 200
    refund_no = response.json()['refund_no']
    # Replay actual finalizer after terminal commit, not an isolated SQL surrogate.
    refund_service.RefundService.apply_result({'out_refund_no': refund_no, 'refund_status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}})
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            points = cur.fetchone()['member_points']
            assert points == Decimal('12')
            cur.execute('SELECT status,used_at FROM coupons WHERE id=%s', (coupon,))
            restored = cur.fetchone()
            assert restored == {'status': 'unused', 'used_at': None}
            cur.execute('SELECT COUNT(*) AS n FROM points_log WHERE user_id=%s AND reason=%s', (users[1], '订单退款退回积分: ' + number))
            assert cur.fetchone()['n'] == 1
    evidence({'scenario': 'full-refund-assets-replay', 'points_before': '10', 'points_after': str(points), 'coupon_before': 'used', 'coupon_after': restored})

def test_direct_refund_retry_survives_rejected_later_application(db, http, users, token, monkeypatch):
    from services import refund_service
    number = create_order(db, users, 'pending_ship')
    calls=[]
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status':'SUCCESS','amount':{'refund':40,'total':100,'currency':'CNY'}}
    monkeypatch.setattr(refund_service,'provider',Provider())
    headers={'Authorization':'Bearer '+token(users[2])}
    payload={'order_number':number,'refund_fee':40,'refund_request_id':'same-item'}
    first=http.post('/wechat-pay/refund',json=payload,headers=headers)
    assert first.status_code==200
    assert http.post('/refund/apply',json={'order_number':number,'refund_type':'refund_only','reason_code':'synthetic'},headers={'Authorization':'Bearer '+token(users[1])}).status_code==409
    replay=http.post('/wechat-pay/refund',json=payload,headers=headers)
    assert replay.status_code==200
    assert replay.json()['refund_no']==first.json()['refund_no']
    assert len(calls)==1


@pytest.mark.parametrize('attempts',[0,1])
def test_refund_coordinates_prepared_or_unknown_payout(db,http,users,token,monkeypatch,attempts):
    from services import refund_service
    number=create_order(db,users,'completed')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO payment_merchant_transfers(order_number,merchant_id,amount,out_batch_no,attempts,state) VALUES(%s,%s,80,%s,%s,%s)',(number,users[2],__import__('uuid').uuid4().hex,attempts,'unknown' if attempts else 'prepared'))
        conn.commit()
    calls=[]
    class Provider:
        def refund(self,**kwargs):
            calls.append(kwargs)
            return {'status':'PROCESSING','amount':{'refund':100,'total':100,'currency':'CNY'}}
    monkeypatch.setattr(refund_service,'provider',Provider())
    result=http.post('/wechat-pay/refund',json={'order_number':number},headers={'Authorization':'Bearer '+token(users[2])})
    assert result.status_code==(409 if attempts else 200)
    assert len(calls)==(0 if attempts else 1)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_merchant_transfers WHERE order_number=%s',(number,))
            assert cur.fetchone()['state']==('unknown' if attempts else 'held')

@pytest.mark.parametrize('terminal,expected_amount',[('SUCCESS',48),('CLOSED',80)])
def test_partial_refund_payout_terminal_reservation(db,http,users,token,monkeypatch,terminal,expected_amount):
    from services import refund_service
    number=create_order(db,users,'completed')
    batch=__import__('uuid').uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payment_merchant_transfers(order_number,merchant_id,amount,out_batch_no,state) VALUES(%s,%s,80,%s,'prepared')",(number,users[2],batch))
        conn.commit()
    class Provider:
        def refund(self,**kwargs):
            return {'status':'PROCESSING','amount':{'refund':40,'total':100,'currency':'CNY'}}
    monkeypatch.setattr(refund_service,'provider',Provider())
    response=http.post('/wechat-pay/refund',json={'order_number':number,'refund_fee':40},headers={'Authorization':'Bearer '+token(users[2])})
    assert response.status_code==200
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state,amount FROM payment_merchant_transfers WHERE order_number=%s',(number,))
            assert cur.fetchone()=={'state':'held','amount':80}
    refund_service.RefundService.apply_result({'out_refund_no':response.json()['refund_no'],'refund_status':terminal,'amount':{'refund':40,'total':100,'currency':'CNY'}})
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state,amount,out_batch_no,attempts FROM payment_merchant_transfers WHERE order_number=%s',(number,))
            assert cur.fetchone()=={'state':'prepared','amount':expected_amount,'out_batch_no':batch,'attempts':0}

def test_closed_refund_replay_preserves_identity_and_blocks_new_number(db,http,users,token,monkeypatch):
    from services import refund_service
    number=create_order(db,users,'completed')
    calls=[]
    class Provider:
        def refund(self,**kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status':'PROCESSING','amount':{'refund':40,'total':100,'currency':'CNY'}}
    monkeypatch.setattr(refund_service,'provider',Provider())
    headers={'Authorization':'Bearer '+token(users[2])}
    payload={'order_number':number,'refund_fee':40,'refund_request_id':'A'}
    first=http.post('/wechat-pay/refund',json=payload,headers=headers).json()['refund_no']
    closed={'out_refund_no':first,'refund_status':'CLOSED','amount':{'refund':40,'total':100,'currency':'CNY'}}
    refund_service.RefundService.apply_result(closed)
    replay=http.post('/wechat-pay/refund',json=payload,headers=headers)
    assert replay.status_code==200 and replay.json()['refund_no']==first and replay.json()['status']=='closed'
    assert http.post('/wechat-pay/refund',json={**payload,'refund_request_id':'B'},headers=headers).status_code==409
    refund_service.RefundService.apply_result(closed)
    assert calls==[first]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s',(number,))
            assert cur.fetchone()['status']=='completed'

def test_refund_apply_invalid_enum_rejected_before_database(http,users,token):
    response=http.post('/refund/apply',json={'order_number':'synthetic','refund_type':'refund','reason_code':'synthetic'},headers={'Authorization':'Bearer '+token(users[1])})
    assert response.status_code==422

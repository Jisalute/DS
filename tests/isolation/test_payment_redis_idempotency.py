"""Real MySQL transactions and isolated Redis; no external provider calls."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from fastapi.testclient import TestClient


def _product(db, merchant):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO products(name,user_id,status) VALUES('Synthetic idempotency item',%s,1)", (merchant,))
            product = cur.lastrowid
            cur.execute('INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,1,10)',
                        (product, uuid.uuid4().hex))
            sku = cur.lastrowid
        conn.commit()
    return product, sku


def _payload(user, product, sku, key):
    return {'user_id': user, 'delivery_way': 'pickup', 'buy_now': True, 'idempotency_key': key,
            'buy_now_items': [{'product_id': product, 'sku_id': sku, 'quantity': 1}]}


def test_redis_same_user_concurrent_create_and_replay(db, isolated_redis, users, token, monkeypatch):
    from api.order import order
    from core.database import get_conn
    from main import app

    product, sku = _product(db, users[2])
    key = 'iso-' + uuid.uuid4().hex
    headers = {'Authorization': 'Bearer ' + token(users[1])}
    payload = _payload(users[1], product, sku, key)
    locked, attempted, release = threading.Event(), threading.Event(), threading.Event()
    connections = []

    class Cursor:
        def __init__(self, cursor, index):
            self.cursor, self.index = cursor, index
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.cursor.close()
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            target = ' '.join(sql.split()).upper() == 'SELECT 1 FROM USERS WHERE ID=%S FOR UPDATE'
            if target and self.index == 1:
                attempted.set()
            result = self.cursor.execute(sql, args)
            if target and self.index == 0:
                locked.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release user row lock')
            return result

    class Connection:
        def __init__(self, conn, index):
            self.conn, self.index = conn, index
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def cursor(self, *args, **kwargs):
            return Cursor(self.conn.cursor(*args, **kwargs), self.index)

    @contextmanager
    def gated_connection():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT CONNECTION_ID() id')
                connection_id = cur.fetchone()['id']
            index = len(connections)
            connections.append(connection_id)
            yield Connection(conn, index)

    monkeypatch.setattr(order, 'get_conn', gated_connection)
    with TestClient(app, raise_server_exceptions=False) as client1, TestClient(app, raise_server_exceptions=False) as client2:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client1.post, '/order/create', json=payload, headers=headers)
            try:
                assert locked.wait(15)
                second = pool.submit(client2.post, '/order/create', json=payload, headers=headers)
                assert attempted.wait(15)
                assert len(set(connections[:2])) == 2
                assert not second.done()
            finally:
                release.set()
            created = first.result(timeout=15)
            assert created.status_code == 200
            concurrent = second.result(timeout=15)
        assert concurrent.status_code == 200
        number = created.json()['order_number']
        assert concurrent.json() == created.json()
        replay = client2.post('/order/create', json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()['order_number'] == number
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM orders WHERE user_id=%s', (users[1],))
            assert cur.fetchone()['n'] == 1
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (sku,))
            assert cur.fetchone()['stock'] == 9
            cur.execute('SELECT COUNT(*) n, SUM(quantity) quantity FROM order_items WHERE sku_id=%s', (sku,))
            assert cur.fetchone() == {'n': 1, 'quantity': 1}


def test_redis_idempotency_key_is_scoped_to_buyer(db, isolated_redis, http, users, token):
    product, sku = _product(db, users[2])
    key = 'iso-' + uuid.uuid4().hex
    responses = []
    for user in (users[1], users[3]):
        response = http.post('/order/create', json=_payload(user, product, sku, key),
                             headers={'Authorization': 'Bearer ' + token(user)})
        responses.append(response)
        assert response.status_code == 200
    numbers = [response.json()['order_number'] for response in responses]
    assert len(set(numbers)) == 2
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT user_id,order_number FROM orders WHERE order_number IN (%s,%s)', tuple(numbers))
            assert {row['user_id'] for row in cur.fetchall()} == {users[1], users[3]}
            cur.execute('SELECT stock FROM product_skus WHERE id=%s', (sku,))
            assert cur.fetchone()['stock'] == 8

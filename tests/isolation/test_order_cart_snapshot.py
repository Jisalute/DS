"""Cart selection and idempotent checkout against actual HTTP and MySQL."""
import uuid

import pytest


@pytest.fixture
def cart_pair(db, users):
    rows = []
    with db() as conn:
        with conn.cursor() as cur:
            for label, price in (('A', 2), ('B', 3)):
                cur.execute('INSERT INTO products(name,user_id,status) VALUES(%s,%s,1)',
                            ('Synthetic cart ' + label, users[2]))
                product = cur.lastrowid
                cur.execute('INSERT INTO product_skus(product_id,sku_code,price,stock) VALUES(%s,%s,%s,10)',
                            (product, uuid.uuid4().hex, price))
                sku = cur.lastrowid
                # Both remain selected in server storage: the client snapshot chooses only A.
                cur.execute('INSERT INTO cart(user_id,product_id,sku_id,quantity,selected) VALUES(%s,%s,%s,1,1)',
                            (users[1], product, sku))
                rows.append({'product_id': product, 'sku_id': sku, 'cart_id': cur.lastrowid})
        conn.commit()
    return rows


def _payload(users, row):
    return {'user_id': str(users[1]), 'delivery_way': 'pickup', 'buy_now': False,
            'idempotency_key': 'iso-' + uuid.uuid4().hex,
            'cart_snapshot': [{'product_id': str(row['product_id']), 'sku_id': str(row['sku_id']), 'quantity': 1}]}


def test_cart_checkout_only_snapshot_subset(db, http, users, token, cart_pair):
    a, b = cart_pair
    response = http.post('/order/create', json=_payload(users, a),
                         headers={'Authorization': 'Bearer ' + token(users[1])})
    assert response.status_code == 200
    number = response.json()['order_number']
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,total_amount FROM orders WHERE order_number=%s AND user_id=%s', (number, users[1]))
            order = cur.fetchone()
            assert order['total_amount'] == 2
            cur.execute('SELECT product_id,sku_id,quantity FROM order_items WHERE order_id=%s', (order['id'],))
            assert cur.fetchall() == [{'product_id': a['product_id'], 'sku_id': a['sku_id'], 'quantity': 1}]
            cur.execute('SELECT id,quantity,selected FROM cart WHERE user_id=%s', (users[1],))
            assert cur.fetchall() == [{'id': b['cart_id'], 'quantity': 1, 'selected': 1}]
            cur.execute('SELECT id,stock FROM product_skus WHERE id IN (%s,%s)', (a['sku_id'], b['sku_id']))
            assert {r['id']: r['stock'] for r in cur.fetchall()} == {a['sku_id']: 9, b['sku_id']: 10}


def test_cart_quantity_changed_elsewhere_conflicts_atomically(db, http, users, token, cart_pair):
    a, b = cart_pair
    payload = _payload(users, a)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE cart SET quantity=2 WHERE id=%s', (a['cart_id'],))
        conn.commit()
    result = http.post('/order/create', json=payload, headers={'Authorization': 'Bearer ' + token(users[1])})
    assert result.status_code == 409
    assert result.json()['detail']['code'] == 'CART_SNAPSHOT_CHANGED'
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM orders WHERE user_id=%s', (users[1],))
            assert cur.fetchone()['n'] == 0
            cur.execute('SELECT id,quantity FROM cart WHERE user_id=%s', (users[1],))
            assert {r['id']: r['quantity'] for r in cur.fetchall()} == {a['cart_id']: 2, b['cart_id']: 1}
            cur.execute('SELECT id,stock FROM product_skus WHERE id IN (%s,%s)', (a['sku_id'], b['sku_id']))
            assert {r['id']: r['stock'] for r in cur.fetchall()} == {a['sku_id']: 10, b['sku_id']: 10}


def test_cart_checkout_replay_survives_cart_change_and_deletion(db, http, users, token, cart_pair):
    a, b = cart_pair
    payload = _payload(users, a)
    headers = {'Authorization': 'Bearer ' + token(users[1])}
    first = http.post('/order/create', json=payload, headers=headers)
    assert first.status_code == 200
    # The purchased A row is gone; another device then re-adds it with another quantity.
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO cart(user_id,product_id,sku_id,quantity,selected) VALUES(%s,%s,%s,4,1)',
                        (users[1], a['product_id'], a['sku_id']))
            cur.execute('DELETE FROM cart WHERE id=%s', (b['cart_id'],))
        conn.commit()
    replay = http.post('/order/create', json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM orders WHERE user_id=%s', (users[1],))
            assert cur.fetchone()['n'] == 1
            cur.execute('SELECT product_id,sku_id,quantity FROM cart WHERE user_id=%s', (users[1],))
            assert cur.fetchall() == [{'product_id': a['product_id'], 'sku_id': a['sku_id'], 'quantity': 4}]
            cur.execute('SELECT id,stock FROM product_skus WHERE id IN (%s,%s)', (a['sku_id'], b['sku_id']))
            assert {r['id']: r['stock'] for r in cur.fetchall()} == {a['sku_id']: 9, b['sku_id']: 10}

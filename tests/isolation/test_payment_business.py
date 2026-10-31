"""CONTROLLED_PROVIDER_BOUNDARY: real HTTP, cryptography and MySQL; no money."""
import base64
import json
import time
import uuid
from contextlib import contextmanager
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@pytest.fixture
def paid_order(db, users):
    number = 'ISO' + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET member_points=10 WHERE id=%s', (users[1],))
            cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,pending_points,status,transaction_id) VALUES(%s,%s,%s,1,2,'pending_ship',%s)",
                (number, users[1], users[2], 'TX' + uuid.uuid4().hex))
            cur.execute("UPDATE finance_accounts SET balance=balance+1 WHERE account_type='maintain_pool'")
            cur.execute("INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark) SELECT 'maintain_pool',%s,1,balance,'income',%s FROM finance_accounts WHERE account_type='maintain_pool'", (users[1], '订单分账: ' + number))
        conn.commit()
    return number


@pytest.fixture
def signed_callback(monkeypatch):
    from api.wechat_pay import routes
    from core.config import settings
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    aes = b'I' * 32
    monkeypatch.setattr(routes.pay_client, 'wechat_public_key', key.public_key())
    monkeypatch.setattr(routes.pay_client, 'apiv3_key', aes)
    monkeypatch.setattr(settings, 'WECHAT_PAY_PUB_KEY_ID', 'ISOLATED_RSA_KEY')
    monkeypatch.setattr(settings, 'WECHAT_PAY_MCH_ID', 'ISOLATED_MERCHANT')
    def encode(data, event='REFUND.SUCCESS'):
        nonce = 'isolation123'
        resource = {'nonce': nonce, 'associated_data': 'isolation',
            'ciphertext': base64.b64encode(AESGCM(aes).encrypt(nonce.encode(),
                json.dumps({**data, 'mchid': 'ISOLATED_MERCHANT'}).encode(), b'isolation')).decode()}
        body = json.dumps({'id': uuid.uuid4().hex, 'event_type': event, 'resource': resource})
        timestamp = str(int(time.time()))
        signature = key.sign(f'{timestamp}\nrequestnonce\n{body}\n'.encode(), padding.PKCS1v15(), hashes.SHA256())
        return body, {'Wechatpay-Signature': base64.b64encode(signature).decode(),
            'Wechatpay-Timestamp': timestamp, 'Wechatpay-Nonce': 'requestnonce', 'Wechatpay-Serial': 'ISOLATED_RSA_KEY'}
    return encode


def test_external_accepted_commit_failure_recovery(db, http, users, token, paid_order, monkeypatch, evidence, signed_callback):
    from services import refund_service
    from core.database import get_conn
    accepted = []
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
            pool_before = cur.fetchone()['balance']
    results = {}
    class Provider:
        def refund(self, **kwargs):
            number = kwargs['out_refund_no']
            accepted.append(number)
            result = {'out_refund_no': number, 'refund_status': 'SUCCESS', 'refund_id': 'ISOLATED_REFUND',
                'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
            results[number] = result
            return result
        def query_refund(self, number, **kwargs):
            return results[number]
    monkeypatch.setattr(refund_service, 'provider', Provider())
    # Fail exactly the transaction after provider acceptance, not the durable intent/lease commits.
    class FailingCommit:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def commit(self):
            if accepted:
                raise RuntimeError('ISOLATED_COMMIT_FAILURE')
            self.conn.commit()
    @contextmanager
    def failing_conn():
        with get_conn() as conn:
            yield FailingCommit(conn)
    monkeypatch.setattr(refund_service, 'get_conn', failing_conn)
    response = http.post('/wechat-pay/refund', json={'order_number': paid_order, 'refund_fee': 100},
        headers={'Authorization': 'Bearer ' + token(users[2])})
    assert response.status_code >= 500
    assert len(accepted) == 1
    number = accepted[0]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_refund_intents WHERE out_refund_no=%s', (number,))
            assert cur.fetchone()['state'] == 'unknown'
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunding'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 10
            cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
            assert cur.fetchone()['balance'] == pool_before
            cur.execute('SELECT COUNT(*) n FROM finance_events WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['n'] == 0
            cur.execute('UPDATE payment_refund_intents SET next_attempt_at=NOW() WHERE out_refund_no=%s', (number,))
        conn.commit()
    monkeypatch.setattr(refund_service, 'get_conn', get_conn)
    refund_service.RefundService.recover(number)
    for _ in range(2):
        body, headers = signed_callback(results[number])
        replay = http.post('/api/wechat-pay/refund-notify', content=body, headers=headers)
        assert replay.status_code == 204
    assert accepted == [number]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT state,attempts FROM payment_refund_intents WHERE out_refund_no=%s', (number,))
            final = cur.fetchone()
            assert final['state'] == 'success'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 12
            cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
            assert cur.fetchone()['balance'] == pool_before - 1
            cur.execute('SELECT COUNT(*) n,SUM(change_amount) delta FROM account_flow WHERE account_type=%s AND remark=%s', ('maintain_pool', '退款回冲: ' + paid_order))
            assert cur.fetchone() == {'n': 1, 'delta': -1}
            cur.execute("SELECT COUNT(*) n,SUM(change_amount) delta FROM points_log WHERE user_id=%s AND reason=%s", (users[1], '订单退款退回积分: ' + paid_order))
            assert cur.fetchone() == {'n': 1, 'delta': 2}
    evidence({'provider_evidence': 'CONTROLLED_BOUNDARY_ONLY', 'external_calls': len(accepted),
        'local_commit_failure': response.status_code, 'replay': 'twice', 'final': final})


def test_unsigned_callback_cannot_mutate(http, paid_order):
    response = http.post('/api/wechat-pay/notify', json={'out_trade_no': paid_order, 'trade_state': 'SUCCESS'})
    assert response.status_code == 401


def test_processing_rejects_tampering_then_signed_success_replays_once(
        db, http, users, token, paid_order, monkeypatch, signed_callback):
    from services import refund_service

    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs)
            return {'status': 'PROCESSING', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    merchant = {'Authorization': 'Bearer ' + token(users[2])}
    buyer = {'Authorization': 'Bearer ' + token(users[1])}
    stranger = {'Authorization': 'Bearer ' + token(users[3])}
    body = {'order_number': paid_order, 'refund_fee': 100, 'refund_request_id': 'item'}
    assert http.post('/wechat-pay/refund', json=body, headers=stranger).status_code == 403
    assert http.post('/wechat-pay/refund', json=body, headers=buyer).status_code == 403
    for bad in (0, -1, 101, True, '100'):
        assert http.post('/wechat-pay/refund', json={**body, 'refund_fee': bad}, headers=merchant).status_code == 422
    assert calls == []
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
            balance = cur.fetchone()['balance']
    response = http.post('/wechat-pay/refund', json=body, headers=merchant)
    assert response.status_code == 200
    assert response.json()['status'] == 'processing'
    number = response.json()['refund_no']
    assert http.post('/wechat-pay/refund', json={**body, 'refund_fee': 99}, headers=merchant).status_code == 409
    assert http.post('/wechat-pay/refund', json={**body, 'refund_request_id': 'another'}, headers=merchant).status_code == 409
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunding'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 10
            cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
            assert cur.fetchone()['balance'] == balance
            cur.execute('SELECT COUNT(*) n FROM finance_events WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['n'] == 0
    result = {'out_refund_no': number, 'refund_status': 'SUCCESS',
              'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    for invalid in ({**result, 'amount': {'refund': 99, 'total': 100, 'currency': 'CNY'}},
                    {**result, 'transaction_id': 'WRONG_TRANSACTION'}):
        content, headers = signed_callback(invalid)
        assert http.post('/api/wechat-pay/refund-notify', content=content, headers=headers).status_code == 400
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_refund_intents WHERE out_refund_no=%s', (number,))
            assert cur.fetchone()['state'] == 'processing'
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunding'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 10
    for _ in range(2):
        content, headers = signed_callback(result)
        assert http.post('/api/wechat-pay/refund-notify', content=content, headers=headers).status_code == 204
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 12
            cur.execute("SELECT balance FROM finance_accounts WHERE account_type='maintain_pool'")
            assert cur.fetchone()['balance'] == balance - 1
            cur.execute('SELECT COUNT(*) n FROM points_log WHERE user_id=%s AND reason=%s',
                        (users[1], '订单退款退回积分: ' + paid_order))
            assert cur.fetchone()['n'] == 1
            cur.execute('SELECT COUNT(*) n FROM account_flow WHERE account_type=%s AND remark=%s',
                        ('maintain_pool', '退款回冲: ' + paid_order))
            assert cur.fetchone()['n'] == 1
    assert len(calls) == 1


@pytest.mark.parametrize('query_found', [True, False])
def test_unknown_retry_queries_same_refund_number_before_resubmission(
        db, http, users, token, paid_order, monkeypatch, query_found):
    from services import refund_service
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(('refund', kwargs['out_refund_no']))
            if len(calls) == 1:
                raise TimeoutError('synthetic unknown result')
            return {'status': 'PROCESSING', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
        def query_refund(self, number, **kwargs):
            calls.append(('query', number))
            if query_found:
                return {'status': 'PROCESSING', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
            return None
    monkeypatch.setattr(refund_service, 'provider', Provider())
    headers = {'Authorization': 'Bearer ' + token(users[2])}
    payload = {'order_number': paid_order, 'refund_fee': 100, 'refund_request_id': 'unknown'}
    assert http.post('/wechat-pay/refund', json=payload, headers=headers).status_code == 503
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT out_refund_no,state FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            intent = cur.fetchone()
            assert intent['state'] == 'unknown'
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunding'
            cur.execute('UPDATE payment_refund_intents SET next_attempt_at=NOW() WHERE out_refund_no=%s',
                        (intent['out_refund_no'],))
        conn.commit()
    retried = http.post('/wechat-pay/refund', json=payload, headers=headers)
    assert retried.status_code == 200
    assert retried.json()['refund_no'] == intent['out_refund_no']
    assert retried.json()['status'] == 'processing'
    expected = [('refund', intent['out_refund_no']), ('query', intent['out_refund_no'])]
    if not query_found:
        expected.append(('refund', intent['out_refund_no']))
    assert calls == expected


def test_concurrent_same_refund_claims_one_provider_attempt(
        db, users, token, paid_order, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from main import app
    from services import refund_service

    entered, release = threading.Event(), threading.Event()
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            entered.set()
            if not release.wait(15):
                raise TimeoutError('test did not release provider')
            return {'status': 'PROCESSING', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    headers = {'Authorization': 'Bearer ' + token(users[2])}
    payload = {'order_number': paid_order, 'refund_fee': 100, 'refund_request_id': 'race'}
    with TestClient(app, raise_server_exceptions=False) as first_client, TestClient(app, raise_server_exceptions=False) as second_client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_client.post, '/wechat-pay/refund', json=payload, headers=headers)
            try:
                assert entered.wait(15)
                second = pool.submit(second_client.post, '/wechat-pay/refund', json=payload, headers=headers)
                replay = second.result(timeout=15)
                assert replay.status_code == 200
                assert replay.json()['status'] == 'unknown'
                assert replay.json()['refund_no'] == calls[0]
            finally:
                release.set()
            assert first.result(timeout=15).status_code == 200
    assert len(calls) == 1
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state,attempts,out_refund_no FROM payment_refund_intents WHERE order_number=%s',
                        (paid_order,))
            assert cur.fetchall() == [{'state': 'processing', 'attempts': 1, 'out_refund_no': calls[0]}]



@pytest.mark.parametrize('actor', ['merchant', 'admin'])
def test_online_partial_refunds_have_distinct_ids_and_replay_once(
        db, http, users, token, paid_order, monkeypatch, actor):
    from core.config import settings
    from services import refund_service
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[3]))
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'SUCCESS', 'amount': {'refund': kwargs['refund_fee'],
                    'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    headers = {'Authorization': 'Bearer ' + token(users[2] if actor == 'merchant' else users[3])}
    payloads = [{'order_number': paid_order, 'refund_fee': fee, 'refund_request_id': key}
                for fee, key in ((40, 'first'), (60, 'second'))]
    numbers = []
    for payload in payloads:
        response = http.post('/wechat-pay/refund', json=payload, headers=headers)
        assert response.status_code == 200
        number = response.json()['refund_no']
        replay = http.post('/wechat-pay/refund', json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()['refund_no'] == number
        numbers.append(number)
    assert len(set(numbers)) == 2
    assert calls == numbers
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state,refund_fee FROM payment_refund_intents WHERE order_number=%s ORDER BY id', (paid_order,))
            assert cur.fetchall() == [{'state': 'success', 'refund_fee': 40}, {'state': 'success', 'refund_fee': 60}]
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 12
            cur.execute('SELECT COUNT(*) n FROM points_log WHERE user_id=%s AND reason=%s',
                        (users[1], '订单退款退回积分: ' + paid_order))
            assert cur.fetchone()['n'] == 1


def test_application_requires_merchant_audit_and_audit_replay(
        db, http, users, token, paid_order, monkeypatch):
    from services import refund_service
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'PROCESSING', 'amount': {'refund': kwargs['refund_fee'],
                    'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    buyer = {'Authorization': 'Bearer ' + token(users[1])}
    merchant = {'Authorization': 'Bearer ' + token(users[2])}
    assert http.post('/refund/apply', json={'order_number': paid_order, 'refund_type': 'refund_only',
                     'reason_code': 'synthetic'}, headers=buyer).status_code == 200
    direct = {'order_number': paid_order, 'refund_fee': 40, 'refund_request_id': 'bypass'}
    assert http.post('/wechat-pay/refund', json=direct, headers=merchant).status_code == 409
    assert calls == []
    audit = {'order_number': paid_order, 'approve': True}
    assert http.post('/refund/audit', json=audit, headers=buyer).status_code == 403
    for _ in range(2):
        assert http.post('/refund/audit', json=audit, headers=merchant).status_code == 200
    assert len(calls) == 1
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state,out_refund_no FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            assert cur.fetchall() == [{'state': 'processing', 'out_refund_no': calls[0]}]


def test_closed_refund_replays_identity_but_blocks_new_request(
        db, http, users, token, paid_order, monkeypatch):
    from services import refund_service
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'CLOSED', 'amount': {'refund': kwargs['refund_fee'],
                    'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    headers = {'Authorization': 'Bearer ' + token(users[2])}
    payload = {'order_number': paid_order, 'refund_fee': 40, 'refund_request_id': 'closed'}
    first = http.post('/wechat-pay/refund', json=payload, headers=headers)
    assert first.status_code == 200
    replay = http.post('/wechat-pay/refund', json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json()['refund_no'] == first.json()['refund_no']
    assert http.post('/wechat-pay/refund', json={**payload, 'refund_request_id': 'new'}, headers=headers).status_code == 409
    assert calls == [first.json()['refund_no']]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            assert cur.fetchall() == [{'state': 'closed'}]


@pytest.mark.parametrize('actor', ['merchant', 'admin'])
def test_offline_partial_refunds_use_explicit_request_identity(
        db, http, users, token, monkeypatch, actor):
    from core.config import settings
    from services import refund_service
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[3]))
    number = 'OFF' + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO offline_order(order_no,merchant_id,user_id,store_name,amount,paid_amount,status) VALUES(%s,%s,%s,'Synthetic',100,100,2)",
                        (number, users[2], users[1]))
            cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,status,transaction_id) VALUES(%s,%s,%s,1,'completed',%s)",
                        (number, users[1], users[2], 'TX' + uuid.uuid4().hex))
        conn.commit()
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'SUCCESS', 'amount': {'refund': kwargs['refund_fee'],
                    'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    buyer = {'Authorization': 'Bearer ' + token(users[1])}
    headers = {'Authorization': 'Bearer ' + token(users[2] if actor == 'merchant' else users[3])}
    payloads = [{'order_no': number, 'refund_amount': fee, 'refund_request_id': key}
                for fee, key in ((40, 'first'), (60, 'second'))]
    assert http.post('/api/offline/tuikuan', json=payloads[0], headers=buyer).status_code == 403
    numbers = []
    for payload in payloads:
        response = http.post('/api/offline/tuikuan', json=payload, headers=headers)
        assert response.status_code == 200
        refund_no = response.json()['data']['refund_no']
        replay = http.post('/api/offline/tuikuan', json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()['data']['refund_no'] == refund_no
        numbers.append(refund_no)
    assert len(set(numbers)) == 2
    assert calls == numbers
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state,refund_fee FROM payment_refund_intents WHERE order_number=%s ORDER BY id', (number,))
            assert cur.fetchall() == [{'state': 'success', 'refund_fee': 40}, {'state': 'success', 'refund_fee': 60}]
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (number,))
            assert cur.fetchone()['status'] == 'refunded'


def test_rejected_application_cannot_be_bypassed_by_direct_refund(
        db, http, users, token, paid_order, monkeypatch):
    from services import refund_service
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError('rejected application must not reach provider')
    monkeypatch.setattr(refund_service, 'provider', Provider())
    buyer = {'Authorization': 'Bearer ' + token(users[1])}
    merchant = {'Authorization': 'Bearer ' + token(users[2])}
    assert http.post('/refund/apply', json={'order_number': paid_order, 'refund_type': 'refund_only',
                     'reason_code': 'synthetic'}, headers=buyer).status_code == 200
    assert http.post('/refund/audit', json={'order_number': paid_order, 'approve': False,
                     'reject_reason': 'synthetic'}, headers=merchant).status_code == 200
    response = http.post('/wechat-pay/refund', json={'order_number': paid_order, 'refund_fee': 40,
                         'refund_request_id': 'rejected'}, headers=merchant)
    assert response.status_code == 409
    assert calls == []
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['n'] == 0


@pytest.mark.parametrize('untrusted', ['unsigned', 'invalid_signature', 'wrong_serial', 'other_code'])
def test_unproven_provider_404_cannot_trigger_resubmission(
        db, http, users, token, paid_order, monkeypatch, untrusted):
    import requests
    from services import refund_service
    from core.wx_pay_client import wxpay_client

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(wxpay_client, 'wechat_public_key', key.public_key())
    monkeypatch.setattr(wxpay_client, 'pub_key_id', 'ISOLATED_KEY')
    monkeypatch.setattr(wxpay_client, '_build_auth_header', lambda *args: 'synthetic')
    monkeypatch.setattr(wxpay_client, '_get_merchant_serial_no', lambda: 'synthetic')
    monkeypatch.setattr(refund_service, 'provider', wxpay_client)
    sent = []
    query_kind = [untrusted]

    def response(status, data, kind='signed'):
        result = requests.Response()
        result.status_code = status
        result._content = json.dumps(data).encode()
        timestamp, nonce = str(int(time.time())), 'synthetic-nonce'
        signature = key.sign(timestamp.encode() + b'\n' + nonce.encode() + b'\n' + result.content + b'\n',
                             padding.PKCS1v15(), hashes.SHA256())
        if kind == 'invalid_signature':
            signature = b'invalid'
        if kind != 'unsigned':
            result.headers.update({'Wechatpay-Timestamp': timestamp, 'Wechatpay-Nonce': nonce,
                                   'Wechatpay-Serial': 'WRONG' if kind == 'wrong_serial' else 'ISOLATED_KEY',
                                   'Wechatpay-Signature': base64.b64encode(signature).decode()})
        return result

    def post(url, data, **kwargs):
        payload = json.loads(data)
        sent.append(payload['out_refund_no'])
        if len(sent) == 1:
            raise requests.Timeout('synthetic unknown')
        return response(200, {'out_refund_no': sent[-1], 'status': 'PROCESSING',
                              'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}})
    def get(url, **kwargs):
        code = 'SYSTEM_ERROR' if query_kind[0] == 'other_code' else 'RESOURCE_NOT_EXISTS'
        return response(404, {'code': code}, query_kind[0])
    monkeypatch.setattr(wxpay_client.session, 'post', post)
    monkeypatch.setattr(wxpay_client.session, 'get', get)
    headers = {'Authorization': 'Bearer ' + token(users[2])}
    payload = {'order_number': paid_order, 'refund_fee': 100, 'refund_request_id': 'provider-proof'}
    assert http.post('/wechat-pay/refund', json=payload, headers=headers).status_code == 503
    def expire_lease():
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE payment_refund_intents SET next_attempt_at=NOW() WHERE order_number=%s', (paid_order,))
            conn.commit()
    expire_lease()
    assert http.post('/wechat-pay/refund', json=payload, headers=headers).status_code == 503
    assert len(sent) == 1
    query_kind[0] = 'signed'
    expire_lease()
    recovered = http.post('/wechat-pay/refund', json=payload, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()['refund_no'] == sent[0]
    assert sent == [sent[0], sent[0]]


def test_refund_notification_event_must_match_status(
        db, http, users, token, paid_order, monkeypatch, signed_callback):
    from services import refund_service
    class Provider:
        def refund(self, **kwargs):
            return {'status': 'PROCESSING', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    response = http.post('/wechat-pay/refund', json={'order_number': paid_order},
                         headers={'Authorization': 'Bearer ' + token(users[2])})
    assert response.status_code == 200
    number = response.json()['refund_no']
    payload = {'out_refund_no': number, 'refund_status': 'SUCCESS',
               'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    body, headers = signed_callback(payload, event='REFUND.CLOSED')
    assert http.post('/api/wechat-pay/refund-notify', content=body, headers=headers).status_code == 400
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_refund_intents WHERE out_refund_no=%s', (number,))
            assert cur.fetchone()['state'] == 'processing'
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunding'


def test_change_recovery_queries_original_number_and_can_finish(
        db, http, users, token, paid_order, monkeypatch):
    from core.config import settings
    from services import refund_service
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[3]))
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(('refund', kwargs['out_refund_no']))
            return {'status': 'CHANGE', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
        def query_refund(self, number, **kwargs):
            calls.append(('query', number))
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    first = http.post('/wechat-pay/refund', json={'order_number': paid_order},
                      headers={'Authorization': 'Bearer ' + token(users[2])})
    assert first.status_code == 200
    assert first.json()['status'] == 'change'
    number = first.json()['refund_no']
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE payment_refund_intents SET next_attempt_at=NOW() WHERE out_refund_no=%s', (number,))
        conn.commit()
    recovery = http.post('/refund/sync-status/' + paid_order,
                         headers={'Authorization': 'Bearer ' + token(users[3])})
    assert recovery.status_code == 200
    assert recovery.json()['refund_no'] == number
    assert recovery.json()['status'] == 'success'
    assert calls == [('refund', number), ('query', number)]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_refund_intents WHERE out_refund_no=%s', (number,))
            assert cur.fetchone()['state'] == 'success'
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunded'


def test_unsigned_provider_success_does_not_finalize_refund(
        db, http, users, token, paid_order, monkeypatch):
    import requests
    from core.wx_pay_client import wxpay_client
    from services import refund_service
    monkeypatch.setattr(wxpay_client, 'wechat_public_key',
                        rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key())
    monkeypatch.setattr(wxpay_client, 'pub_key_id', 'ISOLATED_KEY')
    monkeypatch.setattr(wxpay_client, '_build_auth_header', lambda *args: 'synthetic')
    monkeypatch.setattr(wxpay_client, '_get_merchant_serial_no', lambda: 'synthetic')
    monkeypatch.setattr(refund_service, 'provider', wxpay_client)
    def unsigned_success(url, data, **kwargs):
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps({'out_refund_no': json.loads(data)['out_refund_no'],
            'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}).encode()
        return result
    monkeypatch.setattr(wxpay_client.session, 'post', unsigned_success)
    result = http.post('/wechat-pay/refund', json={'order_number': paid_order},
                       headers={'Authorization': 'Bearer ' + token(users[2])})
    assert result.status_code == 503
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM payment_refund_intents WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['state'] == 'unknown'
            cur.execute('SELECT status FROM orders WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['status'] == 'refunding'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == 10


@pytest.mark.parametrize('provider_state', ['SUCCESS', 'PROCESSING', 'CLOSED', 'LEGACY'])
def test_buyer_application_requires_order_without_existing_refund_intent(
        db, http, users, token, paid_order, monkeypatch, provider_state):
    from services import refund_service
    fresh_order = 'ISO' + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO orders(order_number,user_id,merchant_id,total_amount,status,transaction_id) VALUES(%s,%s,%s,1,'pending_ship',%s)",
                        (fresh_order, users[1], users[2], 'TX' + uuid.uuid4().hex))
        conn.commit()
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': provider_state, 'amount': {'refund': kwargs['refund_fee'],
                    'total': kwargs['total_fee'], 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    buyer = {'Authorization': 'Bearer ' + token(users[1])}
    merchant = {'Authorization': 'Bearer ' + token(users[2])}
    application = {'refund_type': 'refund_only', 'reason_code': 'synthetic'}
    assert http.post('/refund/apply', json={**application, 'order_number': fresh_order},
                     headers=buyer).status_code == 200
    if provider_state == 'LEGACY':
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE orders SET refund_no=%s WHERE order_number=%s',
                            ('LEGACY' + uuid.uuid4().hex, paid_order))
            conn.commit()
    else:
        refund = http.post('/wechat-pay/refund', json={'order_number': paid_order, 'refund_fee': 40,
                           'refund_request_id': 'partial'}, headers=merchant)
        assert refund.status_code == 200
    calls_before_application = list(calls)
    assert http.post('/refund/apply', json={**application, 'order_number': paid_order},
                     headers=buyer).status_code == 409
    assert calls == calls_before_application
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM refunds WHERE order_number=%s', (paid_order,))
            assert cur.fetchone()['n'] == 0
            cur.execute('SELECT COUNT(*) n FROM refunds WHERE order_number=%s', (fresh_order,))
            assert cur.fetchone()['n'] == 1

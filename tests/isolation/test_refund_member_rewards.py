"""Real MySQL member reward refund accounting; controlled payment provider."""
from decimal import Decimal

import pytest
from test_payment_business import paid_order


@pytest.mark.parametrize('balance', [Decimal('0'), Decimal('1.250000'), Decimal('5.000000')])
def test_full_refund_persists_member_grant_recovery_and_shortfall(
        db, http, users, token, paid_order, monkeypatch, balance):
    from services import refund_service
    granted = Decimal('3.123456')
    recovered = min(balance, granted)
    shortfall = granted - recovered
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM orders WHERE order_number=%s', (paid_order,))
            order_id = cur.fetchone()['id']
            cur.execute('UPDATE orders SET pending_points=0 WHERE id=%s', (order_id,))
            cur.execute('UPDATE users SET member_points=%s WHERE id=%s', (balance, users[1]))
            cur.execute("INSERT INTO points_log(user_id,change_amount,balance_after,type,reason,related_order) VALUES(%s,%s,%s,'member','购买会员商品获得积分',%s)",
                        (users[1], granted, granted, order_id))
            grant_log_id = cur.lastrowid
        conn.commit()
    calls = []
    class Provider:
        def refund(self, **kwargs):
            calls.append(kwargs['out_refund_no'])
            return {'status': 'SUCCESS', 'amount': {'refund': 100, 'total': 100, 'currency': 'CNY'}}
    monkeypatch.setattr(refund_service, 'provider', Provider())
    headers = {'Authorization': 'Bearer ' + token(users[2])}
    payload = {'order_number': paid_order, 'refund_fee': 100, 'refund_request_id': 'reward'}
    result = http.post('/wechat-pay/refund', json=payload, headers=headers)
    assert result.status_code == 200
    replay = http.post('/wechat-pay/refund', json=payload, headers=headers)
    assert replay.status_code == 200
    refund_no = result.json()['refund_no']
    assert replay.json()['refund_no'] == refund_no
    assert calls == [refund_no]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM orders WHERE id=%s', (order_id,))
            assert cur.fetchone()['status'] == 'refunded'
            cur.execute('SELECT state FROM payment_refund_intents WHERE out_refund_no=%s', (refund_no,))
            assert cur.fetchone()['state'] == 'success'
            cur.execute('SELECT member_points FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone()['member_points'] == balance - recovered
            cur.execute('SELECT order_id,order_number,user_id,refund_no,granted_amount,recovered_amount,shortfall_amount,recovery_log_id FROM order_member_reward_refunds WHERE order_id=%s', (order_id,))
            audit = cur.fetchone()
            assert audit is not None
            assert {key: audit[key] for key in ('order_id', 'order_number', 'user_id', 'refund_no', 'granted_amount', 'recovered_amount', 'shortfall_amount')} == {
                'order_id': order_id, 'order_number': paid_order, 'user_id': users[1], 'refund_no': refund_no,
                'granted_amount': granted, 'recovered_amount': recovered, 'shortfall_amount': shortfall}
            cur.execute('SELECT change_amount FROM points_log WHERE id=%s', (grant_log_id,))
            assert cur.fetchone()['change_amount'] == granted
            cur.execute('SELECT id,change_amount,balance_after FROM points_log WHERE user_id=%s AND reason=%s',
                        (users[1], '订单退款收回奖励积分: ' + paid_order))
            logs = cur.fetchall()
            if recovered:
                assert logs == [{'id': audit['recovery_log_id'], 'change_amount': -recovered,
                                 'balance_after': balance - recovered}]
            else:
                assert not logs and audit['recovery_log_id'] is None
            cur.execute('SELECT case_key,reason FROM payment_reconciliation_cases WHERE order_number=%s', (paid_order,))
            expected = ([{'case_key': 'member-reward:' + str(order_id), 'reason': 'member_reward_shortfall'}]
                        if shortfall else [])
            assert list(cur.fetchall()) == expected

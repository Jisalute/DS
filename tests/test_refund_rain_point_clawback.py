"""退款资产回冲：雨点、推荐/团队奖励、会员积分与会员等级。

覆盖甲方要求：订单退款后，该订单产生的雨点必须从余额扣回，
包括推荐人奖励、团队奖励与 20% 各类分配（分配由 reverse_split_on_refund
的既有资金池回冲逻辑处理，本文件只验证用户资产侧）。
"""
from contextlib import contextmanager
from decimal import Decimal
import uuid

import pytest


@pytest.fixture
def refund_db(monkeypatch):
    from scripts.security_isolation.safety import guard
    guard()

    import pymysql
    from core.config import get_db_config
    from services import finance_service

    @contextmanager
    def connect():
        conn = pymysql.connect(**get_db_config(), cursorclass=pymysql.cursors.DictCursor)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(finance_service, 'get_conn', connect)
    state = {'users': [], 'orders': [], 'products': []}
    yield connect, state

    with connect() as conn:
        with conn.cursor() as cur:
            for order_no in state['orders']:
                cur.execute('DELETE FROM finance_events WHERE order_number=%s', (order_no,))
            for user_id in state['users']:
                cur.execute('DELETE FROM points_log WHERE user_id=%s', (user_id,))
                cur.execute('DELETE FROM account_flow WHERE related_user=%s', (user_id,))
                cur.execute('DELETE FROM coupons WHERE user_id=%s', (user_id,))
            for order_no in state['orders']:
                cur.execute(
                    'DELETE oi FROM order_items oi JOIN orders o ON o.id=oi.order_id '
                    'WHERE o.order_number=%s', (order_no,))
                cur.execute('DELETE FROM orders WHERE order_number=%s', (order_no,))
            for product_id in state['products']:
                cur.execute('DELETE FROM products WHERE id=%s', (product_id,))
            for user_id in state['users']:
                cur.execute('DELETE FROM users WHERE id=%s', (user_id,))
            conn.commit()


def _user(connect, state, **fields):
    columns = ['name'] + list(fields)
    values = ['u_' + uuid.uuid4().hex[:10]] + list(fields.values())
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users(%s) VALUES(%s)" % (','.join(columns), ','.join(['%s'] * len(columns))),
                tuple(values),
            )
            user_id = cur.lastrowid
        conn.commit()
    state['users'].append(user_id)
    return user_id


def _order(connect, state, order_number, user_id, total, status='refunding', pending_points=0):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO orders(order_number,user_id,merchant_id,total_amount,status,pending_points) '
                'VALUES(%s,%s,%s,%s,%s,%s)',
                (order_number, user_id, user_id, total, status, pending_points),
            )
            order_id = cur.lastrowid
        conn.commit()
    state['orders'].append(order_number)
    return order_id


def _flow(connect, account_type, user_id, amount, remark):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO account_flow(account_type,related_user,change_amount,balance_after,flow_type,remark) '
                "VALUES(%s,%s,%s,%s,'income',%s)",
                (account_type, user_id, amount, amount, remark),
            )
        conn.commit()


def _points_log(connect, user_id, amount, reason, order_id):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO points_log(user_id,change_amount,balance_after,type,reason,related_order) "
                "VALUES(%s,%s,%s,'member',%s,%s)",
                (user_id, amount, amount, reason, order_id),
            )
        conn.commit()


def _value(connect, user_id, field):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT %s AS value FROM users WHERE id=%%s' % field, (user_id,))
            row = cur.fetchone()
    return Decimal(str(row['value'] or 0))


def _refund(connect, order_number, event_key, amount):
    from services.finance_service import reverse_split_on_refund
    with connect() as conn:
        reverse_split_on_refund(
            order_number, external_conn=conn, event_key=event_key,
            refund_amount=Decimal(str(amount)),
        )
        conn.commit()


def _seed_rain_order(connect, state):
    """买家 100 雨点、推荐人 20、团队 10；买家另有 10 抵扣积分与 20 奖励积分。"""
    tag = uuid.uuid4().hex[:10]
    order_no = 'TST' + tag
    buyer = _user(connect, state, true_total_points=100, member_points=50)
    referrer = _user(connect, state, true_total_points=20, referral_points=20)
    team = _user(connect, state, true_total_points=10, team_reward_points=10)
    order_id = _order(connect, state, order_no, buyer, 100, pending_points=10)
    _flow(connect, 'true_total_points', buyer, 100, '购买商品赠送雨点 - 订单#' + order_no)
    _flow(connect, 'referral_points', referrer, 20, '普通商品推荐奖励 - 订单' + order_no)
    _flow(connect, 'team_reward_points', team, 10, '团队L1奖励（来自第1层）- 订单#%s' % order_id)
    _points_log(connect, buyer, 20, '购买普通商品获得积分', order_id)
    return order_no, buyer, referrer, team


def test_partial_refund_prorates_rain_points(refund_db):
    connect, state = refund_db
    order_no, buyer, referrer, team = _seed_rain_order(connect, state)

    _refund(connect, order_no, uuid.uuid4().hex, Decimal('25'))

    assert _value(connect, buyer, 'true_total_points') == Decimal('75.0000')
    assert _value(connect, referrer, 'referral_points') == Decimal('15.0000')
    assert _value(connect, referrer, 'true_total_points') == Decimal('15.0000')
    assert _value(connect, team, 'team_reward_points') == Decimal('7.5000')
    assert _value(connect, team, 'true_total_points') == Decimal('7.5000')


def test_partial_refund_prorates_member_points(refund_db):
    connect, state = refund_db
    order_no, buyer, _, _ = _seed_rain_order(connect, state)

    _refund(connect, order_no, uuid.uuid4().hex, Decimal('25'))

    # 退回 10 * 25% = 2.5，收回 20 * 25% = 5，净 -2.5
    assert _value(connect, buyer, 'member_points') == Decimal('47.5')


def test_replay_does_not_double_charge(refund_db):
    connect, state = refund_db
    order_no, buyer, referrer, _ = _seed_rain_order(connect, state)
    event_key = uuid.uuid4().hex

    _refund(connect, order_no, event_key, Decimal('25'))
    _refund(connect, order_no, event_key, Decimal('25'))

    assert _value(connect, buyer, 'true_total_points') == Decimal('75.0000')
    assert _value(connect, buyer, 'member_points') == Decimal('47.5')
    assert _value(connect, referrer, 'referral_points') == Decimal('15.0000')


def test_cumulative_refund_reaches_full_amount(refund_db):
    connect, state = refund_db
    order_no, buyer, referrer, team = _seed_rain_order(connect, state)

    _refund(connect, order_no, uuid.uuid4().hex, Decimal('25'))
    _refund(connect, order_no, uuid.uuid4().hex, Decimal('75'))

    assert _value(connect, buyer, 'true_total_points') == Decimal('0')
    assert _value(connect, referrer, 'referral_points') == Decimal('0')
    assert _value(connect, team, 'team_reward_points') == Decimal('0')
    # 积分：+10 退回，-20 收回，起始 50 → 40
    assert _value(connect, buyer, 'member_points') == Decimal('40')


def test_short_balance_voids_unused_coupon(refund_db):
    connect, state = refund_db
    order_no, buyer, _, _ = _seed_rain_order(connect, state)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET true_total_points=0 WHERE id=%s', (buyer,))
            cur.execute(
                "INSERT INTO coupons(user_id,coupon_type,amount,status,valid_from,valid_to) "
                "VALUES(%s,'user',30,'unused',CURDATE(),DATE_ADD(CURDATE(),INTERVAL 30 DAY))",
                (buyer,),
            )
            coupon_id = cur.lastrowid
        conn.commit()

    _refund(connect, order_no, uuid.uuid4().hex, Decimal('25'))

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM coupons WHERE id=%s', (coupon_id,))
            assert cur.fetchone()['status'] == 'expired'
            cur.execute(
                "SELECT change_amount FROM account_flow WHERE account_type='coupon' AND related_user=%s",
                (buyer,),
            )
            # 券只能整张作废：应扣 25，作废的券面额 30，流水记录实际作废额
            assert cur.fetchone()['change_amount'] == Decimal('-30.0000')


def test_other_order_is_not_touched(refund_db):
    connect, state = refund_db
    order_no, _, _, _ = _seed_rain_order(connect, state)
    other_no = 'TST' + uuid.uuid4().hex[:10]
    other_user = _user(connect, state, true_total_points=7)
    _order(connect, state, other_no, other_user, 100, status='pending_recv')
    _flow(connect, 'true_total_points', other_user, 7, '购买商品赠送雨点 - 订单#' + other_no)

    _refund(connect, order_no, uuid.uuid4().hex, Decimal('100'))

    assert _value(connect, other_user, 'true_total_points') == Decimal('7')


def test_full_refund_downgrades_member_level(refund_db):
    from services.finance_service import FinanceService

    connect, state = refund_db
    tag = uuid.uuid4().hex[:10]
    order_no = 'TST' + tag
    vip = _user(connect, state, true_total_points=0, member_points=30, member_level=1)
    order_id = _order(connect, state, order_no, vip, 100)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO products(name,is_member_product,status) VALUES(%s,1,1)',
                ('vip_' + tag,),
            )
            product_id = cur.lastrowid
            cur.execute(
                'INSERT INTO order_items(order_id,product_id,quantity,unit_price,total_price) '
                'VALUES(%s,%s,1,100,100)',
                (order_id, product_id),
            )
        conn.commit()
    state['products'].append(product_id)
    _points_log(connect, vip, 20, '购买会员商品获得积分', order_id)

    _refund(connect, order_no, uuid.uuid4().hex, Decimal('100'))
    assert _value(connect, vip, 'member_points') == Decimal('10')
    assert _value(connect, vip, 'member_level') == Decimal('1')

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status='refunded' WHERE order_number=%s", (order_no,))
        conn.commit()
    with connect() as conn:
        FinanceService.revoke_order_discounts(order_no, external_cur=conn.cursor())
        conn.commit()

    assert _value(connect, vip, 'member_level') == Decimal('0')
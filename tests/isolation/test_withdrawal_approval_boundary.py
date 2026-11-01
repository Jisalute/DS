from decimal import Decimal

import pytest


@pytest.mark.parametrize('approve', [True, False])
@pytest.mark.parametrize('initial_status', ['pending_auto', 'pending_manual', 'approved', 'rejected'])
def test_audit_unavailable_preserves_entire_request(db, http, users, token, monkeypatch, initial_status, approve):
    from core.config import settings
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[0]))
    headers = {'Authorization': 'Bearer ' + token(users[0])}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET promotion_balance=7,merchant_balance=11 WHERE id=%s', (users[1],))
            cur.execute('INSERT INTO withdrawals(user_id,amount,tax_amount,actual_amount,status) VALUES(%s,10,1,9,%s)',
                        (users[1], initial_status))
            withdrawal_id = cur.lastrowid
            cur.execute('SELECT * FROM withdrawals WHERE id=%s', (withdrawal_id,))
            before = cur.fetchone()
            cur.execute('SELECT COUNT(*) n FROM account_flow WHERE related_user=%s', (users[1],))
            flows = cur.fetchone()['n']
        conn.commit()
    for _ in range(2):
        response = http.patch('/api/withdrawals/audit', json={'withdrawal_id': withdrawal_id, 'approve': approve}, headers=headers)
        assert response.status_code == 503, response.text
        # Production's global 5xx handler intentionally does not expose internal detail.
        assert response.json() == {'detail': '服务器内部错误，请稍后重试'}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM withdrawals WHERE id=%s', (withdrawal_id,))
            assert cur.fetchone() == before
            cur.execute('SELECT promotion_balance,merchant_balance FROM users WHERE id=%s', (users[1],))
            assert cur.fetchone() == {'promotion_balance': Decimal('7'), 'merchant_balance': Decimal('11')}
            cur.execute('SELECT COUNT(*) n FROM account_flow WHERE related_user=%s', (users[1],))
            assert cur.fetchone()['n'] == flows


@pytest.mark.parametrize('approve,reason', [(True, '付款服务尚未接入'), (False, '扣款来源无法核验')])
def test_service_audit_never_borrows_connection(monkeypatch, approve, reason):
    from core.exceptions import PayoutUnavailableError
    from services import finance_service
    def forbidden():
        raise AssertionError('unverified audit must not open a database transaction')
    monkeypatch.setattr(finance_service, 'get_conn', forbidden)
    with pytest.raises(PayoutUnavailableError, match=reason):
        finance_service.FinanceService().audit_withdrawal(1, approve, 'trusted-admin')


@pytest.mark.parametrize('approve', [True, False])
def test_audit_requires_platform_admin(http, users, token, monkeypatch, approve):
    from core.config import settings
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[0]))
    body = {'withdrawal_id': 1, 'approve': approve}
    assert http.patch('/api/withdrawals/audit', json=body).status_code == 401
    response = http.patch('/api/withdrawals/audit', json=body,
                          headers={'Authorization': 'Bearer ' + token(users[1])})
    assert response.status_code == 403


def test_audit_validates_request_before_unavailable(http, users, token, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[0]))
    response = http.patch('/api/withdrawals/audit', json={'withdrawal_id': 0, 'approve': False},
                          headers={'Authorization': 'Bearer ' + token(users[0])})
    assert response.status_code == 422

@pytest.mark.parametrize('amount,status', [(10, 503), (-10, 422), (0, 422)])
def test_legacy_merchant_withdrawal_never_changes_balances(db, http, users, token, monkeypatch, amount, status):
    from core.config import settings
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[0]))
    headers = {'Authorization': 'Bearer ' + token(users[0])}
    def balances():
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id,promotion_balance,merchant_balance FROM users ORDER BY id')
                return cur.fetchall()
    before = balances()
    response = http.post('/merchant/withdraw', json={'amount': amount}, headers=headers)
    assert response.status_code == status, response.text
    if status == 503:
        assert response.json() == {'detail': '服务器内部错误，请稍后重试'}
    assert balances() == before


def test_legacy_merchant_withdrawal_authorization(http, users, token, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, 'ADMIN_USER_IDS', str(users[0]))
    assert http.post('/merchant/withdraw', json={'amount': 10}).status_code == 401
    for user_id in (users[1], users[2]):
        response = http.post('/merchant/withdraw', json={'amount': 10},
                             headers={'Authorization': 'Bearer ' + token(user_id)})
        assert response.status_code == 403


@pytest.mark.parametrize('amount', [Decimal('10'), Decimal('-10'), Decimal('0')])
def test_legacy_direct_withdrawal_never_borrows_connection(monkeypatch, amount):
    from core.exceptions import PayoutUnavailableError
    from services import finance_service
    def forbidden():
        raise AssertionError('legacy withdrawal must not open a transaction')
    monkeypatch.setattr(finance_service, 'get_conn', forbidden)
    with pytest.raises(PayoutUnavailableError):
        finance_service.withdraw(amount)

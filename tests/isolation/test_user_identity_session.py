"""Exercise the identity endpoint against isolated, session-backed accounts."""
from core.config import settings


def test_identity_is_always_the_authenticated_session(http, users, token, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", str(users[0]))
    assert http.get('/user/me/identity').status_code == 401
    for user_id, other_id in ((users[0], users[1]), (users[1], users[0])):
        headers = {'Authorization': 'Bearer ' + token(user_id, 'jwt')}
        response = http.get('/user/me/identity', headers=headers,
                            params={'user_id': other_id, 'mobile': '19900000000'})
        assert response.status_code == 200
        assert response.json() == {'user_id': str(user_id)}

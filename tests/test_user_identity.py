"""HTTP identity contract using the real authentication dependency and synthetic sessions."""
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.user import routes
from core import auth


@pytest.fixture
def identity_client(monkeypatch):
    sessions = {
        "a" * 124: {"id": 42, "status": 0, "mobile": "private", "openid": "secret"},
        "b" * 124: {"id": 73, "status": 0, "mobile": "admin", "openid": "secret"},
        "c" * 124: {"id": 9007199254740993, "status": 0},
        "d" * 124: {"id": 99, "status": 1},
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, args):
            self.user = sessions.get(args[0])

        def fetchone(self):
            return self.user

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(auth, "get_conn", connection)
    monkeypatch.setattr(auth.settings, "ADMIN_USER_IDS", "73")
    app = FastAPI()
    routes.register_routes(app)
    with TestClient(app) as client:
        yield client


def test_identity_requires_a_valid_session(identity_client):
    missing = identity_client.get("/user/me/identity")
    assert missing.status_code == 401
    assert missing.json() == {"detail": "缺少认证令牌"}
    assert missing.headers["www-authenticate"] == "Bearer"
    invalid = identity_client.get("/user/me/identity", headers={"Authorization": "Bearer " + "z" * 124})
    assert invalid.status_code == 401
    assert invalid.json() == {"detail": "认证令牌无效或已过期"}
    frozen = identity_client.get("/user/me/identity", headers={"Authorization": "Bearer " + "d" * 124})
    assert frozen.status_code == 403


@pytest.mark.parametrize(("token", "expected"), [("a", "42"), ("b", "73"), ("c", "9007199254740993")])
def test_identity_always_comes_from_session(identity_client, token, expected):
    headers = {"Authorization": "Bearer " + token * 124}
    response = identity_client.get("/user/me/identity", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"user_id": expected}
    # Unknown selectors must not let even an administrator switch identity.
    selected = identity_client.get("/user/me/identity?user_id=123&mobile=other", headers=headers)
    assert selected.status_code == 200
    assert selected.json() == {"user_id": expected}



def test_identity_auth_storage_failure_fails_closed(identity_client, monkeypatch):
    def unavailable():
        raise RuntimeError("synthetic database outage")

    monkeypatch.setattr(auth, "get_conn", unavailable)
    response = identity_client.get(
        "/user/me/identity", headers={"Authorization": "Bearer " + "a" * 124}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "认证服务暂不可用"}
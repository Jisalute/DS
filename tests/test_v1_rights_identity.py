"""Authenticated account identity in the rights overview."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1 import rights


def test_rights_identity_uses_authenticated_user_not_client_or_service(monkeypatch):
    app = FastAPI()
    app.include_router(rights.router, prefix="/api/v1")
    calls = []

    def overview(_service, user_id):
        calls.append(user_id)
        return {
            "user_id": "forged-service-id",
            "member_level": 0,
            "capabilities": {"can_list_wine": False},
            "points": {"balance": "0.000000"},
            "rain": {"balance": "0.000000"},
            "coupons": {"balance": "0.000000"},
            "eggs": {"quantity": "0.000000"},
            "goda_coupons": {"balance": "0.000000"},
            "rule_version": "v1.1-p1",
            "disclaimer": "test",
        }

    monkeypatch.setattr(rights.RightsService, "get_my_rights", overview)
    with TestClient(app) as client:
        for user_id in (9007199254740992, 9007199254740993):
            app.dependency_overrides[rights.get_current_user] = lambda: {"id": user_id}
            response = client.get("/api/v1/rights/me?user_id=1")
            assert response.status_code == 200
            assert response.json()["data"]["user_id"] == str(user_id)
    assert calls == [9007199254740992, 9007199254740993]

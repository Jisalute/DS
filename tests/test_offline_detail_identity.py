from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.offline import routes
from services import offline_service


@pytest.fixture
def client():
    app = FastAPI()
    routes.register_offline_routes(app)
    app.dependency_overrides[routes.get_current_user] = lambda: {"id": 7}
    with TestClient(app) as http:
        http.headers["Authorization"] = "Bearer synthetic-token"
        yield http, app


@pytest.mark.parametrize("query", ["", "?user_id=7"])
def test_detail_uses_authenticated_identity(client, monkeypatch, query):
    http, _ = client
    calls = []
    detail = {"order_no": "OFF-synthetic", "amount": 1000, "merchant_id": 99,
              "store_name": "Synthetic store", "product_name": "", "status": 1,
              "coupons": []}

    async def lookup(order_no, user_id):
        calls.append((order_no, user_id))
        return detail

    monkeypatch.setattr(routes.OfflineService, "get_order_detail", lookup)
    response = http.get("/api/offline/dingdan/xiangqing/OFF-synthetic" + query)
    assert response.status_code == 200
    assert response.json() == {"code": 0, "message": "查询成功", "data": detail}
    assert calls == [("OFF-synthetic", 7)]


@pytest.mark.parametrize("actor", [{"id": 7}, {"id": 7, "is_admin": True}])
@pytest.mark.parametrize("target", [8, 0, -1, 9007199254740993])
def test_detail_rejects_other_identity_before_service(client, monkeypatch, actor, target):
    http, app = client
    app.dependency_overrides[routes.get_current_user] = lambda: actor

    async def forbidden_lookup(**kwargs):
        pytest.fail("cross-user request reached the service")

    monkeypatch.setattr(routes.OfflineService, "get_order_detail", forbidden_lookup)
    response = http.get(f"/api/offline/dingdan/xiangqing/OFF-synthetic?user_id={target}")
    assert response.status_code == 403
    assert response.json() == {"detail": "只能查询本人优惠券"}


@pytest.mark.parametrize("query", ["?user_id=not-an-id", "?user_id="])
def test_detail_rejects_invalid_identity(client, monkeypatch, query):
    http, _ = client

    async def forbidden_lookup(**kwargs):
        pytest.fail("invalid request reached the service")

    monkeypatch.setattr(routes.OfflineService, "get_order_detail", forbidden_lookup)
    assert http.get("/api/offline/dingdan/xiangqing/OFF-synthetic" + query).status_code == 422


@pytest.mark.parametrize("status", [403, 404, 503])
def test_detail_preserves_http_exception(client, monkeypatch, status):
    http, _ = client

    async def rejected_lookup(**kwargs):
        raise HTTPException(status_code=status, detail="synthetic rejection")

    monkeypatch.setattr(routes.OfflineService, "get_order_detail", rejected_lookup)
    response = http.get("/api/offline/dingdan/xiangqing/OFF-synthetic?user_id=7")
    assert response.status_code == status
    assert response.json() == {"detail": "synthetic rejection"}


def test_detail_preserves_missing_order_response(client, monkeypatch):
    http, _ = client

    async def missing_lookup(**kwargs):
        raise ValueError("订单不存在")

    monkeypatch.setattr(routes.OfflineService, "get_order_detail", missing_lookup)
    response = http.get("/api/offline/dingdan/xiangqing/OFF-missing")
    assert response.status_code == 400
    assert response.json() == {"detail": "订单不存在"}


@pytest.mark.parametrize("query", ["", "?user_id=7"])
def test_buyer_scan_reads_order_but_only_own_coupons(client, monkeypatch, query):
    http, _ = client
    statements = []
    coupon_calls = []
    order = {"order_no": "OFF-synthetic", "amount": 1000, "merchant_id": 99,
             "store_name": "Synthetic store", "product_name": "", "status": 1}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            statements.append((sql, params))
            self.params = params

        def fetchone(self):
            return None if len(self.params) == 2 else dict(order)

    class Connection:
        def cursor(self, *args):
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    def coupons(_service, user_id, amount):
        coupon_calls.append((user_id, amount))
        return [{"id": 71, "amount": 1}]

    monkeypatch.setattr(offline_service, "get_conn", connection)
    monkeypatch.setattr(offline_service.FinanceService, "list_available", coupons)
    response = http.get("/api/offline/dingdan/xiangqing/OFF-synthetic" + query)
    assert response.status_code == 200
    assert response.json()["data"] == {**order, "coupons": [{"id": 71, "amount": 1.0}]}
    assert [params for _, params in statements] == [("OFF-synthetic", "7"), ("OFF-synthetic",)]
    assert coupon_calls == [(7, 1000)]


def test_detail_authentication_rejection_never_queries_service(client, monkeypatch):
    http, app = client

    async def unauthenticated():
        raise HTTPException(status_code=401, detail="缺少认证令牌")

    async def forbidden_lookup(**kwargs):
        pytest.fail("unauthenticated request reached the service")

    app.dependency_overrides[routes.get_current_user] = unauthenticated
    monkeypatch.setattr(routes.OfflineService, "get_order_detail", forbidden_lookup)
    response = http.get("/api/offline/dingdan/xiangqing/OFF-synthetic?user_id=7")
    assert response.status_code == 401

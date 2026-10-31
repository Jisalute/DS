"""Rights writers must participate in the bridge cutover barrier (real MySQL)."""
from contextlib import contextmanager

import pymysql
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1 import register_v1_routes
from api.v1.rights import get_current_user
import pytest

from services import legacy_assets_bridge as bridge
from services import rights_service
from services.rights_service import RightsError, RightsService
from test_v1_rights_mysql import (
    BRIDGE_MIGRATION, _migration_statements, scratch_db, store,
)


def _restore_triggers(store):
    with store.connect() as conn:
        with conn.cursor() as cur:
            for statement in _migration_statements(BRIDGE_MIGRATION):
                cur.execute(statement)
            signature = bridge.trigger_signature(cur)
            cur.execute("UPDATE legacy_bridge_completion SET completed = TRUE, trigger_signature = %s WHERE id = 1", (signature,))
        conn.commit()


def _attempt(action):
    service = RightsService()
    if action == "overview":
        return service.get_my_rights(1)
    if action == "exchange":
        return service.exchange_eggs(1, "1", idempotency_key="readiness")
    if action == "conversion":
        return service.maybe_auto_convert(1)
    if action == "wine":
        return service.redeem_wine_with_goda_coupons(1, 1)
    return service.sweep_global_auto_conversions(batch_size=1, max_batches=1)


@pytest.mark.parametrize("action", ["overview", "exchange", "conversion", "wine", "sweep"])
@pytest.mark.parametrize("unready", ["missing_009", "missing_completion", "incomplete", "missing_trigger", "replaced_trigger"])
def test_unready_bridge_blocks_rights_before_pool_lock(store, monkeypatch, action, unready):
    store.exec("INSERT INTO users (id) VALUES (1)")
    store.exec("UPDATE user_assets SET egg_quantity = 100, avg_cost_goda_value = 0.001 WHERE user_id = 1")
    store.exec("UPDATE system_pools SET platform_egg_balance = platform_egg_balance - 100 WHERE id = 1")
    before_assets = store.one("SELECT * FROM user_assets WHERE user_id = 1")
    before_pool = store.one("SELECT * FROM system_pools WHERE id = 1")
    before_cursor = store.one("SELECT * FROM rights_conversion_sweep_state WHERE id = 1")
    if unready == "missing_009":
        store.exec("RENAME TABLE legacy_bridge_completion TO readiness_test_saved_completion")
    elif unready == "missing_completion":
        store.exec("DELETE FROM legacy_bridge_completion WHERE id = 1")
    elif unready == "incomplete":
        store.exec("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")
    else:
        store.exec("DROP TRIGGER trg_users_assets_mirror_update")
        if unready == "replaced_trigger":
            store.exec("CREATE TRIGGER trg_users_assets_mirror_update AFTER UPDATE ON users FOR EACH ROW SET @readiness_replaced_trigger = 1")

    def unexpected_pool_lock(*args):
        pytest.fail("bridge readiness must be checked before acquiring the pool lock")

    monkeypatch.setattr(RightsService, "lock_pools", unexpected_pool_lock)
    try:
        with pytest.raises(RightsError) as caught:
            _attempt(action)
        assert caught.value.http_status == 503
        assert caught.value.code == "STATE_CONFLICT"
        assert store.one("SELECT * FROM user_assets WHERE user_id = 1") == before_assets
        assert store.one("SELECT * FROM system_pools WHERE id = 1") == before_pool
        assert store.one("SELECT * FROM rights_conversion_sweep_state WHERE id = 1") == before_cursor
        assert store.all("SELECT * FROM rights_operations") == []
        assert store.all("SELECT * FROM rights_ledger") == []
    finally:
        if unready == "missing_009":
            store.exec("RENAME TABLE readiness_test_saved_completion TO legacy_bridge_completion")
        if unready == "missing_completion":
            with store.connect() as conn:
                with conn.cursor() as cur:
                    signature = bridge.trigger_signature(cur)
                    cur.execute("INSERT INTO legacy_bridge_completion (id, completed, trigger_signature) VALUES (1, TRUE, %s)", (signature,))
                conn.commit()
        if unready in ("missing_trigger", "replaced_trigger"):
            _restore_triggers(store)


def test_trigger_reinstallation_requires_new_completion_signature(store):
    store.exec("INSERT INTO users (id) VALUES (1)")
    store.exec("DROP TRIGGER trg_users_assets_mirror_update")
    # Reinstall a modified definition: correct names alone must not grant readiness.
    store.exec("CREATE TRIGGER trg_users_assets_mirror_update AFTER UPDATE ON users FOR EACH ROW SET @readiness_replaced_trigger = 1")
    try:
        with pytest.raises(RightsError) as caught:
            RightsService().maybe_auto_convert(1)
        assert caught.value.http_status == 503
    finally:
        _restore_triggers(store)
    assert RightsService().maybe_auto_convert(1) is None


def test_cutover_clear_waits_for_inflight_rights_transaction(store, monkeypatch):
    store.exec("INSERT INTO users (id) VALUES (1)")
    store.exec("UPDATE user_assets SET egg_quantity = 100, avg_cost_goda_value = 0.001 WHERE user_id = 1")
    store.exec("UPDATE system_pools SET platform_egg_balance = platform_egg_balance - 100 WHERE id = 1")
    entered = threading.Event()
    release = threading.Event()
    clearing = threading.Event()
    cleared = threading.Event()
    original = RightsService.lock_pools

    def hold_pool(cur):
        # The real readiness SELECT ... LOCK IN SHARE MODE has already executed.
        entered.set()
        assert release.wait(10)
        return original(cur)

    monkeypatch.setattr(RightsService, "lock_pools", staticmethod(hold_pool))

    def clear_completion():
        clearing.set()
        store.exec("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")
        cleared.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(RightsService().maybe_auto_convert, 1)
        assert entered.wait(10)
        migration = executor.submit(clear_completion)
        try:
            assert clearing.wait(10)
            assert not cleared.wait(0.2), "completion clear must wait for the in-flight shared lock"
        finally:
            release.set()
        assert writer.result(timeout=15) is not None
        migration.result(timeout=15)
    assert cleared.is_set()
    with pytest.raises(RightsError) as caught:
        RightsService().maybe_auto_convert(1)
    assert caught.value.http_status == 503


def test_cutover_during_scan_prevents_checkpoint_advance(store, monkeypatch):
    store.exec("INSERT INTO users (id) VALUES (1)")
    store.exec("UPDATE user_assets SET egg_quantity = 100, avg_cost_goda_value = 0.001 WHERE user_id = 1")
    store.exec("UPDATE system_pools SET platform_egg_balance = platform_egg_balance - 100 WHERE id = 1")
    original = RightsService.maybe_auto_convert

    def convert_then_cutover(self, user_id):
        result = original(self, user_id)
        store.exec("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")
        return result

    monkeypatch.setattr(RightsService, "maybe_auto_convert", convert_then_cutover)
    with pytest.raises(RightsError) as caught:
        RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)
    assert caught.value.http_status == 503
    state = store.one("SELECT * FROM rights_conversion_sweep_state WHERE id = 1")
    assert state["last_user_id"] == 0
    assert state["upper_user_id"] == 1
    assert state["revision"] == 1  # Only cycle initialization committed before cutover.
    assert store.one("SELECT egg_quantity FROM user_assets WHERE user_id = 1")["egg_quantity"] == 0
    monkeypatch.setattr(RightsService, "maybe_auto_convert", original)
    store.exec("UPDATE legacy_bridge_completion SET completed = TRUE WHERE id = 1")
    assert RightsService().sweep_global_auto_conversions()["cycle_complete"] is True
    assert len(store.all("SELECT * FROM rights_operations WHERE operation_type = 'egg_auto_conversion'")) == 1


@pytest.mark.parametrize("cut_revision, expected_queries, expected_cursor", [(1, 0, 0), (2, 1, 1)])
def test_cutover_after_initialization_or_between_batches_blocks_next_query(
    store, monkeypatch, cut_revision, expected_queries, expected_cursor,
):
    for user_id in (1, 2):
        store.exec("INSERT INTO users (id) VALUES (%s)", (user_id,))
        store.exec(
            "UPDATE user_assets SET egg_quantity = 100, avg_cost_goda_value = 0.5 WHERE user_id = %s",
            (user_id,),
        )
    store.exec("UPDATE system_pools SET platform_egg_balance = platform_egg_balance - 200 WHERE id = 1")
    queries = []
    attempted = []
    original_execute = pymysql.cursors.DictCursor.execute
    original_convert = RightsService.maybe_auto_convert
    cut = False

    def execute(cur, query, args=None):
        if "SELECT user_id FROM user_assets" in query and "egg_quantity > 0" in query:
            queries.append(args)
        return original_execute(cur, query, args)

    @contextmanager
    def connect():
        nonlocal cut
        with store.connect() as conn:
            yield conn
        # Run the real cutover UPDATE after initialization/checkpoint committed,
        # before the scanner can open its next batch transaction.
        state = store.one("SELECT revision FROM rights_conversion_sweep_state WHERE id = 1")
        if not cut and state["revision"] == cut_revision:
            cut = True
            store.exec("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")

    def convert(self, user_id):
        attempted.append(user_id)
        return original_convert(self, user_id)

    monkeypatch.setattr(pymysql.cursors.DictCursor, "execute", execute)
    monkeypatch.setattr(rights_service, "get_conn", connect)
    monkeypatch.setattr(RightsService, "maybe_auto_convert", convert)
    with pytest.raises(RightsError) as caught:
        RightsService().sweep_global_auto_conversions(batch_size=1)
    assert caught.value.http_status == 503
    assert caught.value.code == "STATE_CONFLICT"
    assert cut is True
    assert len(queries) == expected_queries
    assert attempted == ([] if expected_queries == 0 else [1])
    state = store.one("SELECT * FROM rights_conversion_sweep_state WHERE id = 1")
    assert state["revision"] == cut_revision
    assert state["last_user_id"] == expected_cursor
    assert state["upper_user_id"] == 2
    assert store.all("SELECT * FROM rights_operations") == []
    assert store.all("SELECT * FROM rights_ledger") == []


def test_overview_http_fails_closed_without_hiding_real_zero_balances(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 50)")
    app = FastAPI()
    register_v1_routes(app)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1}
    with TestClient(app) as client:
        store.exec("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")
        response = client.get("/api/v1/rights/me")
        assert response.status_code == 503
        assert response.json()["code"] == "STATE_CONFLICT"
        assert response.json()["data"] is None

        store.exec("UPDATE legacy_bridge_completion SET completed = TRUE WHERE id = 1")
        response = client.get("/api/v1/rights/me")
        assert response.status_code == 200
        assert response.json()["data"]["points"]["balance"] == "100.000000"
        assert response.json()["data"]["rain"]["balance"] == "50.000000"

        store.exec("DELETE FROM user_assets WHERE user_id = 1")
        response = client.get("/api/v1/rights/me")
        assert response.status_code == 503
        assert response.json()["code"] == "STATE_CONFLICT"
        assert response.json()["data"] is None

        store.exec("UPDATE users SET member_points = 0, true_total_points = 0 WHERE id = 1")
        store.exec("INSERT INTO user_assets (user_id) VALUES (1)")
        response = client.get("/api/v1/rights/me")
        assert response.status_code == 200
        assert response.json()["data"]["points"]["balance"] == "0.000000"
        assert response.json()["data"]["rain"]["balance"] == "0.000000"

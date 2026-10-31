"""Durable global conversion scanning against the isolated rights database."""
import ast
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.rights_service import RightsError, RightsService
from test_v1_rights_mysql import scratch_db, store  # noqa: F401 - shared isolated fixtures


def _holders(store, ids=(1, 2, 3), cost="0.5"):
    for user_id in ids:
        store.exec("INSERT INTO users (id) VALUES (%s)", (user_id,))
        store.exec(
            "UPDATE user_assets SET egg_quantity = 100, avg_cost_goda_value = %s WHERE user_id = %s",
            (cost, user_id),
        )
        store.exec("UPDATE system_pools SET platform_egg_balance = platform_egg_balance - 100 WHERE id = 1")


def _state(store):
    return store.one("SELECT * FROM rights_conversion_sweep_state WHERE id = 1")


def test_page_budget_resumes_after_service_restart(store):
    _holders(store)
    first = RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)
    assert first["status"] == "partial"
    assert first["last_user_id"] == 1
    assert _state(store)["upper_user_id"] == 3
    second = RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)
    assert second["last_user_id"] == 2
    third = RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)
    assert third["cycle_complete"] is True
    assert _state(store)["last_user_id"] == 0
    assert _state(store)["upper_user_id"] == 0
    assert RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)["last_user_id"] == 1


@pytest.mark.parametrize("error", [RightsError("PRIVATE_SQL", 409, "private-sql-parameter-13800138000"), RuntimeError("private-sql-parameter-13800138000")])
def test_failed_holder_retried_next_cycle_without_version_change(store, monkeypatch, caplog, error):
    _holders(store, ids=(1,), cost="0.001")
    original = RightsService.maybe_auto_convert
    failed = False

    def convert(self, user_id):
        nonlocal failed
        if user_id == 1 and not failed:
            failed = True
            raise error
        return original(self, user_id)
    monkeypatch.setattr(RightsService, "maybe_auto_convert", convert)
    first = RightsService().sweep_global_auto_conversions(batch_size=1)
    assert first["cycle_complete"] is True
    assert first["status"] == "partial"
    assert first["failures"][0]["user_id"] == 1
    assert first["failures"] == [{"user_id": 1, "code": "SWEEP_FAILED", "message": type(error).__name__}]
    assert "private-sql-parameter-13800138000" not in caplog.text
    assert "PRIVATE_SQL" not in caplog.text
    version = store.one("SELECT valuation_version FROM system_pools WHERE id = 1")["valuation_version"]
    second = RightsService().sweep_global_auto_conversions(batch_size=1)
    assert second["users_converted"] == 1
    assert store.one("SELECT egg_quantity FROM user_assets WHERE user_id = 1")["egg_quantity"] == 0
    assert version < store.one("SELECT valuation_version FROM system_pools WHERE id = 1")["valuation_version"]


def test_valuation_change_revisits_earlier_holder_next_cycle(store, monkeypatch):
    _holders(store, ids=(1, 2), cost="0.01")
    original = RightsService.maybe_auto_convert

    def convert(self, user_id):
        if user_id == 2:
            store.exec("UPDATE system_pools SET pool_coupon_balance = 3000000, valuation_version = valuation_version + 1 WHERE id = 1")
        return original(self, user_id)

    monkeypatch.setattr(RightsService, "maybe_auto_convert", convert)
    first = RightsService().sweep_global_auto_conversions(batch_size=1)
    assert first["users_converted"] == 1
    assert store.one("SELECT egg_quantity FROM user_assets WHERE user_id = 1")["egg_quantity"] == 100
    second = RightsService().sweep_global_auto_conversions(batch_size=1)
    assert second["users_converted"] == 1


def test_crash_after_conversion_before_checkpoint_replays_safely(store, monkeypatch):
    _holders(store, ids=(1, 2), cost="0.001")
    original = RightsService.maybe_auto_convert

    def crash(self, user_id):
        result = original(self, user_id)
        if user_id == 1:
            raise KeyboardInterrupt("simulated process termination")
        return result

    monkeypatch.setattr(RightsService, "maybe_auto_convert", crash)
    with pytest.raises(KeyboardInterrupt):
        RightsService().sweep_global_auto_conversions(batch_size=1)
    assert _state(store)["last_user_id"] == 0
    assert _state(store)["upper_user_id"] == 2
    monkeypatch.setattr(RightsService, "maybe_auto_convert", original)
    assert RightsService().sweep_global_auto_conversions(batch_size=1)["users_converted"] == 1
    operations = store.all("SELECT user_id FROM rights_operations WHERE operation_type = 'egg_auto_conversion'")
    assert sorted(row["user_id"] for row in operations) == [1, 2]


def test_concurrent_scanners_checkpoint_with_compare_and_swap(store, monkeypatch):
    _holders(store, cost="0.001")
    original = RightsService.maybe_auto_convert
    barrier = threading.Barrier(2)

    def convert(self, user_id):
        if user_id == 1:
            barrier.wait(timeout=15)
        return original(self, user_id)

    monkeypatch.setattr(RightsService, "maybe_auto_convert", convert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(RightsService().sweep_global_auto_conversions, 1, 1) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(result["cursor_conflict"] for result in results) == 1
    assert _state(store)["last_user_id"] == 1
    assert sum(result["users_converted"] for result in results) == 1
    monkeypatch.setattr(RightsService, "maybe_auto_convert", original)
    assert RightsService().sweep_global_auto_conversions(batch_size=1)["users_converted"] == 2


def test_new_holders_do_not_extend_current_cycle(store):
    _holders(store, ids=(1, 2))
    RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)
    _holders(store, ids=(3,))
    assert RightsService().sweep_global_auto_conversions(batch_size=1, max_batches=1)["cycle_complete"] is True
    next_cycle = RightsService().sweep_global_auto_conversions(batch_size=1)
    assert next_cycle["users_scanned"] == 3


def test_empty_scan_and_later_first_holder(store):
    empty = RightsService().sweep_global_auto_conversions()
    assert empty["cycle_complete"] is True
    assert empty["users_scanned"] == 0
    _holders(store, ids=(1,), cost="0.001")
    assert RightsService().sweep_global_auto_conversions()["users_converted"] == 1


def test_pool_shortfall_is_retried_after_replenishment(store):
    _holders(store, ids=(1,), cost="0.001")
    store.exec("UPDATE user_assets SET egg_quantity = 99999950 WHERE user_id = 1")
    store.exec("UPDATE system_pools SET pool_coupon_balance = 1, platform_egg_balance = 50 WHERE id = 1")
    first = RightsService().sweep_global_auto_conversions()
    assert first["users_deferred"] == 1
    store.exec("UPDATE user_assets SET egg_quantity = 100 WHERE user_id = 1")
    store.exec("UPDATE system_pools SET pool_coupon_balance = 1000000, platform_egg_balance = 99999900, valuation_version = valuation_version + 1 WHERE id = 1")
    assert RightsService().sweep_global_auto_conversions()["users_converted"] == 1


def test_missing_sweep_state_fails_closed(store):
    store.exec("DELETE FROM rights_conversion_sweep_state WHERE id = 1")
    try:
        with pytest.raises(RightsError, match="转券扫描状态未初始化"):
            RightsService().sweep_global_auto_conversions()
    finally:
        store.exec("INSERT INTO rights_conversion_sweep_state (id) VALUES (1)")


def test_scheduler_invokes_scanner_on_every_tick(monkeypatch, caplog):
    # Execute the real method without importing unrelated provider/startup dependencies.
    path = Path(__file__).resolve().parent.parent / "core" / "scheduler.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TaskScheduler")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "sweep_v1_auto_conversions")
    namespace = {"logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    calls = []

    def sweep(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("private-sql-parameter-13800138000")
        return {"users_converted": 0, "failures": []}

    monkeypatch.setattr(RightsService, "sweep_global_auto_conversions", sweep)
    receiver = SimpleNamespace(_v1_swept_version=1)
    for _ in range(3):
        namespace["sweep_v1_auto_conversions"](receiver)
    assert len(calls) == 3
    assert "private-sql-parameter-13800138000" not in caplog.text
    assert "RuntimeError" in caplog.text

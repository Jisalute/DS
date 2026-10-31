"""旧账本桥接（users → user_assets）与历史迁移脚本的 MySQL 集成测试。

覆盖漏洞 1 & 2：
* 旧代码任何形式改动积分/雨点（增量、绝对赋值、无关字段更新）都能正确镜像；
* 迁移脚本把历史余额补齐，且**幂等**（第二次执行 0 写入）；
* 迁移前后对账必须完全相等，差额会被显式报出；
* 桥接触发器缺失时对账要显式失败，不能"看起来正常"。

MySQL 不可用时整组 skip，不污染 .env 指向的业务库。

复用 `tests/test_v1_rights_mysql.py` 的临时库句柄，避免两套建库逻辑分叉。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pymysql
import pytest

from services import legacy_assets_bridge as bridge
from test_v1_rights_mysql import ScratchDatabase, _migration_statements, _mysql_kwargs

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations" / "007_v1_rights.sql"
BRIDGE_MIGRATION = ROOT / "migrations" / "008_legacy_assets_bridge.sql"
COMPLETION_MIGRATION = ROOT / "migrations" / "009_legacy_bridge_completion.sql"

sys.path.insert(0, str(ROOT / "scripts"))
import migrate_legacy_assets  # noqa: E402


class LegacyDatabase(ScratchDatabase):
    """临时库句柄：reset 只清本套测试用到的表（旧账本 + 权益表）。"""

    def reset(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                for table in ("user_assets", "users", "system_pools"):
                    cur.execute(f"TRUNCATE TABLE {table}")
                cur.execute("UPDATE legacy_bridge_completion SET completed = FALSE, trigger_signature = NULL, completed_at = NULL WHERE id = 1")
                cur.execute(
                    """INSERT INTO system_pools
                           (id, total_egg_supply, pool_coupon_balance, subsidy_coupon_balance,
                            platform_egg_balance, valuation_version)
                       VALUES (1, 100000000, 1000000, 0, 100000000, 1)"""
                )
            conn.commit()


@pytest.fixture(scope="module")
def legacy_db():
    try:
        admin = pymysql.connect(**_mysql_kwargs())
    except Exception as exc:  # pragma: no cover - 环境相关
        pytest.skip(f"MySQL 不可用，跳过旧账本桥接测试: {exc}")

    import uuid

    name = f"ds_legacy_bridge_{uuid.uuid4().hex[:10]}"
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE `{name}` DEFAULT CHARACTER SET utf8mb4")
    admin.close()

    conn = pymysql.connect(**_mysql_kwargs(name))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE users (
                       id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
                       member_level TINYINT NOT NULL DEFAULT 0,
                       member_points DECIMAL(12,6) NOT NULL DEFAULT 0,
                       true_total_points DECIMAL(12,6) NOT NULL DEFAULT 0
                   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            for stmt in _migration_statements(MIGRATION):
                cur.execute(stmt)
            for stmt in _migration_statements(BRIDGE_MIGRATION):
                cur.execute(stmt)
            for stmt in _migration_statements(COMPLETION_MIGRATION):
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()

    db = LegacyDatabase(name)
    yield db
    admin = pymysql.connect(**_mysql_kwargs())
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
    admin.close()


@pytest.fixture
def store(legacy_db, monkeypatch):
    legacy_db.reset()
    # 迁移脚本自带 get_conn，测试期间指向临时库
    monkeypatch.setattr(migrate_legacy_assets, "get_conn", legacy_db.connect)
    from services import settlement_service
    monkeypatch.setattr(settlement_service, "get_conn", legacy_db.connect)
    return legacy_db


# ==================== 镜像触发器：旧账本任何写法都要同步 ====================

def test_legacy_delta_update_mirrors_to_user_assets(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 20)")

    row = store.one("SELECT * FROM user_assets WHERE user_id = 1")
    assert Decimal(row["points"]) == Decimal("100")
    assert Decimal(row["rain_balance"]) == Decimal("20")

    # 旧业务最典型的写法：增量加
    store.exec("UPDATE users SET member_points = member_points + 50, true_total_points = true_total_points + 5 WHERE id = 1")
    row = store.one("SELECT * FROM user_assets WHERE user_id = 1")
    assert Decimal(row["points"]) == Decimal("150")
    assert Decimal(row["rain_balance"]) == Decimal("25")

    # 旧业务另一种写法：绝对赋值（雨点兑券、公益捐赠都在用）
    store.exec("UPDATE users SET true_total_points = 7 WHERE id = 1")
    row = store.one("SELECT * FROM user_assets WHERE user_id = 1")
    assert Decimal(row["rain_balance"]) == Decimal("7")


def test_unrelated_user_update_keeps_mirror_stable(store):
    store.exec("INSERT INTO users (id, member_points) VALUES (1, 100)")
    before = store.one("SELECT points, updated_at FROM user_assets WHERE user_id = 1")

    # 登录、改昵称、改等级这类更新不应动到资产镜像
    store.exec("UPDATE users SET member_level = 3 WHERE id = 1")

    after = store.one("SELECT points, updated_at FROM user_assets WHERE user_id = 1")
    assert Decimal(after["points"]) == Decimal("100")
    assert after["updated_at"] == before["updated_at"]


def test_negative_deduction_raises_and_rolls_back(store):
    """旧账本被扣成负数 = 上游越界扣减 Bug，必须整条语句/事务回滚，不得夹 0。"""
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 10, 5)")

    with pytest.raises(pymysql.err.OperationalError) as exc:
        store.exec("UPDATE users SET member_points = member_points - 50 WHERE id = 1")
    assert "legacy_asset_negative" in str(exc.value)

    # 回滚到底：旧账本与镜像都停在原值，没有 0、也没有任何部分写入
    legacy = store.one("SELECT member_points, true_total_points FROM users WHERE id = 1")
    mirror = store.one("SELECT points, rain_balance FROM user_assets WHERE user_id = 1")
    assert Decimal(legacy["member_points"]) == Decimal("10")
    assert Decimal(legacy["true_total_points"]) == Decimal("5")
    assert Decimal(mirror["points"]) == Decimal("10")
    assert Decimal(mirror["rain_balance"]) == Decimal("5")


def test_negative_absolute_assignment_raises(store):
    """旧代码的绝对赋值写法（SET true_total_points = %s）同样要被拦住。"""
    store.exec("INSERT INTO users (id, true_total_points) VALUES (1, 5)")

    with pytest.raises(pymysql.err.OperationalError) as exc:
        store.exec("UPDATE users SET true_total_points = -1 WHERE id = 1")
    assert "legacy_asset_negative" in str(exc.value)
    assert Decimal(store.one("SELECT true_total_points FROM users WHERE id = 1")["true_total_points"]) == Decimal("5")
    assert Decimal(store.one("SELECT rain_balance FROM user_assets WHERE user_id = 1")["rain_balance"]) == Decimal("5")


def test_negative_insert_is_rejected(store):
    """新建会员就带负数同样是 Bug，明确报错而不是让镜像 CHECK 报个模糊的违约。"""
    with pytest.raises(pymysql.err.OperationalError) as exc:
        store.exec("INSERT INTO users (id, member_points) VALUES (9, -1)")
    assert "legacy_asset_negative" in str(exc.value)
    assert store.one("SELECT 1 AS hit FROM users WHERE id = 9") is None
    assert store.one("SELECT 1 AS hit FROM user_assets WHERE user_id = 9") is None


def test_ledger_recovers_after_rejected_write(store):
    """SIGNAL 只回滚那一条语句，连接与后续合法写入不受影响。"""
    store.exec("INSERT INTO users (id, member_points) VALUES (1, 10)")
    with pytest.raises(pymysql.err.OperationalError):
        store.exec("UPDATE users SET member_points = member_points - 11 WHERE id = 1")

    store.exec("UPDATE users SET member_points = member_points + 5 WHERE id = 1")
    assert Decimal(store.one("SELECT member_points FROM users WHERE id = 1")["member_points"]) == Decimal("15")
    assert Decimal(store.one("SELECT points FROM user_assets WHERE user_id = 1")["points"]) == Decimal("15")


def _exec_statements(store, path: Path) -> None:
    """按迁移文件逐条执行；不带参数绑定，避免注释里的 % 被当成格式符。"""
    with store.connect() as conn:
        with conn.cursor() as cur:
            for stmt in _migration_statements(path):
                cur.execute(stmt)
        conn.commit()


def test_bridge_requires_installed_triggers(store):
    with store.connect() as conn:
        with conn.cursor() as cur:
            bridge.assert_bridge_installed(cur)

    store.exec("DROP TRIGGER IF EXISTS trg_users_assets_mirror_update")

    with store.connect() as conn:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as exc:
                bridge.assert_bridge_installed(cur)
    assert "008_legacy_assets_bridge" in str(exc.value)

    # 复原，避免影响同模块后续用例
    _exec_statements(store, BRIDGE_MIGRATION)
    with store.connect() as conn:
        with conn.cursor() as cur:
            bridge.assert_bridge_installed(cur)


# ==================== 桥接写入口 ====================

def test_add_points_and_rain_roundtrip(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 10)")

    with store.connect() as conn:
        with conn.cursor() as cur:
            assert bridge.add_points(cur, 1, "25") == Decimal("125")
            assert bridge.add_rain(cur, 1, "-4") == Decimal("6")
        conn.commit()

    row = store.one("SELECT points, rain_balance FROM user_assets WHERE user_id = 1")
    assert Decimal(row["points"]) == Decimal("125")
    assert Decimal(row["rain_balance"]) == Decimal("6")

    snapshot = None
    with store.connect() as conn:
        with conn.cursor() as cur:
            snapshot = bridge.assets_snapshot(cur, 1)
    assert snapshot["legacy_points"] == snapshot["mirror_points"] == Decimal("125")
    assert snapshot["legacy_rain"] == snapshot["mirror_rain"] == Decimal("6")


def test_add_points_rejects_overdraft(store):
    store.exec("INSERT INTO users (id, member_points) VALUES (1, 100)")
    with store.connect() as conn:
        with conn.cursor() as cur:
            with pytest.raises(bridge.LegacyBridgeError):
                bridge.add_points(cur, 1, "-101")
        conn.rollback()

    assert Decimal(store.one("SELECT member_points FROM users WHERE id = 1")["member_points"]) == Decimal("100")
    assert Decimal(store.one("SELECT points FROM user_assets WHERE user_id = 1")["points"]) == Decimal("100")


def test_add_points_requires_existing_user(store):
    with store.connect() as conn:
        with conn.cursor() as cur:
            with pytest.raises(bridge.LegacyBridgeError):
                bridge.add_points(cur, 999, "1")
        conn.rollback()


# ==================== 历史数据迁移脚本 ====================

def test_migration_backfills_history_and_reconciles(store):
    # Only absent mirrors without V1 points/rain history are unambiguous backfills.
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 1000, 50), (2, 250.5, 0), (3, 0, 7)")
    store.exec("DELETE FROM user_assets WHERE user_id = 1")
    store.exec("DELETE FROM user_assets WHERE user_id = 2")

    before = migrate_legacy_assets.reconcile(dry_run=False)
    assert before["balanced"] is False, "迁移前就应当报出差异"

    result = migrate_legacy_assets.run()

    assert result["balanced"] is True
    assert result["after"]["drift_points"] == "0.000000"
    assert result["after"]["drift_rain"] == "0.000000"
    assert result["after"]["points_mismatch_users"] == 0
    assert result["after"]["rain_mismatch_users"] == 0
    assert result["after"]["assets_row_missing"] == 0
    assert result["written"]["updated"] == 0
    assert result["written"]["inserted"] >= 1

    assert Decimal(store.one("SELECT member_points FROM users WHERE id = 1")["member_points"]) == Decimal("1000")
    assert Decimal(store.one("SELECT points FROM user_assets WHERE user_id = 1")["points"]) == Decimal("1000")
    assert Decimal(store.one("SELECT points FROM user_assets WHERE user_id = 2")["points"]) == Decimal("250.5")
    assert Decimal(store.one("SELECT rain_balance FROM user_assets WHERE user_id = 3")["rain_balance"]) == Decimal("7")


def test_migration_is_idempotent(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 1000, 50),(2, 33, 0)")
    store.exec("DELETE FROM user_assets WHERE user_id = 1")

    first = migrate_legacy_assets.run()
    assert first["written"]["inserted"] == 1

    second = migrate_legacy_assets.run()
    assert second["written"]["inserted"] == 0
    assert second["written"]["updated"] == 0, "重复执行不得再写库"
    assert second["written"]["unchanged"] == second["written"]["scanned"] == 2
    assert second["balanced"] is True


def test_migration_dry_run_writes_nothing(store):
    store.exec("INSERT INTO users (id, member_points) VALUES (1, 800)")
    store.exec("UPDATE user_assets SET points = 0 WHERE user_id = 1")

    result = migrate_legacy_assets.run(dry_run=True)

    assert result["mode"] == "dry-run"
    assert Decimal(store.one("SELECT points FROM user_assets WHERE user_id = 1")["points"]) == Decimal("0")
    assert result["balanced"] is False


def test_migration_refuses_negative_legacy_values(store):
    """008 上线前的历史脏数据：负值会员被拒绝迁移，脚本判定失败，绝不悄悄写成 0。"""
    # 触发器上线前的历史数据可以带负值，这里模拟那一批存量（临时摘掉 INSERT 触发器造数）
    store.exec("DROP TRIGGER IF EXISTS trg_users_assets_mirror_insert")
    try:
        store.exec(
            "INSERT INTO users (id, member_points, true_total_points) VALUES (1, -12, 0), (2, 30, 0)"
        )
    finally:
        _exec_statements(store, BRIDGE_MIGRATION)

    result = migrate_legacy_assets.run()

    assert result["written"]["negative"] == 1
    assert [item["user_id"] for item in result["negative_users"]] == [1]
    assert result["ok"] is False, "存在负值会员时不得判定迁移成功"
    # 负值会员不落镜像（不会被夹成 0），正常会员照常迁移
    assert store.one("SELECT 1 AS hit FROM user_assets WHERE user_id = 1") is None
    assert Decimal(store.one("SELECT points FROM user_assets WHERE user_id = 2")["points"]) == Decimal("30")


def test_parity_view_exposes_drift(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 20)")
    store.exec("UPDATE user_assets SET points = 90 WHERE user_id = 1")

    row = store.one("SELECT * FROM v_legacy_asset_parity WHERE user_id = 1")
    assert Decimal(row["drift_points"]) == Decimal("10")
    assert Decimal(row["drift_rain"]) == Decimal("0")

    drift = None
    with store.connect() as conn:
        with conn.cursor() as cur:
            drift = bridge.drift_users(cur)
    assert [int(item["user_id"]) for item in drift] == [1]


def test_migration_reloads_stale_page_before_writing(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 20)")
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, member_points, true_total_points FROM users")
            stale = cur.fetchall()
            store.exec("UPDATE users SET member_points = member_points + 7, true_total_points = true_total_points + 3 WHERE id = 1")
            migrate_legacy_assets.migrate_batch(cur, stale, False)
        conn.commit()
    assert store.one("SELECT points, rain_balance FROM user_assets WHERE user_id = 1") == {"points": Decimal("107"), "rain_balance": Decimal("23")}


@pytest.mark.parametrize("mode", ["dry", "limit"])
def test_partial_or_dry_migration_never_publishes_completion(store, mode):
    store.exec("INSERT INTO users (id) VALUES (1)")
    result = migrate_legacy_assets.run(dry_run=mode == "dry", limit=1 if mode == "limit" else None)
    assert result["balanced"] is True
    assert result["completed"] is False
    assert not store.one("SELECT completed FROM legacy_bridge_completion WHERE id = 1")["completed"]


def test_failed_rerun_revokes_old_completion(store, monkeypatch):
    assert migrate_legacy_assets.run()["completed"] is True
    def fail(*args, **kwargs):
        raise RuntimeError("migration interrupted")
    monkeypatch.setattr(migrate_legacy_assets, "iter_legacy_users", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        migrate_legacy_assets.run()
    assert not store.one("SELECT completed FROM legacy_bridge_completion WHERE id = 1")["completed"]


def test_concurrent_migration_rejected_without_revoking_completion(store):
    assert migrate_legacy_assets.run()["completed"] is True
    with store.connect() as owner:
        with owner.cursor() as cur:
            cur.execute("SELECT GET_LOCK(CONCAT('legacy_bridge:', LEFT(SHA2(DATABASE(), 256), 50)), 0)")
            try:
                with pytest.raises(bridge.LegacyBridgeError, match="已在运行"):
                    migrate_legacy_assets.run()
            finally:
                cur.execute("SELECT RELEASE_LOCK(CONCAT('legacy_bridge:', LEFT(SHA2(DATABASE(), 256), 50)))")
    assert store.one("SELECT completed FROM legacy_bridge_completion WHERE id = 1")["completed"]


def test_final_drift_never_publishes_completion(store, monkeypatch):
    store.exec("INSERT INTO users (id, member_points) VALUES (1, 100)")
    original = migrate_legacy_assets.migrate_batch
    def corrupt_after_batch(cur, *args, **kwargs):
        result = original(cur, *args, **kwargs)
        cur.execute("UPDATE user_assets SET points = points + 1 WHERE user_id = 1")
        return result
    monkeypatch.setattr(migrate_legacy_assets, "migrate_batch", corrupt_after_batch)
    assert migrate_legacy_assets.run()["completed"] is False
    assert not store.one("SELECT completed FROM legacy_bridge_completion WHERE id = 1")["completed"]


@pytest.mark.parametrize("entry", ["daily", "expiry", "rain", "coupons", "user_expiry", "user_rain", "user_coupons"])
@pytest.mark.parametrize("unready", ["incomplete", "missing_trigger", "changed_signature"])
def test_all_settlement_entries_fail_closed(store, entry, unready):
    from datetime import date
    from services.rights_service import RightsError
    from services.settlement_service import SettlementService
    if unready != "incomplete":
        migrate_legacy_assets.run()
    if unready == "missing_trigger":
        store.exec("DROP TRIGGER trg_users_assets_mirror_update")
    elif unready == "changed_signature":
        store.exec("UPDATE legacy_bridge_completion SET trigger_signature = REPEAT('0', 64) WHERE id = 1")
    service = SettlementService()
    day = date(2026, 10, 30)
    calls = {
        "daily": lambda: service.run_daily_settlement(day, "0.0001"),
        "expiry": lambda: service.expire_due_coupons(day),
        "rain": lambda: service.generate_rain(day, Decimal("0.0001")),
        "coupons": lambda: service.convert_rain_to_coupons(day),
        "user_expiry": lambda: service._expire_user_coupons(1, day),
        "user_rain": lambda: service._settle_user_rain(1, day, Decimal("0.0001")),
        "user_coupons": lambda: service._convert_user_rain(1, day),
    }
    try:
        with pytest.raises(RightsError) as exc:
            calls[entry]()
        assert exc.value.http_status == 503
    finally:
        if unready == "missing_trigger":
            _exec_statements(store, BRIDGE_MIGRATION)


def test_readiness_shared_lock_blocks_migration_invalidation(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    migrate_legacy_assets.run()
    started = threading.Event()
    def invalidate():
        with store.connect() as conn:
            with conn.cursor() as cur:
                started.set()
                cur.execute("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")
            conn.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connect() as conn:
            with conn.cursor() as cur:
                bridge.assert_settlement_ready(cur)
                future = pool.submit(invalidate)
                assert started.wait(5)
                assert not future.done()
            conn.commit()
        future.result(timeout=10)
    with store.connect() as conn:
        with conn.cursor() as cur:
            with pytest.raises(Exception):
                bridge.assert_settlement_ready(cur)


def test_migration_missing_mirror_uses_latest_locked_balance(store):
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 20)")
    store.exec("DELETE FROM user_assets WHERE user_id = 1")
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, member_points, true_total_points FROM users")
            stale = cur.fetchall()
            store.exec("UPDATE users SET member_points = member_points + 7 WHERE id = 1")
            migrate_legacy_assets.migrate_batch(cur, stale, False)
        conn.commit()
    assert store.one("SELECT points FROM user_assets WHERE user_id = 1")["points"] == Decimal("107")


def test_member_transaction_rechecks_gate_after_scan(store, monkeypatch):
    from datetime import date
    from services.rights_service import RightsError
    from services.settlement_service import SettlementService
    store.exec("INSERT INTO users (id, member_points) VALUES (1, 100)")
    migrate_legacy_assets.run()
    service = SettlementService()
    original = service._settle_user_rain
    def invalidate_before_member(*args):
        store.exec("UPDATE legacy_bridge_completion SET completed = FALSE WHERE id = 1")
        return original(*args)
    monkeypatch.setattr(service, "_settle_user_rain", invalidate_before_member)
    with pytest.raises(RightsError):
        service.generate_rain(date(2026, 10, 30), Decimal("0.01"))
    assert store.one("SELECT COUNT(*) AS n FROM rights_daily_settlements")["n"] == 0
    assert store.one("SELECT true_total_points FROM users WHERE id = 1")["true_total_points"] == 0


def test_dry_run_preserves_existing_completion(store):
    migrate_legacy_assets.run()
    before = store.one("SELECT * FROM legacy_bridge_completion WHERE id = 1")
    assert migrate_legacy_assets.run(dry_run=True)["completed"] is False
    assert store.one("SELECT * FROM legacy_bridge_completion WHERE id = 1") == before


def test_trigger_replacement_invalidates_completed_migration(store):
    from services.rights_service import RightsError
    migrate_legacy_assets.run()
    # Change a definition deterministically; CREATED precision varies by MySQL version.
    store.exec("DROP TRIGGER trg_users_assets_mirror_update")
    try:
        store.exec("""CREATE TRIGGER trg_users_assets_mirror_update AFTER UPDATE ON users
                      FOR EACH ROW SET @bridge_test_changed = 1""")
        with store.connect() as conn:
            with conn.cursor() as cur:
                with pytest.raises(RightsError):
                    bridge.assert_settlement_ready(cur)
    finally:
        _exec_statements(store, BRIDGE_MIGRATION)


def test_migration_waits_for_concurrent_legacy_writer(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store.exec("INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 20)")
    started = threading.Event()
    stale = [{"id": 1, "member_points": Decimal("100"), "true_total_points": Decimal("20")}]
    def migrate():
        with store.connect() as conn:
            with conn.cursor() as cur:
                started.set()
                migrate_legacy_assets.migrate_batch(cur, stale, False)
            conn.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connect() as writer:
            with writer.cursor() as cur:
                cur.execute("UPDATE users SET member_points = member_points + 7 WHERE id = 1")
                future = pool.submit(migrate)
                assert started.wait(5)
                assert not future.done()
            writer.commit()
        future.result(timeout=10)
    assert store.one("SELECT points FROM user_assets WHERE user_id = 1")["points"] == Decimal("107")


@pytest.mark.parametrize("missing", ["table", "row"])
def test_missing_completion_metadata_fails_closed(store, missing):
    from services.rights_service import RightsError
    if missing == "table":
        store.exec("RENAME TABLE legacy_bridge_completion TO saved_legacy_bridge_completion")
    else:
        store.exec("DELETE FROM legacy_bridge_completion WHERE id = 1")
    try:
        with store.connect() as conn:
            with conn.cursor() as cur:
                with pytest.raises(RightsError) as exc:
                    bridge.assert_settlement_ready(cur)
                assert exc.value.http_status == 503
    finally:
        if missing == "table":
            store.exec("RENAME TABLE saved_legacy_bridge_completion TO legacy_bridge_completion")
        else:
            store.exec("INSERT INTO legacy_bridge_completion (id, completed) VALUES (1, FALSE)")


def test_limited_rerun_revokes_even_balanced_completion(store):
    store.exec("INSERT INTO users (id) VALUES (1)")
    assert migrate_legacy_assets.run()["completed"] is True
    result = migrate_legacy_assets.run(limit=1)
    assert result["balanced"] is True
    assert result["completed"] is False
    assert not store.one("SELECT completed FROM legacy_bridge_completion WHERE id = 1")["completed"]

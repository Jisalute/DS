"""Historical dual-ledger conflicts and actual rain conversion accounting; not executed here."""
from datetime import date
from decimal import Decimal

import pytest

from test_v1_rights_mysql import scratch_db, store  # noqa: F401
from services import legacy_assets_bridge as bridge
from services.rights_service import RightsService
from services.settlement_service import SettlementService
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import migrate_legacy_assets


@pytest.mark.parametrize('asset,event', [('rain', 'rain_daily_settlement'), ('points', 'points_expiry_backfill')])
def test_historical_v1_credit_is_never_overwritten(store, monkeypatch, asset, event):
    monkeypatch.setattr(migrate_legacy_assets, 'get_conn', store.connect)
    store.exec('INSERT INTO users (id) VALUES (1)')
    column = 'rain_balance' if asset == 'rain' else 'points'
    store.exec(f'UPDATE user_assets SET {column} = 10 WHERE user_id = 1')
    with store.connect() as conn:
        with conn.cursor() as cur:
            RightsService._write_ledger(cur, 1, asset, event, Decimal('10'), Decimal('10'))
        conn.commit()
    result = migrate_legacy_assets.run()
    assert not result['completed']
    conflict = result['conflict_users'][0]
    assert conflict['user_id'] == 1
    assert conflict['points_rain_ledger_rows'] == 1
    assert conflict['mirror_rain' if asset == 'rain' else 'mirror_points'] == '10.000000'
    assert store.one(f'SELECT {column} AS balance FROM user_assets WHERE user_id = 1')['balance'] == 10
    assert store.one('SELECT COUNT(*) AS n FROM rights_ledger')['n'] == 1


def test_zero_existing_mirror_is_ambiguous_but_missing_history_free_row_backfills(store, monkeypatch):
    monkeypatch.setattr(migrate_legacy_assets, 'get_conn', store.connect)
    store.exec('INSERT INTO users (id, member_points) VALUES (1, 100), (2, 25)')
    store.exec('UPDATE user_assets SET points = 0 WHERE user_id = 1')
    store.exec('DELETE FROM user_assets WHERE user_id = 2')
    result = migrate_legacy_assets.run(batch_size=1)
    assert not result['completed']
    assert [r['user_id'] for r in result['conflict_users']] == [1]
    assert store.one('SELECT points FROM user_assets WHERE user_id = 1')['points'] == 0
    assert store.one('SELECT points FROM user_assets WHERE user_id = 2')['points'] == 25
    assert not store.one('SELECT completed FROM legacy_bridge_completion WHERE id = 1')['completed']


def test_missing_mirror_with_v1_history_requires_review(store, monkeypatch):
    monkeypatch.setattr(migrate_legacy_assets, 'get_conn', store.connect)
    store.exec('INSERT INTO users (id, member_points) VALUES (1, 100)')
    with store.connect() as conn:
        with conn.cursor() as cur:
            RightsService._write_ledger(cur, 1, 'points', 'points_expiry_backfill', Decimal('10'), Decimal('110'))
        conn.commit()
    store.exec('DELETE FROM user_assets WHERE user_id = 1')
    result = migrate_legacy_assets.run()
    assert result['conflict_users'][0]['reason'] == 'MISSING_MIRROR_WITH_V1_HISTORY'
    assert store.one('SELECT * FROM user_assets WHERE user_id = 1') is None


@pytest.mark.parametrize('generation_first', [True, False])
def test_history_conversion_and_same_day_new_sources_close_all_records(store, generation_first):
    store.exec('INSERT INTO users (id, member_points, true_total_points) VALUES (1, 10000, 50)')
    service = SettlementService()
    day = date(2026, 10, 30)
    if generation_first:
        assert service._settle_user_rain(1, day, Decimal('0.0001')) == 1
    assert service._convert_user_rain(1, day) == (51 if generation_first else 50)
    if not generation_first:
        assert service._settle_user_rain(1, day, Decimal('0.0001')) == 1
        assert service._convert_user_rain(1, day) == 1
    assert service._settle_user_rain(1, day, Decimal('0.0001')) == 0
    assert service._convert_user_rain(1, day) == 0
    store.exec('UPDATE users SET true_total_points = true_total_points + 3 WHERE id = 1')
    assert service._convert_user_rain(1, day) == 3
    summary = store.one('SELECT rain_generated, rain_converted, coupons_issued FROM rights_daily_settlements WHERE user_id = 1')
    assert summary == {'rain_generated': Decimal('1'), 'rain_converted': Decimal('54'), 'coupons_issued': Decimal('54')}
    assert store.one('SELECT SUM(quantity) AS n FROM rights_coupon_batches WHERE user_id = 1')['n'] == 54
    assert store.one("SELECT SUM(quantity_delta) AS n FROM rights_ledger WHERE asset_type = 'coupon' AND event_type = 'coupon_issued'")['n'] == 54
    assert store.one("SELECT SUM(quantity_delta) AS n FROM rights_ledger WHERE asset_type = 'rain' AND event_type = 'rain_to_coupon'")['n'] == -54
    assert store.one('SELECT rain_balance, coupon_balance FROM user_assets WHERE user_id = 1') == {'rain_balance': Decimal('0'), 'coupon_balance': Decimal('54')}


def test_conversion_failure_rolls_back_audit_batches_and_balances(store, monkeypatch):
    store.exec('INSERT INTO users (id, true_total_points) VALUES (1, 50)')
    original = RightsService._write_ledger
    def fail_coupon(cur, user_id, asset_type, *args, **kwargs):
        if asset_type == 'coupon':
            raise RuntimeError('injected ledger failure')
        return original(cur, user_id, asset_type, *args, **kwargs)
    monkeypatch.setattr(RightsService, '_write_ledger', staticmethod(fail_coupon))
    with pytest.raises(RuntimeError, match='injected'):
        SettlementService()._convert_user_rain(1, date(2026, 10, 30))
    assert store.one('SELECT true_total_points FROM users WHERE id = 1')['true_total_points'] == 50
    assert store.one('SELECT COUNT(*) AS n FROM rights_coupon_batches')['n'] == 0
    assert store.one('SELECT COUNT(*) AS n FROM rights_daily_settlements')['n'] == 0
    assert store.one('SELECT COUNT(*) AS n FROM rights_ledger')['n'] == 0
    monkeypatch.setattr(RightsService, '_write_ledger', staticmethod(original))
    assert SettlementService()._convert_user_rain(1, date(2026, 10, 30)) == 50


def test_midrun_failure_preserves_ambiguous_balances_and_committed_safe_backfill(store, monkeypatch):
    monkeypatch.setattr(migrate_legacy_assets, 'get_conn', store.connect)
    store.exec('INSERT INTO users (id, member_points) VALUES (1, 25), (2, 100), (3, 50)')
    store.exec('DELETE FROM user_assets WHERE user_id = 1')
    store.exec('UPDATE user_assets SET points = 7 WHERE user_id = 2')
    original = migrate_legacy_assets.migrate_batch
    def fail_third(cur, rows, *args):
        if rows[0]['id'] == 3:
            raise RuntimeError('interrupted after committed batches')
        return original(cur, rows, *args)
    monkeypatch.setattr(migrate_legacy_assets, 'migrate_batch', fail_third)
    with pytest.raises(RuntimeError, match='interrupted'):
        migrate_legacy_assets.run(batch_size=1)
    assert store.one('SELECT points FROM user_assets WHERE user_id = 1')['points'] == 25
    assert store.one('SELECT points FROM user_assets WHERE user_id = 2')['points'] == 7
    assert not store.one('SELECT completed FROM legacy_bridge_completion WHERE id = 1')['completed']


def test_dry_run_conflict_reports_original_values_without_writing(store, monkeypatch):
    monkeypatch.setattr(migrate_legacy_assets, 'get_conn', store.connect)
    store.exec('INSERT INTO users (id, member_points) VALUES (1, 100)')
    store.exec('UPDATE user_assets SET points = 7 WHERE user_id = 1')
    before = store.one('SELECT * FROM legacy_bridge_completion WHERE id = 1')
    result = migrate_legacy_assets.run(dry_run=True)
    assert result['conflict_users'][0]['legacy_points'] == '100.000000'
    assert result['conflict_users'][0]['mirror_points'] == '7.000000'
    assert store.one('SELECT points FROM user_assets WHERE user_id = 1')['points'] == 7
    assert store.one('SELECT * FROM legacy_bridge_completion WHERE id = 1') == before


def test_history_only_concurrent_conversion_records_actual_quantity_once(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store.exec('INSERT INTO users (id, true_total_points) VALUES (1, 50)')
    barrier = threading.Barrier(2)
    def convert():
        barrier.wait(timeout=10)
        return SettlementService()._convert_user_rain(1, date(2026, 10, 30))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(convert) for _ in range(2)]
        assert sorted(f.result(timeout=30) for f in futures) == [Decimal('0'), Decimal('50')]
    assert store.one('SELECT rain_generated, rain_converted, coupons_issued FROM rights_daily_settlements WHERE user_id = 1') == {'rain_generated': Decimal('0'), 'rain_converted': Decimal('50'), 'coupons_issued': Decimal('50')}
    assert store.one('SELECT COUNT(*) AS n FROM rights_coupon_batches')['n'] == 1
    assert store.one("SELECT SUM(quantity_delta) AS n FROM rights_ledger WHERE asset_type = 'coupon'")['n'] == 50


def test_generated_rain_after_history_conversion_stays_pending_until_conversion_commits(store, monkeypatch):
    store.exec('INSERT INTO users (id, member_points, true_total_points) VALUES (1, 10000, 50)')
    service = SettlementService()
    day = date(2026, 10, 30)
    assert service._convert_user_rain(1, day) == 50
    assert service._settle_user_rain(1, day, Decimal('0.0001')) == 1
    pending = store.one('SELECT status, settled_at, failure_code, rain_generated, rain_converted, coupons_issued FROM rights_daily_settlements WHERE user_id = 1')
    assert pending == {'status': 'pending', 'settled_at': None, 'failure_code': None, 'rain_generated': Decimal('1'), 'rain_converted': Decimal('50'), 'coupons_issued': Decimal('50')}
    original = RightsService._write_ledger
    def fail_coupon(cur, user_id, asset_type, *args, **kwargs):
        if asset_type == 'coupon':
            raise RuntimeError('injected posthistory conversion failure')
        return original(cur, user_id, asset_type, *args, **kwargs)
    monkeypatch.setattr(RightsService, '_write_ledger', staticmethod(fail_coupon))
    with pytest.raises(RuntimeError, match='posthistory'):
        service._convert_user_rain(1, day)
    assert store.one('SELECT status, settled_at, failure_code, rain_generated, rain_converted, coupons_issued FROM rights_daily_settlements WHERE user_id = 1') == pending
    assert store.one('SELECT true_total_points FROM users WHERE id = 1')['true_total_points'] == 1
    assert store.one('SELECT SUM(quantity) AS n FROM rights_coupon_batches WHERE user_id = 1')['n'] == 50
    assert store.one("SELECT SUM(quantity_delta) AS n FROM rights_ledger WHERE asset_type = 'coupon'")['n'] == 50
    monkeypatch.setattr(RightsService, '_write_ledger', staticmethod(original))
    assert service._settle_user_rain(1, day, Decimal('0.0001')) == 0
    assert service._convert_user_rain(1, day) == 1
    settled = store.one('SELECT status, settled_at, rain_generated, rain_converted, coupons_issued FROM rights_daily_settlements WHERE user_id = 1')
    assert settled['status'] == 'settled'
    assert settled['settled_at'] is not None
    assert settled['rain_generated'] == 1
    assert settled['rain_converted'] == settled['coupons_issued'] == 51
    assert service._convert_user_rain(1, day) == 0


@pytest.fixture
def pre_v1_upgrade_db(monkeypatch):
    """Install the real SQL after legacy users exist, not after an empty schema."""
    import hashlib
    import uuid
    import pymysql
    from test_v1_rights_mysql import ScratchDatabase, _mysql_kwargs, _migration_statements
    try:
        admin = pymysql.connect(**_mysql_kwargs())
    except Exception as exc:
        pytest.skip(f'MySQL unavailable: {exc}')
    name = 'ds_pre_v1_upgrade_' + uuid.uuid4().hex[:10]
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE `{name}` DEFAULT CHARACTER SET utf8mb4')
    admin.close()
    db = ScratchDatabase(name)
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE users (
                    id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
                    member_level TINYINT NOT NULL DEFAULT 0,
                    member_points DECIMAL(12,6) NOT NULL DEFAULT 0,
                    true_total_points DECIMAL(12,6) NOT NULL DEFAULT 0
                ) ENGINE=InnoDB''')
                cur.execute('INSERT INTO users (id, member_points, true_total_points) VALUES (1, 100, 50)')
                cur.execute('''CREATE TABLE schema_migrations (
                    version VARCHAR(255) PRIMARY KEY, sha256 CHAR(64) NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB''')
                root = Path(__file__).resolve().parents[1]
                for version in ('007_v1_rights.sql', '008_legacy_assets_bridge.sql', '009_legacy_bridge_completion.sql'):
                    path = root / 'migrations' / version
                    for stmt in _migration_statements(path):
                        cur.execute(stmt)
                    cur.execute('INSERT INTO schema_migrations (version, sha256) VALUES (%s, %s)', (version, hashlib.sha256(path.read_bytes()).hexdigest()))
            conn.commit()
        monkeypatch.setattr(migrate_legacy_assets, 'get_conn', db.connect)
        from services import settlement_service
        monkeypatch.setattr(settlement_service, 'get_conn', db.connect)
        yield db
    finally:
        admin = pymysql.connect(**_mysql_kwargs())
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS `{name}`')
        admin.close()


def test_real_legacy_upgrade_requires_trusted_origin_despite_valid_versions(pre_v1_upgrade_db):
    from services.rights_service import RightsError
    db = pre_v1_upgrade_db
    assert db.one('SELECT points, rain_balance FROM user_assets WHERE user_id = 1') == {'points': Decimal('100'), 'rain_balance': Decimal('0')}
    assert len(db.all('SELECT version, sha256 FROM schema_migrations')) == 3
    for table in ('rights_ledger', 'rights_daily_settlements', 'rights_operations'):
        assert db.one(f'SELECT COUNT(*) AS n FROM {table}')['n'] == 0
    for _ in range(2):
        result = migrate_legacy_assets.run()
        assert not result['completed']
        assert result['conflict_users'][0]['reason'] == 'AMBIGUOUS_EXISTING_BALANCE'
        assert result['conflict_users'][0]['legacy_rain'] == '50.000000'
        assert result['conflict_users'][0]['mirror_rain'] == '0.000000'
        assert db.one('SELECT rain_balance FROM user_assets WHERE user_id = 1')['rain_balance'] == 0
        assert db.one('SELECT true_total_points FROM users WHERE id = 1')['true_total_points'] == 50
        with pytest.raises(RightsError):
            SettlementService()._convert_user_rain(1, date(2026, 10, 30))
    assert db.one('SELECT COUNT(*) AS n FROM rights_coupon_batches')['n'] == 0
    assert db.one('SELECT COUNT(*) AS n FROM rights_ledger')['n'] == 0

"""Real isolated MySQL advisory-lock ownership and allocation rollback."""
import hashlib

import pymysql
import pytest
from test_payment_config_atomicity import finance_config


def _lock_name(db):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT DATABASE() name')
            name = cur.fetchone()['name']
    return 'ds:alloc:' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:48]


def _configs(db):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT account_type,config_params FROM finance_accounts ORDER BY account_type')
            return cur.fetchall()


def _assert_free(db, lock_name):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT IS_FREE_LOCK(%s) free', (lock_name,))
            assert cur.fetchone()['free'] == 1


def test_named_lock_timeout_preserves_configs_and_never_releases_other_session(db, finance_config):
    from core.exceptions import FinanceException
    from services.finance_service import FinanceService
    name = _lock_name(db)
    before = _configs(db)
    with db() as holder:
        with holder.cursor() as cur:
            cur.execute('SELECT CONNECTION_ID() id')
            holder_id = cur.fetchone()['id']
            cur.execute('SELECT GET_LOCK(%s, 0) acquired', (name,))
            assert cur.fetchone()['acquired'] == 1
            try:
                with pytest.raises(FinanceException):
                    FinanceService().set_pool_allocations({'maintain_pool': '0.005'})
                cur.execute('SELECT IS_USED_LOCK(%s) owner', (name,))
                assert cur.fetchone()['owner'] == holder_id
                assert _configs(db) == before
            finally:
                cur.execute('SELECT RELEASE_LOCK(%s) released', (name,))
                assert cur.fetchone()['released'] == 1
    _assert_free(db, name)
    FinanceService().set_pool_allocations({'maintain_pool': '0.005'})
    _assert_free(db, name)


def test_write_failure_rolls_back_first_update_and_closes_physical_connection(
        db, monkeypatch, finance_config):
    from services import finance_service
    before, name = _configs(db), _lock_name(db)
    original = finance_service.pymysql.connect
    seen = {'writes': 0, 'closed': 0}
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, key): return getattr(self.cursor, key)
        def execute(self, sql, args=None):
            if ' '.join(sql.lower().split()).startswith('update finance_accounts'):
                seen['writes'] += 1
                if seen['writes'] == 2:
                    raise pymysql.err.OperationalError(1205, 'synthetic second allocation update failure')
            return self.cursor.execute(sql, args)
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, key): return getattr(self.conn, key)
        def cursor(self, *args, **kwargs): return Cursor(self.conn.cursor(*args, **kwargs))
        def close(self):
            seen['closed'] += 1
            return self.conn.close()
    def instrumented(*args, **kwargs):
        conn = original(*args, **kwargs)
        return Connection(conn) if kwargs.get('autocommit') is False else conn
    monkeypatch.setattr(finance_service.pymysql, 'connect', instrumented)
    with pytest.raises(pymysql.err.OperationalError):
        finance_service.FinanceService().set_pool_allocations({'maintain_pool': '0.005', 'public_welfare': '0.005'})
    assert seen == {'writes': 2, 'closed': 1}
    assert _configs(db) == before
    _assert_free(db, name)


def test_connection_failure_does_not_modify_config_or_leak_lock(db, monkeypatch, finance_config):
    from services import finance_service
    before, name = _configs(db), _lock_name(db)
    original = finance_service.pymysql.connect
    def fail_physical_connection(*args, **kwargs):
        if kwargs.get('autocommit') is False:
            raise pymysql.err.OperationalError(2003, 'synthetic connection failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(finance_service.pymysql, 'connect', fail_physical_connection)
    with pytest.raises(pymysql.err.OperationalError):
        finance_service.FinanceService().set_pool_allocations({'maintain_pool': '0.005'})
    assert _configs(db) == before
    _assert_free(db, name)


def test_synthetic_database_names_have_independent_advisory_locks_on_same_server(db):
    """Two names on one guarded DB; this is not a real cross-database deployment test."""
    from services.finance_service import allocation_config_lock_name
    from scripts.security_isolation.safety import guard
    prefix = guard()
    first_name = allocation_config_lock_name(prefix + '_synthetic_a')
    second_name = allocation_config_lock_name(prefix + '_synthetic_b')
    assert first_name != second_name
    with db() as first, db() as second:
        with first.cursor() as a, second.cursor() as b:
            a.execute('SELECT CONNECTION_ID() id')
            first_id = a.fetchone()['id']
            b.execute('SELECT CONNECTION_ID() id')
            second_id = b.fetchone()['id']
            assert first_id != second_id
            a.execute('SELECT GET_LOCK(%s,0) acquired', (first_name,))
            assert a.fetchone()['acquired'] == 1
            try:
                b.execute('SELECT GET_LOCK(%s,0) acquired', (second_name,))
                assert b.fetchone()['acquired'] == 1
                try:
                    a.execute('SELECT IS_USED_LOCK(%s) owner', (first_name,))
                    assert a.fetchone()['owner'] == first_id
                    b.execute('SELECT IS_USED_LOCK(%s) owner', (second_name,))
                    assert b.fetchone()['owner'] == second_id
                finally:
                    b.execute('SELECT RELEASE_LOCK(%s) released', (second_name,))
                    assert b.fetchone()['released'] == 1
            finally:
                a.execute('SELECT RELEASE_LOCK(%s) released', (first_name,))
                assert a.fetchone()['released'] == 1

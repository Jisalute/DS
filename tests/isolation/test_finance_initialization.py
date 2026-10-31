"""Initialization must not rewrite configured finance allocations in the guarded DB."""
import json

from scripts.security_isolation.safety import guard


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def test_repeated_initialization_preserves_allocations_and_invalid_shapes(db):
    from core.config import get_db_config
    from database_setup import initialize_database

    run_id = guard()
    assert get_db_config()['database'] == run_id
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT DATABASE() db')
            assert cur.fetchone()['db'] == run_id
            cur.execute('SELECT run_id FROM security_isolation_owner')
            assert cur.fetchall() == [{'run_id': run_id}]
            cur.execute('SELECT id,config_params FROM finance_accounts')
            saved = cur.fetchall()
            configured = {
                'maintain_pool': {'allocation': '0.017', 'extra': {'owner': 'synthetic'}},
                'director_pool': 'synthetic-invalid-shape',
                'fund_pool': ['synthetic-invalid-shape'],
                'branch_pool': {'allocation': None, 'extra': 'preserve-invalid-field'},
                'city_pool': {'extra': 'preserve-while-defaulting'},
            }
            for account, value in configured.items():
                cur.execute('UPDATE finance_accounts SET config_params=%s WHERE account_type=%s',
                            (json.dumps(value), account))
                assert cur.rowcount == 1
        conn.commit()
    try:
        for _ in range(2):
            # Refuse to run DDL unless both the configured DB and its owner are still isolated.
            assert guard() == run_id and get_db_config()['database'] == run_id
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT DATABASE() db')
                    assert cur.fetchone()['db'] == run_id
                    cur.execute('SELECT run_id FROM security_isolation_owner')
                    assert cur.fetchall() == [{'run_id': run_id}]
            initialize_database()
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT account_type,config_params FROM finance_accounts WHERE account_type IN (%s,%s,%s,%s,%s)',
                                tuple(configured))
                    actual = {row['account_type']: _decode(row['config_params']) for row in cur.fetchall()}
            assert actual == {
                'maintain_pool': configured['maintain_pool'],
                'director_pool': configured['director_pool'],
                'fund_pool': configured['fund_pool'],
                'branch_pool': configured['branch_pool'],
                'city_pool': {'extra': 'preserve-while-defaulting', 'allocation': '0.01'},
            }
    finally:
        with db() as conn:
            with conn.cursor() as cur:
                for row in saved:
                    value = row['config_params']
                    if value is not None and not isinstance(value, str):
                        value = json.dumps(value)
                    cur.execute('UPDATE finance_accounts SET config_params=%s WHERE id=%s', (value, row['id']))
            conn.commit()


def test_initialization_does_not_overwrite_concurrent_allocation_write(db, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from core.config import get_db_config
    from database_setup import DatabaseManager, initialize_database

    run_id = guard()
    assert get_db_config()['database'] == run_id
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT DATABASE() db')
            assert cur.fetchone()['db'] == run_id
            cur.execute('SELECT run_id FROM security_isolation_owner')
            assert cur.fetchall() == [{'run_id': run_id}]
            cur.execute('SELECT id,config_params FROM finance_accounts')
            saved = cur.fetchall()
            cur.execute('UPDATE finance_accounts SET config_params=%s WHERE account_type=%s',
                        (json.dumps({'old_field': 'preserved'}), 'city_pool'))
            assert cur.rowcount == 1
        conn.commit()
    read_old, resume = threading.Event(), threading.Event()
    ids = {}
    original = DatabaseManager.init_all_tables
    class Cursor:
        def __init__(self, cursor):
            self.cursor, self.city_read = cursor, False
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            normalized = ' '.join(sql.lower().split())
            self.city_read = (normalized.startswith('select id, config_params from finance_accounts')
                              and args == ('city_pool',))
            return self.cursor.execute(sql, args)
        def fetchone(self):
            result = self.cursor.fetchone()
            if self.city_read and not read_old.is_set():
                with self.cursor.connection.cursor() as other:
                    other.execute('SELECT CONNECTION_ID() id')
                    ids['initializer'] = other.fetchone()['id']
                read_old.set()
                if not resume.wait(15):
                    raise TimeoutError('test did not resume initializer')
            return result
    def intercepted(manager, cursor):
        return original(manager, Cursor(cursor))
    monkeypatch.setattr(DatabaseManager, 'init_all_tables', intercepted)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            initialization = pool.submit(initialize_database)
            try:
                assert read_old.wait(15)
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute('SELECT CONNECTION_ID() id')
                        ids['writer'] = cur.fetchone()['id']
                        assert ids['writer'] != ids['initializer']
                        cur.execute("UPDATE finance_accounts SET config_params=JSON_SET(config_params,'$.allocation','0.007','$.concurrent','preserved') WHERE account_type='city_pool'")
                        assert cur.rowcount == 1
                    conn.commit()
            finally:
                resume.set()
            initialization.result(timeout=30)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT config_params FROM finance_accounts WHERE account_type='city_pool'")
                assert _decode(cur.fetchone()['config_params']) == {
                    'old_field': 'preserved', 'allocation': '0.007', 'concurrent': 'preserved'}
    finally:
        resume.set()
        with db() as conn:
            with conn.cursor() as cur:
                for row in saved:
                    value = row['config_params']
                    if value is not None and not isinstance(value, str):
                        value = json.dumps(value)
                    cur.execute('UPDATE finance_accounts SET config_params=%s WHERE id=%s', (value, row['id']))
            conn.commit()

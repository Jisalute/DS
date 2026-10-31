"""Daily subsidy rolls back real MySQL writes on a platform-branch SQL failure."""
from contextlib import contextmanager

import pymysql
import pytest


def _snapshot(db):
    with db() as conn:
        with conn.cursor() as cur:
            result = {}
            for table, columns in (
                ('users', 'id,member_points,subsidy_points,true_total_points'),
                ('finance_accounts', 'id,account_type,balance,config_params'),
                ('account_flow', 'id'), ('points_log', 'id'), ('weekly_subsidy_records', 'id'),
            ):
                cur.execute(f'SELECT {columns} FROM {table} ORDER BY id')
                result[table] = cur.fetchall()
            return result


@pytest.mark.parametrize('error_code', [1205, 1213])
def test_platform_subsidy_failure_rolls_back_prior_user_writes(db, users, monkeypatch, error_code):
    from core.database import get_conn
    from services import finance_service

    ordinary = next(user for user in users if user != 26)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO users(id,name,status) VALUES(26,'Synthetic subsidy platform',0)")
            cur.execute('UPDATE users SET member_points=100 WHERE id=%s', (ordinary,))
            cur.execute("UPDATE finance_accounts SET balance=1000,config_params=%s WHERE account_type='subsidy_pool'",
                        ('{\"points_value\":\"0.01\",\"auto_clear\":true}',))
            cur.execute("UPDATE finance_accounts SET balance=100 WHERE account_type='company_points'")
        conn.commit()
    before = _snapshot(db)
    state = {'ordinary_write': False, 'fault': False, 'clear_called': False}

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.cursor.close()
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            normalized = ' '.join(sql.lower().split())
            if (normalized.startswith('update users set subsidy_points') and 'true_total_points' in normalized
                    and args and args[-1] == 26):
                assert state['ordinary_write'], 'platform fault must follow an actual ordinary-user write'
                state['fault'] = True
                raise pymysql.err.OperationalError(error_code, 'synthetic platform-branch failure')
            result = self.cursor.execute(sql, args)
            if normalized.startswith('insert into weekly_subsidy_records') and args and args[0] != 26:
                state['ordinary_write'] = True
            return result

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def cursor(self, *args, **kwargs):
            return Cursor(self.conn.cursor(*args, **kwargs))

    @contextmanager
    def fault_connection():
        with get_conn() as conn:
            yield Connection(conn)

    monkeypatch.setattr(finance_service, 'get_conn', fault_connection)
    service = finance_service.FinanceService()
    original_clear = service.adjust_subsidy_points_value
    def record_clear(*args, **kwargs):
        state['clear_called'] = True
        return original_clear(*args, **kwargs)
    monkeypatch.setattr(service, 'adjust_subsidy_points_value', record_clear)
    assert service.distribute_daily_subsidy(force=True) is False
    assert state == {'ordinary_write': True, 'fault': True, 'clear_called': False}
    assert _snapshot(db) == before

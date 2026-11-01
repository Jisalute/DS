"""Real MySQL writes must not survive a failed adapter transaction suffix."""
from contextlib import contextmanager
from decimal import Decimal
import uuid

import pymysql
import pytest


@pytest.fixture
def adapter_table(db):
    table = 'adapter_atomic_' + uuid.uuid4().hex
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE `{table}` (id INT PRIMARY KEY, balance DECIMAL(12,4) NOT NULL) ENGINE=InnoDB')
            cur.execute(f'INSERT INTO `{table}` VALUES(1,10),(2,10)')
        conn.commit()
    yield table
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE `{table}`')


def _balances(db, table):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT id,balance FROM `{table}` ORDER BY id')
            return [row['balance'] for row in cur.fetchall()]


def test_real_lock_timeout_discards_prior_write_without_replay(db, adapter_table, monkeypatch):
    from core import db_adapter
    contexts = []

    @contextmanager
    def connect():
        with db() as conn:
            contexts.append({'entered': True, 'exited': False})
            entry = contexts[-1]
            try:
                with conn.cursor() as cur:
                    cur.execute('SET SESSION innodb_lock_wait_timeout=1')
                yield conn
            finally:
                entry['exited'] = True

    monkeypatch.setattr(db_adapter, 'get_conn', connect)
    adapter = db_adapter.PyMySQLAdapter()
    with db() as blocker:
        with blocker.cursor() as cur:
            cur.execute(f'SELECT id FROM `{adapter_table}` WHERE id=2 FOR UPDATE')
        try:
            adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance-3 WHERE id=1')
            with pytest.raises(pymysql.err.OperationalError) as error:
                adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance+3 WHERE id=2')
            assert error.value.args[0] == 1205
            assert contexts == [{'entered': True, 'exited': True}]
            with pytest.raises(RuntimeError):
                adapter.commit()
            with pytest.raises(RuntimeError):
                adapter.execute(f'UPDATE `{adapter_table}` SET balance=99 WHERE id=2')
            assert _balances(db, adapter_table) == [Decimal('10'), Decimal('10')]
        finally:
            blocker.rollback()
            adapter.rollback()


@pytest.mark.parametrize('error', [pymysql.err.OperationalError(1213, 'synthetic deadlock'),
                                   pymysql.err.InterfaceError(0, 'synthetic connection failure')])
def test_statement_failure_cannot_commit_only_suffix(db, adapter_table, monkeypatch, error):
    from core import db_adapter
    state = {'connections': 0, 'exits': 0, 'statements': 0}

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            state['statements'] += 1
            if state['statements'] == 2:
                raise error
            return self.cursor.execute(sql, args)

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def cursor(self):
            return Cursor(self.conn.cursor())

    @contextmanager
    def connect():
        with db() as conn:
            state['connections'] += 1
            try:
                yield Connection(conn)
            finally:
                state['exits'] += 1

    monkeypatch.setattr(db_adapter, 'get_conn', connect)
    adapter = db_adapter.PyMySQLAdapter()
    with pytest.raises(type(error)) as raised:
        with adapter.begin():
            adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance-3 WHERE id=1')
            adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance+3 WHERE id=2')
    assert raised.value is error
    assert state == {'connections': 1, 'exits': 1, 'statements': 2}
    assert _balances(db, adapter_table) == [Decimal('10'), Decimal('10')]
    # Explicit transaction completion permits reuse, but never automatic replay.
    with adapter.begin():
        adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance+1 WHERE id=1')
    assert state['connections'] == state['exits'] == 2
    assert _balances(db, adapter_table) == [Decimal('11'), Decimal('10')]


def test_close_discards_pending_transaction_and_context_commits(db, adapter_table, monkeypatch):
    from core import db_adapter
    monkeypatch.setattr(db_adapter, 'get_conn', db)
    adapter = db_adapter.PyMySQLAdapter()
    adapter.execute(f'UPDATE `{adapter_table}` SET balance=1 WHERE id=1')
    adapter.close()
    assert _balances(db, adapter_table) == [Decimal('10'), Decimal('10')]
    with adapter:
        adapter.execute(f'UPDATE `{adapter_table}` SET balance=12 WHERE id=1')
    assert _balances(db, adapter_table) == [Decimal('12'), Decimal('10')]


@pytest.fixture
def production_pool(monkeypatch):
    from core import database
    from core.config import get_db_config
    from dbutils.pooled_db import PooledDB

    pool = PooledDB(creator=pymysql, maxconnections=2, mincached=0, maxcached=2,
                    blocking=True, maxusage=0, setsession=[], ping=1,
                    cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                    **get_db_config())
    monkeypatch.setattr(database, 'get_pool', lambda: pool)
    yield pool
    pool.close()


@pytest.mark.parametrize('failure_at', ['statement', 'commit'])
def test_real_pooled_connection_loss_never_commits_transaction_suffix(
        db, adapter_table, production_pool, failure_at):
    from core.db_adapter import PyMySQLAdapter

    adapter = PyMySQLAdapter()
    try:
        identity = adapter.execute('SELECT CONNECTION_ID() AS id').fetchone()['id']
        adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance-3 WHERE id=1')
        # A separate real connection kills only this test-owned physical session.
        with db() as killer:
            with killer.cursor() as cur:
                cur.execute(f'KILL CONNECTION {int(identity)}')
        with pytest.raises((pymysql.err.OperationalError, pymysql.err.InterfaceError)):
            if failure_at == 'statement':
                adapter.execute(f'UPDATE `{adapter_table}` SET balance=balance+3 WHERE id=2')
            else:
                adapter.commit()
        with pytest.raises(RuntimeError):
            adapter.commit()
        assert _balances(db, adapter_table) == [Decimal('10'), Decimal('10')]
        assert production_pool._connections == 0
        adapter.rollback()
        with adapter.begin():
            new_identity = adapter.execute('SELECT CONNECTION_ID() AS id').fetchone()['id']
            assert new_identity != identity
            adapter.execute(f'UPDATE `{adapter_table}` SET balance=11 WHERE id=1')
        assert _balances(db, adapter_table) == [Decimal('11'), Decimal('10')]
        assert production_pool._connections == 0
    finally:
        adapter.rollback()


def test_commit_acknowledgement_loss_is_not_reported_as_rollback(
        db, adapter_table, production_pool, monkeypatch):
    from core import db_adapter
    from core.database import get_conn

    failure = pymysql.err.OperationalError(2013, 'synthetic lost commit acknowledgement')

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def commit(self):
            self.conn.commit()
            raise failure

    @contextmanager
    def lose_acknowledgement():
        with get_conn() as conn:
            yield Connection(conn)

    monkeypatch.setattr(db_adapter, 'get_conn', lose_acknowledgement)
    adapter = db_adapter.PyMySQLAdapter()
    try:
        adapter.execute(f'UPDATE `{adapter_table}` SET balance=13 WHERE id=1')
        with pytest.raises(pymysql.err.OperationalError) as error:
            adapter.commit()
        assert error.value is failure
        with pytest.raises(RuntimeError):
            adapter.commit()
        # Cleanup cannot undo a commit whose acknowledgement was lost.
        assert _balances(db, adapter_table) == [Decimal('13'), Decimal('10')]
        assert production_pool._connections == 0
    finally:
        adapter.rollback()


@pytest.mark.parametrize('fault', ['rollback', 'cursor_close', 'context_exit'])
def test_acknowledged_commit_survives_cleanup_fault(
        db, adapter_table, production_pool, monkeypatch, fault):
    from core import db_adapter
    from core.database import get_conn

    state = {'committed': False, 'rollback_calls': 0, 'returned': False}

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def close(self):
            self.cursor.close()
            if state['committed'] and fault == 'cursor_close':
                raise pymysql.err.InterfaceError(0, 'synthetic cursor cleanup failure')

    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def cursor(self):
            return Cursor(self.conn.cursor())
        def commit(self):
            self.conn.commit()
            state['committed'] = True
        def rollback(self):
            state['rollback_calls'] += 1
            if state['committed'] and fault == 'rollback':
                raise pymysql.err.OperationalError(2013, 'synthetic post-commit disconnect')
            return self.conn.rollback()

    @contextmanager
    def cleanup_fault():
        try:
            with get_conn() as conn:
                yield Connection(conn)
        finally:
            state['returned'] = True
        if state['committed'] and fault == 'context_exit':
            raise pymysql.err.InterfaceError(0, 'synthetic context cleanup failure')

    monkeypatch.setattr(db_adapter, 'get_conn', cleanup_fault)
    adapter = db_adapter.PyMySQLAdapter()
    adapter.execute(f'UPDATE `{adapter_table}` SET balance=14 WHERE id=1')
    adapter.commit()
    assert state == {'committed': True, 'rollback_calls': 0, 'returned': True}
    assert _balances(db, adapter_table) == [Decimal('14'), Decimal('10')]
    assert production_pool._connections == 0
    assert adapter._conn is adapter._cursor is adapter._connection_context is None
    assert adapter._failed is False

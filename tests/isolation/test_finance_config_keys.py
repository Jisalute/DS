"""Actual MySQL config-key updates must preserve independent configuration."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal

import pytest
from test_payment_config_atomicity import finance_config


def _set_config(db, account, value):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE finance_accounts SET config_params=%s WHERE account_type=%s',
                        (json.dumps(value), account))
        conn.commit()


def _config(db, account):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT config_params FROM finance_accounts WHERE account_type=%s', (account,))
            value = cur.fetchone()['config_params']
            return json.loads(value) if isinstance(value, str) else value


def test_manual_subsidy_and_dividend_settings_preserve_other_keys(db, finance_config):
    from services.finance_service import FinanceService
    service = FinanceService()
    subsidy = {'allocation': '0.12', 'daily_subsidy_ratio': '0.07', 'unrelated': {'x': 1}}
    director = {'allocation': '0.02', 'unrelated': {'x': 2}}
    _set_config(db, 'subsidy_pool', subsidy)
    _set_config(db, 'director_pool', director)
    assert service.set_daily_subsidy_ratio(0.09) is True
    subsidy['daily_subsidy_ratio'] = '0.09'
    assert _config(db, 'subsidy_pool') == subsidy
    assert service.adjust_subsidy_points_value(0.002, auto_clear=False) is True
    assert _config(db, 'subsidy_pool')['auto_clear'] is False
    assert service.adjust_subsidy_points_value(0.002, auto_clear=True) is True
    result = _config(db, 'subsidy_pool')
    assert {key: result[key] for key in subsidy} == subsidy
    assert Decimal(str(result['points_value'])) == Decimal('0.002')
    assert result['auto_clear'] is True
    assert service.adjust_subsidy_points_value(None) is True
    assert _config(db, 'subsidy_pool') == subsidy
    assert service.adjust_unilevel_dividend_amount(1.25)['success'] is True
    result = _config(db, 'director_pool')
    assert {key: result[key] for key in director} == director
    assert Decimal(str(result['fixed_amount_per_weight'])) == Decimal('1.25')
    assert service.adjust_unilevel_dividend_amount(None)['success'] is True
    assert _config(db, 'director_pool') == director


@pytest.mark.parametrize('method,account', [
    ('ratio', 'subsidy_pool'), ('subsidy_set', 'subsidy_pool'), ('subsidy_clear', 'subsidy_pool'),
    ('dividend_set', 'director_pool'), ('dividend_clear', 'director_pool')])
def test_nonobject_finance_config_is_rejected_unchanged(db, finance_config, method, account):
    from core.exceptions import FinanceException
    from services.finance_service import FinanceService
    _set_config(db, account, ['synthetic-invalid'])
    service = FinanceService()
    operations = {
        'ratio': lambda: service.set_daily_subsidy_ratio(0.09),
        'subsidy_set': lambda: service.adjust_subsidy_points_value(0.002, auto_clear=True),
        'subsidy_clear': lambda: service.adjust_subsidy_points_value(None),
        'dividend_set': lambda: service.adjust_unilevel_dividend_amount(1.25),
        'dividend_clear': lambda: service.adjust_unilevel_dividend_amount(None),
    }
    if method == 'ratio':
        assert operations[method]() is False
    else:
        with pytest.raises(FinanceException):
            operations[method]()
    assert _config(db, account) == ['synthetic-invalid']


@pytest.mark.parametrize('allocation_first', [True, False])
def test_allocation_and_daily_ratio_concurrent_updates_preserve_both_keys(
        db, monkeypatch, finance_config, allocation_first):
    from core.database import get_conn
    from services import finance_service
    _set_config(db, 'subsidy_pool', {'allocation': '0.12', 'daily_subsidy_ratio': '0.05', 'unrelated': 'kept'})
    first_actor = 'allocation' if allocation_first else 'ratio'
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    ids = {}
    class Cursor:
        def __init__(self, cursor, actor, connection_id):
            self.cursor, self.actor, self.connection_id = cursor, actor, connection_id
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            normalized = ' '.join(sql.lower().split())
            target = (normalized.startswith('update finance_accounts') and isinstance(args, (tuple, list))
                      and 'subsidy_pool' in args)
            if target:
                ids[self.actor] = self.connection_id
                if self.actor != first_actor:
                    attempted.set()
            result = self.cursor.execute(sql, args)
            if target and self.actor == first_actor and not held.is_set():
                held.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release subsidy pool row')
            return result
    class Connection:
        def __init__(self, conn, actor, connection_id):
            self.conn, self.actor, self.connection_id = conn, actor, connection_id
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs):
            return Cursor(self.conn.cursor(*args, **kwargs), self.actor, self.connection_id)
    @contextmanager
    def ratio_connection():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT CONNECTION_ID() id')
                connection_id = cur.fetchone()['id']
            yield Connection(conn, 'ratio', connection_id)
    original_connect = finance_service.pymysql.connect
    def allocation_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        if kwargs.get('autocommit') is not False:
            return conn
        with conn.cursor() as cur:
            cur.execute('SELECT CONNECTION_ID() id')
            connection_id = cur.fetchone()['id']
        return Connection(conn, 'allocation', connection_id)
    monkeypatch.setattr(finance_service, 'get_conn', ratio_connection)
    monkeypatch.setattr(finance_service.pymysql, 'connect', allocation_connect)
    def allocation(): return finance_service.FinanceService().set_pool_allocations({'subsidy_pool': '0.11'})
    def ratio(): return finance_service.FinanceService().set_daily_subsidy_ratio(0.09)
    actions = (allocation, ratio) if allocation_first else (ratio, allocation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(actions[0])
        try:
            assert held.wait(15)
            second = pool.submit(actions[1])
            assert attempted.wait(15)
            assert ids['allocation'] != ids['ratio']
            assert not second.done()
        finally:
            release.set()
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert results[1 if allocation_first else 0] is True
    assert _config(db, 'subsidy_pool') == {'allocation': '0.11', 'daily_subsidy_ratio': '0.09', 'unrelated': 'kept'}


def test_successful_daily_distribution_auto_clears_only_manual_keys(db, users, finance_config):
    from services.finance_service import FinanceService
    service = FinanceService()
    base = {'allocation': '0.12', 'daily_subsidy_ratio': '0.05', 'unrelated': 'kept'}
    _set_config(db, 'subsidy_pool', base)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO users(id,name,status) VALUES(26,'Synthetic subsidy platform',0)")
            cur.execute('UPDATE users SET member_points=100 WHERE id=%s', (users[1],))
            cur.execute("UPDATE finance_accounts SET balance=1000000 WHERE account_type='subsidy_pool'")
        conn.commit()
    assert service.adjust_subsidy_points_value(0.002, auto_clear=True) is True
    assert _config(db, 'subsidy_pool')['auto_clear'] is True
    assert service.distribute_daily_subsidy(force=True) is True
    assert _config(db, 'subsidy_pool') == base


@pytest.mark.parametrize('platform_first', [True, False])
def test_platform_and_referral_rates_concurrently_preserve_each_other(
        db, monkeypatch, finance_config, platform_first):
    from core.database import get_conn
    from services import finance_service
    _set_config(db, 'platform_revenue_pool', {
        'distribution_platform_rate': '0.20', 'direct_referral_reward_rate': '0.25',
        'unrelated': {'keep': 'synthetic'}})
    first_actor = 'platform' if platform_first else 'referral'
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    ids = {}
    class Cursor:
        def __init__(self, cursor, actor, connection_id):
            self.cursor, self.actor, self.connection_id = cursor, actor, connection_id
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            target = ' '.join(sql.lower().split()).startswith('update finance_accounts')
            if target:
                ids[self.actor] = self.connection_id
                if self.actor != first_actor:
                    attempted.set()
            result = self.cursor.execute(sql, args)
            if target and self.actor == first_actor and not held.is_set():
                held.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release platform configuration row')
            return result
    class Connection:
        def __init__(self, conn, actor, connection_id):
            self.conn, self.actor, self.connection_id = conn, actor, connection_id
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs):
            return Cursor(self.conn.cursor(*args, **kwargs), self.actor, self.connection_id)
    @contextmanager
    def instrumented():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT CONNECTION_ID() id')
                connection_id = cur.fetchone()['id']
            yield Connection(conn, local.actor, connection_id)
    monkeypatch.setattr(finance_service, 'get_conn', instrumented)
    def update(actor):
        local.actor = actor
        service = finance_service.FinanceService()
        if actor == 'platform':
            return service.set_distribution_platform_rate(Decimal('0.30'))
        return service.set_direct_referral_reward_rate(Decimal('0.40'))
    second_actor = 'referral' if platform_first else 'platform'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(update, first_actor)
        try:
            assert held.wait(15)
            second = pool.submit(update, second_actor)
            assert attempted.wait(15)
            assert ids['platform'] != ids['referral']
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=15)
        second.result(timeout=15)
    result = _config(db, 'platform_revenue_pool')
    assert Decimal(str(result['distribution_platform_rate'])) == Decimal('0.30')
    assert Decimal(str(result['direct_referral_reward_rate'])) == Decimal('0.40')
    assert result['unrelated'] == {'keep': 'synthetic'}


@pytest.mark.parametrize('invalid_shape', ['synthetic-scalar', ['synthetic-array']])
@pytest.mark.parametrize('method', ['platform', 'referral'])
def test_platform_rate_setters_reject_nonobject_config(db, finance_config, method, invalid_shape):
    from core.exceptions import FinanceException
    from services.finance_service import FinanceService
    _set_config(db, 'platform_revenue_pool', invalid_shape)
    service = FinanceService()
    with pytest.raises(FinanceException):
        if method == 'platform':
            service.set_distribution_platform_rate(Decimal('0.30'))
        else:
            service.set_direct_referral_reward_rate(Decimal('0.40'))
    assert _config(db, 'platform_revenue_pool') == invalid_shape


@pytest.mark.parametrize('method,value', [
    ('platform', '-1'), ('platform', '0'), ('platform', '0.500001'),
    ('referral', '-1'), ('referral', '0'), ('referral', '1.000001')])
def test_platform_rate_setters_keep_existing_ratio_bounds(db, finance_config, method, value):
    from services.finance_service import FinanceService
    original = {'distribution_platform_rate': '0.20', 'direct_referral_reward_rate': '0.25',
                'unrelated': 'kept'}
    _set_config(db, 'platform_revenue_pool', original)
    service = FinanceService()
    with pytest.raises(ValueError):
        if method == 'platform':
            service.set_distribution_platform_rate(Decimal(value))
        else:
            service.set_direct_referral_reward_rate(Decimal(value))
    assert _config(db, 'platform_revenue_pool') == original

"""
统一的数据库连接管理模块
使用 pymysql + DBUtils 连接池（2c2g 多 worker 场景）
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Optional

import pymysql
from dbutils.pooled_db import PooledDB

from core.config import get_db_config, settings
from core.logging import get_logger

logger = get_logger(__name__)

_db_config: Optional[dict] = None
_pool: Optional[PooledDB] = None
_pool_lock = threading.Lock()


def get_db_config_cached() -> dict:
    global _db_config
    if _db_config is None:
        _db_config = get_db_config()
    return _db_config


def _pool_size_for_process() -> int:
    """单进程连接池上限；总连接数 ≈ UVICORN_WORKERS × MYSQL_POOL_SIZE。"""
    return max(1, int(settings.MYSQL_POOL_SIZE))


def get_pool() -> PooledDB:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        cfg = get_db_config_cached()
        size = _pool_size_for_process()
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=size,
            mincached=min(1, size),
            maxcached=size,
            blocking=True,
            maxusage=0,
            setsession=[],
            ping=1,
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset=cfg["charset"],
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        logger.info(
            "数据库连接池已初始化: pool_size=%s workers=%s (预估总连接上限≈%s)",
            size,
            settings.UVICORN_WORKERS,
            size * max(1, settings.UVICORN_WORKERS),
        )
        return _pool


def pool_stats() -> dict:
    """供 /health 与运维排查。"""
    size = _pool_size_for_process()
    workers = max(1, settings.UVICORN_WORKERS)
    return {
        "pool_size_per_worker": size,
        "uvicorn_workers": workers,
        "estimated_max_connections": size * workers,
    }


@contextmanager
def get_conn():
    """
    获取数据库连接的上下文管理器（统一入口，从连接池借还）

    使用示例:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    pool = get_pool()
    conn = pool.connection()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_cursor():
    with get_conn() as conn:
        with conn.cursor() as cur:
            yield cur
            conn.commit()


def execute_query(sql: str, params: Optional[tuple] = None) -> list:
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute_one(sql: str, params: Optional[tuple] = None) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute_update(sql: str, params: Optional[tuple] = None) -> int:
    with get_cursor() as cur:
        return cur.execute(sql, params)


def execute_insert(sql: str, params: Optional[tuple] = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.lastrowid


def execute_transaction(operations: list) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            for sql, params in operations:
                cur.execute(sql, params)
            conn.commit()
            return True


def check_db_ready() -> bool:
    """轻量探活：SELECT 1。"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                row = cur.fetchone()
                return bool(row and row.get("ok") == 1)
    except Exception:
        return False

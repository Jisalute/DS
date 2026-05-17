"""分散式鎖：MySQL GET_LOCK / Redis SET NX（可續期）。"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Generator, Optional

from core.database import get_conn
from core.logging import get_logger
from core.redis_client import get_redis_client

logger = get_logger(__name__)

_RELEASE_LOCK_SQL = "SELECT RELEASE_LOCK(%s)"


@contextmanager
def mysql_named_lock(lock_name: str, timeout_seconds: int = 0) -> Generator[bool, None, None]:
    """
    MySQL 命名鎖（單實例或多 worker 共用同一 MySQL 時有效）。
    timeout_seconds=0 表示不等待；取得鎖才 yield True。
    """
    acquired = False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, timeout_seconds))
                row = cur.fetchone()
                val = list(row.values())[0] if row else 0
                acquired = val == 1
        yield acquired
    finally:
        if acquired:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(_RELEASE_LOCK_SQL, (lock_name,))
            except Exception as e:
                logger.warning("釋放 MySQL 鎖 %s 失敗: %s", lock_name, e)


class RedisRenewableLock:
    """Redis 鎖：TTL 大於典型事務；長事務可背景續期。"""

    def __init__(
        self,
        key: str,
        ttl_seconds: int = 120,
        renew_interval_seconds: int = 30,
    ):
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self.token = str(uuid.uuid4())
        self._redis = get_redis_client()
        self._held = False
        self._stop = threading.Event()
        self._renew_thread: Optional[threading.Thread] = None

    def acquire(self) -> bool:
        if not self._redis:
            return False
        ok = self._redis.set(self.key, self.token, nx=True, ex=self.ttl_seconds)
        if not ok:
            return False
        self._held = True
        self._stop.clear()
        self._renew_thread = threading.Thread(target=self._renew_loop, daemon=True)
        self._renew_thread.start()
        return True

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.renew_interval_seconds):
            if not self._held or not self._redis:
                break
            try:
                current = self._redis.get(self.key)
                if current != self.token:
                    break
                self._redis.expire(self.key, self.ttl_seconds)
            except Exception as e:
                logger.warning("Redis 鎖續期失敗 %s: %s", self.key, e)

    def release(self) -> None:
        self._stop.set()
        if self._renew_thread and self._renew_thread.is_alive():
            self._renew_thread.join(timeout=2)
        if not self._held or not self._redis:
            return
        try:
            pipe = self._redis.pipeline(True)
            pipe.watch(self.key)
            if self._redis.get(self.key) == self.token:
                pipe.multi()
                pipe.delete(self.key)
                pipe.execute()
        except Exception as e:
            logger.warning("釋放 Redis 鎖 %s 失敗: %s", self.key, e)
        finally:
            self._held = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *args: object) -> None:
        self.release()


@contextmanager
def redis_or_mysql_lock(
    redis_key: str,
    mysql_lock_name: str,
    ttl_seconds: int = 120,
) -> Generator[bool, None, None]:
    """優先 Redis；無 Redis 時用 MySQL GET_LOCK。"""
    rlock = RedisRenewableLock(redis_key, ttl_seconds=ttl_seconds)
    if rlock.acquire():
        try:
            yield True
        finally:
            rlock.release()
        return
    with mysql_named_lock(mysql_lock_name, timeout_seconds=0) as acquired:
        yield acquired

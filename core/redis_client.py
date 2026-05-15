"""共用 Redis 客戶端（連線池、下單鎖、全局限流）。"""
from __future__ import annotations

from typing import Optional

import redis

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)

_client: Optional[redis.Redis] = None
_init_attempted = False


def get_redis_client() -> Optional[redis.Redis]:
    """取得 Redis 客戶端；未配置或連線失敗時返回 None（呼叫方降級）。"""
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True
    if not settings.REDIS_ENABLED:
        return None
    try:
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _client.ping()
        logger.info("Redis 已連線 %s:%s db=%s", settings.REDIS_HOST, settings.REDIS_PORT, settings.REDIS_DB)
    except Exception as e:
        logger.warning("Redis 連線失敗，相關功能將降級: %s", e)
        _client = None
    return _client

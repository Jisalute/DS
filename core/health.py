"""健康检查：/health（存活）、/ready（就绪，含数据库）。"""
from fastapi import APIRouter

from core.database import check_db_ready, pool_stats
from core.redis_client import get_redis_client

router = APIRouter(tags=["系统"], include_in_schema=False)


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    db_ok = check_db_ready()
    redis = get_redis_client()
    redis_ok = None
    if redis is not None:
        try:
            redis_ok = redis.ping()
        except Exception:
            redis_ok = False
    body = {
        "status": "ready" if db_ok else "not_ready",
        "checks": {"database": db_ok, "redis": redis_ok},
        "pool": pool_stats(),
    }
    if not db_ok:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=body)
    return body

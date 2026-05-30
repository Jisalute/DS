# core/security_middleware.py — IP 封禁、文档访问控制、财务敏感接口鉴权与限流
import ipaddress
import secrets
from typing import FrozenSet, Optional, Tuple

import jwt
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from core.config import (
    ENVIRONMENT,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    get_admin_api_key,
    parse_csv_ids,
    parse_csv_strings,
    settings,
)
from core.logging import get_logger
from core.rate_limiter import SimpleWindowIPRateLimiter

logger = get_logger(__name__)

OPENAPI_PATH = "/openapi.json"
DOCS_PATHS = frozenset({"/docs", "/redoc", OPENAPI_PATH})

# 需管理员鉴权的写操作路径（POST/PUT/DELETE）
FINANCE_ADMIN_WRITE_PATHS: FrozenSet[str] = frozenset({
    "/api/init",
    "/api/subsidy/points-value/adjust",
    "/api/subsidy/distribute",
    "/api/subsidy/daily-ratio/adjust",
    "/api/subsidy/fund",
    "/api/unilevel/adjust",
    "/api/unilevel/dividend",
    "/api/fund-pools/clear",
    "/api/fund-pools/allocations",
    "/api/fund-pools/distribution-platform-rate",
    "/api/fund-pools/direct-referral-reward-rate",
    "/api/coupons/distribute",
    "/api/coupons/distribute-batch",
    "/api/coupons/exchange",
    "/api/fund-pools/transform-to-coupon",
    "/api/withdraw/merchant-to-bankcard",
})

# 需管理员鉴权的敏感读操作
FINANCE_ADMIN_READ_PATHS: FrozenSet[str] = frozenset({
    "/api/withdraw/merchant-to-bankcard/list",
})

finance_admin_rate_limiter = SimpleWindowIPRateLimiter(
    max_calls=settings.FINANCE_ADMIN_RATE_LIMIT_PER_MIN,
    period_seconds=60.0,
)


def get_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    x_real = (request.headers.get("x-real-ip") or "").strip()
    if x_real:
        return x_real
    if request.client and request.client.host:
        return request.client.host
    return ""


def _is_production() -> bool:
    return (ENVIRONMENT or "").lower() in ("production", "prod")


def _ip_in_blocklist(ip: str) -> bool:
    blocked = parse_csv_strings(settings.IP_BLOCKLIST)
    return bool(ip) and ip in blocked


def _ip_allowed_for_docs(ip: str) -> bool:
    if not _is_production():
        return True
    if ip in ("127.0.0.1", "::1"):
        return True
    allowlist = parse_csv_strings(settings.DOCS_ALLOW_IPS)
    if not allowlist:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        if "/" in entry:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif ip == entry:
            return True
    return False


def _verify_bearer_admin(request: Request) -> Optional[dict]:
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token.startswith("eyJ"):
        return None
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": True},
        )
        user_id = payload.get("user_id")
        if user_id is None:
            return None
        admin_ids = parse_csv_ids(settings.ADMIN_USER_IDS)
        if int(user_id) not in admin_ids:
            return None
        return {"auth_type": "token", "user_id": int(user_id)}
    except jwt.PyJWTError:
        return None


def verify_finance_admin_request(request: Request) -> Tuple[bool, Optional[dict]]:
    """校验 admin_key 或管理员 JWT。供中间件与 Depends 共用。"""
    admin_key = request.query_params.get("admin_key", "").strip()
    expected = get_admin_api_key()
    if admin_key and expected and secrets.compare_digest(admin_key, expected):
        return True, {"auth_type": "admin_key"}

    bearer = _verify_bearer_admin(request)
    if bearer:
        return True, bearer

    return False, None


def is_finance_admin_path(path: str, method: str) -> bool:
    if method == "POST" and path in FINANCE_ADMIN_WRITE_PATHS:
        return True
    if method == "GET" and path in FINANCE_ADMIN_READ_PATHS:
        return True
    return False


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        client_ip = get_client_ip(request)

        if _ip_in_blocklist(client_ip):
            logger.warning("已拒绝封禁 IP 访问: ip=%s path=%s", client_ip, path)
            return JSONResponse(status_code=403, content={"detail": "访问被拒绝"})

        if path in DOCS_PATHS and not _ip_allowed_for_docs(client_ip):
            logger.warning("已拒绝非授权 IP 访问文档: ip=%s path=%s", client_ip, path)
            return JSONResponse(status_code=403, content={"detail": "文档接口仅允许内网或授权 IP 访问"})

        if is_finance_admin_path(path, method):
            if method == "POST" and not finance_admin_rate_limiter.allow(client_ip):
                logger.warning("财务敏感接口限流: ip=%s path=%s", client_ip, path)
                return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})

            ok, auth_info = verify_finance_admin_request(request)
            if not ok:
                logger.warning("财务敏感接口鉴权失败: ip=%s %s %s", client_ip, method, path)
                return JSONResponse(
                    status_code=403,
                    content={"detail": "需要管理员口令(admin_key)或管理员 Bearer Token"},
                )
            request.state.finance_admin = auth_info
            logger.info(
                "财务敏感接口鉴权成功: ip=%s %s %s auth=%s",
                client_ip,
                method,
                path,
                auth_info.get("auth_type") if auth_info else None,
            )

        return await call_next(request)


def setup_security_middleware(app: FastAPI) -> None:
    app.add_middleware(SecurityMiddleware)

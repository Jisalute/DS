# core/middleware.py - 统一中间件配置
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI

from core.config import settings

# 开发环境未配置 CORS_ALLOW_ORIGINS 时的默认前端源（禁止使用 * + credentials）
_DEFAULT_DEV_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]


def resolve_cors_origins() -> tuple[list[str], bool]:
    """返回 (allow_origins, allow_credentials)。始终为显式源列表，避免 allow_origins=['*'] 与 credentials 的非法组合。"""
    raw = (settings.CORS_ALLOW_ORIGINS or "").strip()
    env = (settings.ENVIRONMENT or "").lower()
    is_prod = env in ("production", "prod")
    if raw:
        origins = [x.strip() for x in raw.split(",") if x.strip()]
        if not origins and not is_prod:
            origins = list(_DEFAULT_DEV_CORS_ORIGINS)
        return origins, True
    if is_prod:
        return [], True
    return list(_DEFAULT_DEV_CORS_ORIGINS), True


def setup_cors(app: FastAPI):
    """配置 CORS 中间件（显式 Origin 白名单）"""
    allow_origins, allow_credentials = resolve_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def setup_static_files(app: FastAPI):
    """配置静态文件服务"""
    static_dir = Path("static")
    if static_dir.exists() and static_dir.is_dir():
        try:
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        except Exception as e:
            print(f"⚠️ 静态文件目录挂载失败（可忽略）: {e}")

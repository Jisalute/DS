"""应用启动时的安全与配置校验（任务一：生产环境资金相关配置）"""

from core.config import settings, ENVIRONMENT


def validate_production_safety() -> None:
    """
    生产环境必须显式配置 CORS，且禁止微信 Mock。
    若违反则阻止进程启动（避免误部署）。
    """
    env = (ENVIRONMENT or "").strip().lower()
    if env not in ("production", "prod"):
        return
    if settings.wx_mock_mode_bool:
        raise RuntimeError(
            "生产环境禁止启用 WX_MOCK_MODE：请将 WX_MOCK_MODE 设为 false 并配置真实微信支付参数。"
        )
    if not (settings.CORS_ALLOW_ORIGINS or "").strip():
        raise RuntimeError(
            "生产环境必须在 .env 中设置 CORS_ALLOW_ORIGINS（逗号分隔的完整前端 Origin，"
            "例如 https://your-domain.com）。"
        )

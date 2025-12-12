from fastapi.responses import JSONResponse
import json
from decimal import Decimal
from fastapi.requests import Request as FastAPIRequest
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from typing import Any


class DecimalJSONResponse(JSONResponse):
    """Custom JSONResponse that serializes Decimal to float."""
    def render(self, content: any) -> bytes:
        return json.dumps(content, ensure_ascii=False, default=lambda o: float(o) if isinstance(o, Decimal) else str(o)).encode("utf-8")


async def custom_http_exception_handler(request: FastAPIRequest, exc: StarletteHTTPException):
    return DecimalJSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def validation_exception_handler(request: FastAPIRequest, exc: RequestValidationError):
    return DecimalJSONResponse(status_code=422, content={"detail": exc.errors()})


async def generic_exception_handler(request: FastAPIRequest, exc: Exception):
    return DecimalJSONResponse(status_code=500, content={"detail": str(exc)})


def register_exception_handlers(app: Any) -> None:
    """Register global exception handlers on the FastAPI app."""
    app.add_exception_handler(StarletteHTTPException, custom_http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
    # 全局替换 FastAPI/Starlette 的 JSONResponse 为支持 Decimal 的实现，
    # 这样即便某些中间件或库直接使用 JSONResponse 类也能正确序列化 Decimal。
    try:
        import fastapi.responses as _fastapi_responses
        import starlette.responses as _starlette_responses
        _fastapi_responses.JSONResponse = DecimalJSONResponse
        _starlette_responses.JSONResponse = DecimalJSONResponse
    except Exception:
        # 如果替换失败，不影响异常处理注册
        pass

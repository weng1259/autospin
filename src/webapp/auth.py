"""Web API 的 Bearer token 鉴权与统一错误响应结构。"""
from __future__ import annotations

import secrets
from typing import Literal, TypedDict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp


ErrorSeverity = Literal["warning", "alarm"]


class ErrorDetail(TypedDict):
    """与 L3Error 公共字段对齐的 HTTP 错误详情。"""

    error_code: str
    human_message: str
    agent_message: str
    severity: ErrorSeverity
    recoverable: bool
    suggested_action: str
    suggested_action_zh: str


class ErrorEnvelope(TypedDict):
    """所有 Web 层错误响应的顶层结构。"""

    error: ErrorDetail


def error_envelope(
    *,
    error_code: str,
    human_message: str,
    agent_message: str,
    severity: ErrorSeverity,
    recoverable: bool,
    suggested_action: str,
    suggested_action_zh: str = "",
) -> ErrorEnvelope:
    """构造稳定的 ``{"error": {...}}`` 响应体。"""
    return {
        "error": {
            "error_code": error_code,
            "human_message": human_message,
            "agent_message": agent_message,
            "severity": severity,
            "recoverable": recoverable,
            "suggested_action": suggested_action,
            "suggested_action_zh": suggested_action_zh,
        }
    }


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """除公开健康检查外，对每条请求校验固定 Bearer token。"""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        super().__init__(app)
        self._token = token.encode("utf-8")

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method == "GET" and request.url.path == "/api/health":
            return await call_next(request)

        authorization = request.headers.get("Authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        supplied = credential.encode("utf-8")
        authenticated = (
            separator == " "
            and scheme.lower() == "bearer"
            and secrets.compare_digest(supplied, self._token)
        )
        if authenticated:
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content=error_envelope(
                error_code="L3.AUTHENTICATION_FAILED",
                human_message="缺少或无效的 Bearer token。",
                agent_message=(
                    "Authentication failed. Send the server token in the "
                    "Authorization: Bearer <token> header."
                ),
                severity="warning",
                recoverable=True,
                suggested_action="Retry with the token printed when the server started.",
                suggested_action_zh="使用服务启动时打印的 token 重试。",
            ),
        )

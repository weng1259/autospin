"""FastAPI 应用工厂：注册表注入、鉴权、健康检查与错误处理。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import math

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _json_safe(value: object) -> object:
    """把校验错误 detail 变成 JSON 可序列化：非有限 float 与未知对象转字符串。"""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)
from pydantic import BaseModel

from ..hardware.errors import L3Error, OperationConflictError
from ..schema_export import SCHEMA_VERSION
from .auth import BearerTokenMiddleware, error_envelope
from .estop import register_estop_route
from .gate import (
    OperationGate,
    OperationStatusPoller,
    operation_from_conflict,
    register_operation_routes,
    register_operation_status_routes,
)
from .registry import DeviceRegistry
from .routes_devices import register_device_routes
from .routes_gantry import register_gantry_routes


class HealthResponse(BaseModel):
    """公开健康检查响应。"""

    ok: bool
    version: str
    mock: bool


def create_app(registry: DeviceRegistry, *, token: str) -> FastAPI:
    """为一个注册表创建独立的 Web 应用实例。"""

    if isinstance(registry.poller, OperationStatusPoller):
        # 同一 registry 复用 create_app 时不许双层包装（seq/last_operation
        # 会叠两跳，审查 P2）。
        operation_poller = registry.poller
    else:
        operation_poller = OperationStatusPoller(registry.poller)
        registry.poller = operation_poller
    operation_gate = OperationGate(
        on_completed=operation_poller.record_completed,
    )

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        del app_instance
        registry.poller.start()
        try:
            yield
        finally:
            if not registry.mock:
                # 服务退出即停机是有意的安全语义；mock 模式绝不触碰设备。
                # 必须先于 poller.stop()：卡死设备会让 join 挂住，停机不能
                # 排在它后面被跳过（审查 P1）。失败必须留痕。
                try:
                    registry.estop.halt_all()
                except Exception:
                    logging.getLogger("webapp").exception(
                        "shutdown halt_all failed — 机器可能未停，需人工确认"
                    )
            registry.poller.stop()

    app = FastAPI(
        title="智能旋涂仪 Web API",
        version=SCHEMA_VERSION,
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.operation_gate = operation_gate
    # 急停必须先于后续业务路由和任何未来重逻辑中间件注册。
    register_estop_route(app, registry)
    register_operation_status_routes(app, operation_poller)
    register_operation_routes(app, operation_gate)
    register_gantry_routes(app, registry, operation_gate)
    register_device_routes(app, registry, operation_gate)
    app.add_middleware(BearerTokenMiddleware, token=token)

    @app.exception_handler(OperationConflictError)
    async def handle_operation_conflict(
        request: Request,
        exc: OperationConflictError,
    ) -> JSONResponse:
        del request
        current = operation_from_conflict(exc)
        if current is None:
            current = operation_gate.current()
        content: dict[str, object] = dict(
            error_envelope(
                error_code=exc.error_code,
                human_message=exc.human_message,
                agent_message=exc.agent_message,
                severity=exc.severity,
                recoverable=exc.recoverable,
                suggested_action=exc.suggested_action,
                suggested_action_zh=exc.suggested_action_zh,
            )
        )
        content["current_operation"] = (
            None if current is None else current.model_dump(mode="json")
        )
        return JSONResponse(status_code=409, content=content)

    @app.exception_handler(L3Error)
    async def handle_l3_error(request: Request, exc: L3Error) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                error_code=exc.error_code,
                human_message=exc.human_message,
                agent_message=exc.agent_message,
                severity=exc.severity,
                recoverable=exc.recoverable,
                suggested_action=exc.suggested_action,
                suggested_action_zh=exc.suggested_action_zh,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # 默认 handler 会把违规输入原样回显进响应体——NaN/Infinity 输入会让
        # JSON 编码器在序列化时抛 ValueError 把 422 变 500（审查补丁回归）。
        del request
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                error_code="L3.INTERNAL_ERROR",
                human_message="服务内部错误。",
                agent_message=(
                    "An unexpected server error occurred. Inspect the server logs."
                ),
                severity="alarm",
                recoverable=False,
                suggested_action="Inspect server logs before retrying.",
                suggested_action_zh="查看服务端日志，确认原因后再重试。",
            ),
        )

    @app.get("/api/health")
    def health() -> HealthResponse:
        return HealthResponse(ok=True, version=SCHEMA_VERSION, mock=registry.mock)

    return app

"""龙门 Web 端点：全部硬件访问经全局 operation 门闸。"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..hardware.errors import L3Error
from ..hardware.gantry_backend import GantryBackend
from ..hardware.types import Position
from .auth import error_envelope
from .gate import Operation, OperationGate
from .registry import DeviceRegistry


class EmptyRequest(BaseModel):
    """无参数 POST 的显式请求体。"""

    model_config = ConfigDict(extra="forbid")


class IdempotencyRequest(EmptyRequest):
    """允许调用方复用 backend 幂等键；缺省时使用 operation id。"""

    idempotency_key: str | None = Field(default=None, min_length=1)


class MoveRequest(EmptyRequest):
    """绝对移动请求，字段名保持 Web 卡片约定。"""

    # allow_inf_nan=False：starlette 的 json.loads 接受非标 NaN/Infinity 字面量，
    # 不拦会拿到 202 后才在 L3 层异步失败（审查 P2，Web 边界应同步 422）。
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    z: float = Field(allow_inf_nan=False)
    feed: float = Field(gt=0.0, allow_inf_nan=False)


class JogRequest(EmptyRequest):
    """相对 jog 请求。"""

    axis: str = Field(min_length=1)
    distance: float = Field(allow_inf_nan=False)
    feed: float = Field(gt=0.0, allow_inf_nan=False)


class ZBrakeRequest(EmptyRequest):
    released: bool


class AcceptedOperation(BaseModel):
    """所有被门闸接纳的龙门操作统一响应。"""

    operation_id: str
    accepted: Literal[True] = True


class DeviceNotAttachedError(L3Error):
    """Web 注册表没有接入所请求的设备。"""

    error_code = "L3.DEVICE_NOT_ATTACHED"
    severity = "warning"
    recoverable = True
    suggested_action = "Attach the device backend before retrying."
    suggested_action_zh = "先接入对应设备后端，再重试。"


def _get_gantry(registry: DeviceRegistry) -> GantryBackend:
    gantry = registry.gantry
    if gantry is None:
        raise DeviceNotAttachedError(
            human_message="龙门架后端未接入。",
            agent_message="DeviceRegistry.gantry is not attached.",
        )
    return gantry


def _accepted(operation: Operation) -> AcceptedOperation:
    return AcceptedOperation(operation_id=operation.id)


def register_gantry_routes(
    app: FastAPI,
    registry: DeviceRegistry,
    gate: OperationGate,
) -> None:
    """挂载龙门端点；除独立 ``/api/estop`` 外没有写硬件旁路。"""

    @app.exception_handler(DeviceNotAttachedError)
    async def handle_device_not_attached(
        request: Request,
        exc: DeviceNotAttachedError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=503,
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

    @app.post(
        "/api/gantry/connect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def connect(request: EmptyRequest = EmptyRequest()) -> AcceptedOperation:
        del request
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit("gantry", "connect", lambda _: gantry.connect())
        )

    @app.post(
        "/api/gantry/disconnect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def disconnect(request: EmptyRequest = EmptyRequest()) -> AcceptedOperation:
        del request
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit("gantry", "disconnect", lambda _: gantry.close())
        )

    @app.post(
        "/api/gantry/home",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def home(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit(
                "gantry",
                "home",
                lambda operation: gantry.home(
                    idempotency_key=(
                        request.idempotency_key or operation.id
                    )
                ),
            )
        )

    @app.post(
        "/api/gantry/move",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def move(request: MoveRequest) -> AcceptedOperation:
        gantry = _get_gantry(registry)
        target = Position(x_mm=request.x, y_mm=request.y, z_mm=request.z)
        return _accepted(
            gate.submit(
                "gantry",
                "move",
                lambda _: gantry.move_to(
                    target,
                    feed_mm_min=request.feed,
                ),
            )
        )

    @app.post(
        "/api/gantry/dry-run",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def dry_run(request: MoveRequest) -> AcceptedOperation:
        gantry = _get_gantry(registry)
        target = Position(x_mm=request.x, y_mm=request.y, z_mm=request.z)
        return _accepted(
            gate.submit(
                "gantry",
                "dry-run",
                lambda _: gantry.move_to(
                    target,
                    feed_mm_min=request.feed,
                    dry_run=True,
                ),
            )
        )

    @app.post(
        "/api/gantry/halt",
        response_model=dict,
    )
    def halt(request: EmptyRequest = EmptyRequest()) -> dict:
        del request
        status = _get_gantry(registry).halt()
        payload = (
            status.model_dump(mode="json")
            if hasattr(status, "model_dump")
            else {"value": str(status)}
        )
        return {"halted": True, "status": payload}

    @app.get(
        "/api/gantry/homing-diagnostics",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def homing_diagnostics() -> AcceptedOperation:
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit(
                "gantry",
                "homing-diagnostics",
                lambda _: gantry.get_homing_diagnostics(),
            )
        )

    @app.post(
        "/api/gantry/z-brake",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def z_brake(request: ZBrakeRequest) -> AcceptedOperation:
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit(
                "gantry",
                "z-brake",
                lambda _: gantry.set_z_brake_released(request.released),
            )
        )

    @app.post(
        "/api/gantry/jog",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def jog(request: JogRequest) -> AcceptedOperation:
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit(
                "gantry",
                "jog",
                lambda _: gantry.jog(
                    request.axis,
                    request.distance,
                    request.feed,
                ),
            )
        )

    @app.post(
        "/api/gantry/recover",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def recover(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit(
                "gantry",
                "recover",
                lambda operation: gantry.recover_from_alarm(
                    idempotency_key=(
                        request.idempotency_key or operation.id
                    )
                ),
            )
        )

    @app.get(
        "/api/gantry/grbl-settings",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def validate_grbl_settings() -> AcceptedOperation:
        gantry = _get_gantry(registry)
        return _accepted(
            gate.submit(
                "gantry",
                "grbl-settings",
                lambda _: gantry.validate_grbl_settings(),
            )
        )

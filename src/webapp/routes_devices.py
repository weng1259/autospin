"""外设 Web 端点；仅滑台立即停止绕过全局 operation 门闸。"""

# 安全禁令：``/api/linearstage/stop`` 是与急停同级的立即停止路径，永远不得
# 增加 operation/busy 门闸、应用层锁、排队或后台线程；其余本文件端点全部
# 必须经 OperationGate 后台执行。
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..hardware.errors import L3Error
from ..hardware.gripper_backend import GripperBackend
from ..hardware.heater_backend import HeaterBackend
from ..hardware.linearstage_backend import (
    LinearStageActionResult,
    LinearStageBackend,
)
from ..hardware.pipette_backend import PipetteBackend
from ..hardware.relay_backend import RelayBackend
from ..hardware.spincoater_backend import SpincoaterBackend
from .auth import error_envelope
from .gate import Operation, OperationGate
from .registry import DeviceRegistry


class EmptyRequest(BaseModel):
    """无参数 POST 的显式请求体。"""

    model_config = ConfigDict(extra="forbid")


class IdempotencyRequest(EmptyRequest):
    """允许调用方复用 backend 幂等键；缺省时使用 operation id。"""

    idempotency_key: str | None = Field(default=None, min_length=1)


class HeaterSetSvRequest(IdempotencyRequest):
    """加热台目标温度请求。"""

    sv_c: float = Field(allow_inf_nan=False)


class SpinStartRequest(IdempotencyRequest):
    """旋涂启动请求。"""

    rpm: float = Field(allow_inf_nan=False)


class SpinAccelerationRequest(IdempotencyRequest):
    """旋涂软件加速度设置请求，单位 RPM/s。"""

    rpm_per_s: float = Field(ge=50.0, le=6000.0, allow_inf_nan=False)


class SpinDecelerationRequest(IdempotencyRequest):
    """旋涂软件减速度设置请求，单位 RPM/s。"""

    rpm_per_s: float = Field(ge=50.0, le=6000.0, allow_inf_nan=False)


class SpinStopRequest(IdempotencyRequest):
    """旋涂停止请求；默认使用制动。"""

    use_brake: bool = True


class PipetteVolumeRequest(IdempotencyRequest):
    """移液枪吸液或排液体积请求。"""

    volume_ul: float = Field(allow_inf_nan=False)


class LinearStageMoveRequest(IdempotencyRequest):
    """滑台绝对位置请求。"""

    position_mm: float = Field(allow_inf_nan=False)


class RelayChannelRequest(IdempotencyRequest):
    """通用继电器请求；CH1/CH2 在 Web 边界永久禁用。"""

    channel: int = Field(ge=3, le=8)
    on: bool
    force: bool = False


class AcceptedOperation(BaseModel):
    """所有被门闸接纳的外设操作统一响应。"""

    operation_id: str
    accepted: Literal[True] = True


class DeviceNotAttachedError(L3Error):
    """Web 注册表没有接入所请求的设备。"""

    error_code = "L3.DEVICE_NOT_ATTACHED"
    severity = "warning"
    recoverable = True
    suggested_action = "Attach the device backend before retrying."
    suggested_action_zh = "先接入对应设备后端，再重试。"


def _get_heater(registry: DeviceRegistry) -> HeaterBackend:
    heater = registry.heater
    if heater is None:
        raise DeviceNotAttachedError(
            human_message="加热台后端未接入。",
            agent_message="DeviceRegistry.heater is not attached.",
        )
    return heater


def _get_spincoater(registry: DeviceRegistry) -> SpincoaterBackend:
    spincoater = registry.spincoater
    if spincoater is None:
        raise DeviceNotAttachedError(
            human_message="旋涂后端未接入。",
            agent_message="DeviceRegistry.spincoater is not attached.",
        )
    return spincoater


def _get_pipette(registry: DeviceRegistry) -> PipetteBackend:
    pipette = registry.pipette
    if pipette is None:
        raise DeviceNotAttachedError(
            human_message="移液枪后端未接入。",
            agent_message="DeviceRegistry.pipette is not attached.",
        )
    return pipette


def _get_linear_stage(registry: DeviceRegistry) -> LinearStageBackend:
    linear_stage = registry.linear_stage
    if linear_stage is None:
        raise DeviceNotAttachedError(
            human_message="滑台后端未接入。",
            agent_message="DeviceRegistry.linear_stage is not attached.",
        )
    return linear_stage


def _get_relay(registry: DeviceRegistry) -> RelayBackend:
    relay = registry.relay
    if relay is None:
        raise DeviceNotAttachedError(
            human_message="继电器后端未接入。",
            agent_message="DeviceRegistry.relay is not attached.",
        )
    return relay


def _get_gripper(registry: DeviceRegistry) -> GripperBackend:
    gripper = registry.gripper
    if gripper is None:
        raise DeviceNotAttachedError(
            human_message="夹爪后端未接入。",
            agent_message="DeviceRegistry.gripper is not attached.",
        )
    return gripper


def _accepted(operation: Operation) -> AcceptedOperation:
    return AcceptedOperation(operation_id=operation.id)


def _idempotency_key(
    request: IdempotencyRequest,
    operation: Operation,
) -> str:
    return request.idempotency_key or operation.id


def register_device_routes(
    app: FastAPI,
    registry: DeviceRegistry,
    gate: OperationGate,
) -> None:
    """挂载六类外设端点；滑台 stop 是唯一 operation 门闸旁路。"""

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
        "/api/heater/connect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def heater_connect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        heater = _get_heater(registry)
        return _accepted(
            gate.submit("heater", "connect", lambda _: heater.connect())
        )

    @app.post(
        "/api/heater/disconnect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def heater_disconnect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        heater = _get_heater(registry)
        return _accepted(
            gate.submit("heater", "disconnect", lambda _: heater.close())
        )

    @app.post(
        "/api/heater/set-sv",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def heater_set_sv(request: HeaterSetSvRequest) -> AcceptedOperation:
        heater = _get_heater(registry)
        return _accepted(
            gate.submit(
                "heater",
                "set-sv",
                lambda operation: heater.set_sv(
                    request.sv_c,
                    idempotency_key=_idempotency_key(request, operation),
                ),
            )
        )

    @app.get(
        "/api/heater/pv",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def heater_pv() -> AcceptedOperation:
        heater = _get_heater(registry)
        return _accepted(
            gate.submit("heater", "pv", lambda _: heater.read_pv())
        )

    @app.post(
        "/api/spincoater/connect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_connect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "connect",
                lambda _: spincoater.connect(),
            )
        )

    @app.post(
        "/api/spincoater/disconnect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_disconnect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "disconnect",
                lambda _: spincoater.close(),
            )
        )

    @app.post(
        "/api/spincoater/start",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_start(request: SpinStartRequest) -> AcceptedOperation:
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "start",
                lambda operation: spincoater.start(
                    request.rpm,
                    idempotency_key=_idempotency_key(request, operation),
                ),
            )
        )

    @app.post(
        "/api/spincoater/acceleration",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_acceleration(
        request: SpinAccelerationRequest,
    ) -> AcceptedOperation:
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "set-acceleration",
                lambda _: spincoater.set_acceleration(request.rpm_per_s),
            )
        )

    @app.post(
        "/api/spincoater/deceleration",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_deceleration(
        request: SpinDecelerationRequest,
    ) -> AcceptedOperation:
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "set-deceleration",
                lambda _: spincoater.set_deceleration(request.rpm_per_s),
            )
        )

    @app.post(
        "/api/spincoater/stop",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_stop(
        request: SpinStopRequest = SpinStopRequest(),
    ) -> AcceptedOperation:
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "stop",
                lambda operation: spincoater.stop(
                    use_brake=request.use_brake,
                    idempotency_key=_idempotency_key(request, operation),
                ),
            )
        )

    @app.get(
        "/api/spincoater/fault",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def spincoater_fault() -> AcceptedOperation:
        spincoater = _get_spincoater(registry)
        return _accepted(
            gate.submit(
                "spincoater",
                "fault",
                lambda _: spincoater.read_fault(),
            )
        )

    @app.post(
        "/api/pipette/connect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def pipette_connect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        pipette = _get_pipette(registry)
        return _accepted(
            gate.submit("pipette", "connect", lambda _: pipette.connect())
        )

    @app.post(
        "/api/pipette/disconnect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def pipette_disconnect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        pipette = _get_pipette(registry)
        return _accepted(
            gate.submit("pipette", "disconnect", lambda _: pipette.close())
        )

    @app.post(
        "/api/pipette/home",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def pipette_home(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        pipette = _get_pipette(registry)
        return _accepted(
            gate.submit(
                "pipette",
                "home",
                lambda operation: pipette.home(
                    idempotency_key=_idempotency_key(request, operation)
                ),
            )
        )

    @app.post(
        "/api/pipette/aspirate",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def pipette_aspirate(
        request: PipetteVolumeRequest,
    ) -> AcceptedOperation:
        pipette = _get_pipette(registry)
        return _accepted(
            gate.submit(
                "pipette",
                "aspirate",
                lambda operation: pipette.aspirate(
                    request.volume_ul,
                    idempotency_key=_idempotency_key(request, operation),
                ),
            )
        )

    @app.post(
        "/api/pipette/dispense",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def pipette_dispense(
        request: PipetteVolumeRequest,
    ) -> AcceptedOperation:
        pipette = _get_pipette(registry)
        return _accepted(
            gate.submit(
                "pipette",
                "dispense",
                lambda operation: pipette.dispense(
                    request.volume_ul,
                    idempotency_key=_idempotency_key(request, operation),
                ),
            )
        )

    @app.post(
        "/api/pipette/eject-tip",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def pipette_eject_tip(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        pipette = _get_pipette(registry)
        return _accepted(
            gate.submit(
                "pipette",
                "eject-tip",
                lambda operation: pipette.eject_tip(
                    idempotency_key=_idempotency_key(request, operation)
                ),
            )
        )

    @app.post(
        "/api/linearstage/connect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def linear_stage_connect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        linear_stage = _get_linear_stage(registry)
        return _accepted(
            gate.submit(
                "linear_stage",
                "connect",
                lambda _: linear_stage.connect(),
            )
        )

    @app.post(
        "/api/linearstage/disconnect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def linear_stage_disconnect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        linear_stage = _get_linear_stage(registry)
        return _accepted(
            gate.submit(
                "linear_stage",
                "disconnect",
                lambda _: linear_stage.close(),
            )
        )

    @app.post(
        "/api/linearstage/home",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def linear_stage_home(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        linear_stage = _get_linear_stage(registry)
        return _accepted(
            gate.submit(
                "linear_stage",
                "home",
                lambda operation: linear_stage.home(
                    idempotency_key=_idempotency_key(request, operation)
                ),
            )
        )

    @app.post(
        "/api/linearstage/move",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def linear_stage_move(
        request: LinearStageMoveRequest,
    ) -> AcceptedOperation:
        linear_stage = _get_linear_stage(registry)
        return _accepted(
            gate.submit(
                "linear_stage",
                "move",
                lambda operation: linear_stage.move_to(
                    request.position_mm,
                    idempotency_key=_idempotency_key(request, operation),
                ),
            )
        )

    @app.post(
        "/api/linearstage/stop",
        response_model=LinearStageActionResult,
    )
    def linear_stage_stop(
        request: EmptyRequest = EmptyRequest(),
    ) -> LinearStageActionResult:
        # 有意不读 gate.current()、不 submit：占用期间也必须直达 backend。
        del request
        return _get_linear_stage(registry).stop()

    @app.post(
        "/api/relay/connect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def relay_connect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        relay = _get_relay(registry)
        return _accepted(
            gate.submit("relay", "connect", lambda _: relay.connect())
        )

    @app.post(
        "/api/relay/disconnect",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def relay_disconnect(
        request: EmptyRequest = EmptyRequest(),
    ) -> AcceptedOperation:
        del request
        relay = _get_relay(registry)
        return _accepted(
            gate.submit("relay", "disconnect", lambda _: relay.close())
        )

    @app.post(
        "/api/relay/ch",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def relay_channel(request: RelayChannelRequest) -> AcceptedOperation:
        relay = _get_relay(registry)
        action = "ch-on" if request.on else "ch-off"

        def set_channel(operation: Operation) -> object:
            idempotency_key = _idempotency_key(request, operation)
            if request.force:
                if not relay.is_connected():
                    relay.connect()
                return relay.force_set(
                    request.channel,
                    request.on,
                    idempotency_key=idempotency_key,
                )
            if request.on:
                return relay.ch_on(
                    request.channel,
                    idempotency_key=idempotency_key,
                )
            return relay.ch_off(
                request.channel,
                idempotency_key=idempotency_key,
            )

        return _accepted(gate.submit("relay", action, set_channel))

    @app.post(
        "/api/gripper/open",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def gripper_open(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        gripper = _get_gripper(registry)
        return _accepted(
            gate.submit(
                "gripper",
                "open",
                lambda operation: gripper.open(
                    idempotency_key=_idempotency_key(request, operation)
                ),
            )
        )

    @app.post(
        "/api/gripper/close",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def gripper_close(
        request: IdempotencyRequest = IdempotencyRequest(),
    ) -> AcceptedOperation:
        gripper = _get_gripper(registry)
        return _accepted(
            gate.submit(
                "gripper",
                "close",
                lambda operation: gripper.close(
                    idempotency_key=_idempotency_key(request, operation)
                ),
            )
        )

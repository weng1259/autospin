"""示教-重放 Web 端点。

录制控制只修改内存旗标，列表与删除只访问 routine 文件，因此都不经过
``OperationGate``。重放会真实驱动设备，必须占用门闸。

``POST /api/routines/replay/abort`` 与急停同级：它永远直通、不排队，也不尝试
占用 operation 门闸。端点设置的事件会由 ``RoutinePlayer.abort_check`` 在步骤边界
读取；这样即使门闸正被重放占用，中止请求仍能到达 player。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
from typing import Any, NoReturn, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..hardware.errors import L3Error
from ..routine import (
    DEFAULT_ROUTINES_DIR,
    PlayResult,
    RecordingProxy,
    Routine,
    RoutinePlayer,
    RoutineRecorder,
    RoutineStep,
)
from .gate import Operation, OperationGate
from .registry import DeviceRegistry
from .routes_gantry import AcceptedOperation


class EmptyRoutineRequest(BaseModel):
    """不接受额外字段的空 POST 请求。"""

    model_config = ConfigDict(extra="forbid")


class ArmRecordingRequest(EmptyRoutineRequest):
    """开始示教录制时提供的人类可读程序名。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)


class RoutineStepSummary(BaseModel):
    """录制状态里最近一步的紧凑摘要。"""

    seq: int
    device: str
    action: str
    label: str
    t_offset_s: float


class RecordingStatus(BaseModel):
    """当前内存 recorder 的只读快照。"""

    armed: bool
    name: str
    step_count: int
    recent_steps: list[RoutineStepSummary]


class RoutineSummary(BaseModel):
    """程序列表的一行。"""

    name: str
    step_count: int
    duration_s: float
    has_motion: bool


class DisarmRecordingResponse(BaseModel):
    """停止录制并完成原子存盘后的响应。"""

    recording: RecordingStatus
    saved: RoutineSummary


class DeleteRoutineResponse(BaseModel):
    """删除程序文件的确认响应。"""

    deleted: bool
    name: str


class AbortReplayResponse(BaseModel):
    """直通中止通道是否找到正在运行的重放。"""

    abort_requested: bool
    operation_id: str | None


class RoutineReplayPrerequisiteError(L3Error):
    """RoutinePlayer 的归零安全闸拒绝了含运动程序。"""

    error_code = "L3.ROUTINE_MACHINE_NOT_HOMED"
    severity = "warning"
    recoverable = True
    suggested_action = "Home the gantry, then replay the routine again."
    suggested_action_zh = "先完成龙门归零，再重新重放程序。"


class RoutineReplayAbortedError(L3Error):
    """用户通过直通端点中止了重放。"""

    error_code = "L3.ROUTINE_REPLAY_ABORTED"
    severity = "warning"
    recoverable = True
    suggested_action = "Inspect device state before starting another operation."
    suggested_action_zh = "确认各设备当前状态后，再开始下一项操作。"


class RoutineReplayFailedError(L3Error):
    """RoutinePlayer 报告某一步执行失败。"""

    error_code = "L3.ROUTINE_REPLAY_FAILED"
    severity = "alarm"
    recoverable = True
    suggested_action = "Inspect the failed step and device state before retrying."
    suggested_action_zh = "检查失败步骤和对应设备状态，排除原因后再重试。"


class _ReplayAbortController:
    """每个 FastAPI app 独立的一份重放中止信号。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operation_id: str | None = None
        self._event: threading.Event | None = None

    def begin(self, operation_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._operation_id = operation_id
            self._event = event
        return event

    def request_abort(self) -> tuple[bool, str | None]:
        with self._lock:
            event = self._event
            operation_id = self._operation_id
            if event is None:
                return False, None
            event.set()
            return True, operation_id

    def finish(self, operation_id: str) -> None:
        with self._lock:
            if self._operation_id == operation_id:
                self._operation_id = None
                self._event = None


def _safe_filename(name: str) -> str:
    """保持与 ``routine.py`` 文件命名规则一致，同时把路径字符关在目录内。"""

    keep = "-_."
    cleaned = "".join(
        character if (character.isalnum() or character in keep) else "_"
        for character in name
    ).strip("_")
    return cleaned or "routine"


def _routine_path(base_dir: Path, name: str) -> Path:
    return base_dir / f"{_safe_filename(name)}.json"


def _atomic_write_routine(routine: Routine, base_dir: Path) -> Path:
    """把 JSON 写到同目录临时文件，再以 ``os.replace`` 原子替换。"""

    base_dir.mkdir(parents=True, exist_ok=True)
    destination = _routine_path(base_dir, routine.name)
    temporary = destination.with_name(
        f".{destination.name}.{uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(routine.to_json(), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


_LOGGER = logging.getLogger("webapp.routines")


def _load_routine(base_dir: Path, name: str) -> Routine:
    path = _routine_path(base_dir, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="routine not found")
    try:
        return Routine.load(path)
    except Exception as exc:
        # 程序文件是"人可读、可手改"的 JSON——手改笔误不许把单读炸成 500
        # （审查 P1：坏文件曾同时打挂列表与删除）。
        raise HTTPException(
            status_code=422,
            detail=f"routine file is corrupt or unreadable: {exc}",
        ) from exc


def _step_summary(step: RoutineStep) -> RoutineStepSummary:
    return RoutineStepSummary(
        seq=step.seq,
        device=step.device,
        action=step.action,
        label=step.label,
        t_offset_s=step.t_offset_s,
    )


def _recording_status(recorder: RoutineRecorder) -> RecordingStatus:
    steps = recorder.steps
    return RecordingStatus(
        armed=recorder.is_armed,
        name=recorder.name,
        step_count=len(steps),
        recent_steps=[_step_summary(step) for step in steps[-5:]],
    )


def _routine_summary(routine: Routine) -> RoutineSummary:
    duration_s = max(
        (step.t_offset_s for step in routine.steps),
        default=0.0,
    )
    return RoutineSummary(
        name=routine.name,
        step_count=len(routine.steps),
        duration_s=round(duration_s, 3),
        has_motion=routine.has_motion,
    )


def _unwrapped(backend: object | None) -> object | None:
    if isinstance(backend, RecordingProxy):
        return cast(object, backend.unwrapped)
    return backend


def _player_backends(registry: DeviceRegistry) -> dict[str, Any]:
    spincoater = _unwrapped(registry.spincoater)
    candidates: dict[str, object | None] = {
        "gantry": _unwrapped(registry.gantry),
        "relay": _unwrapped(registry.relay),
        "gripper": _unwrapped(registry.gripper),
        "heater": _unwrapped(registry.heater),
        "spin": spincoater,
        # 接受人工编辑文件里更直观的设备名，同时 recorder 仍写 routine.py
        # 原生约定的 ``spin``。
        "spincoater": spincoater,
        "pipette": _unwrapped(registry.pipette),
        "linear_stage": _unwrapped(registry.linear_stage),
    }
    return {
        device: backend
        for device, backend in candidates.items()
        if backend is not None
    }


def _write_operation_progress(
    gate: OperationGate,
    operation_id: str,
    *,
    steps_completed: int,
    steps_total: int,
    step_label: str,
) -> None:
    """在 ``gate.py`` 禁改前提下，持其锁更新当前 operation 的进度记录。

    OperationGate 暂无公开 progress API；直接复用它自己的锁，确保状态查询永远
    看不到半写入 payload。W3.6 只把进度放在既有 ``Operation.result`` 字段，
    不改变 operation 形状。
    """

    payload: dict[str, object] = {
        "steps_completed": steps_completed,
        "steps_total": steps_total,
        "step_label": step_label,
    }
    with gate._lock:
        current = gate._current
        if current is not None and current.id == operation_id:
            current.result = payload


def _raise_replay_failure(routine: Routine, result: PlayResult) -> NoReturn:
    if result.aborted:
        raise RoutineReplayAbortedError(
            result.error or "程序重放已被用户中止。",
            "Routine replay was aborted by the direct abort endpoint.",
        )
    if (
        routine.has_motion
        and result.steps_completed == 0
        and "未归零" in result.error
    ):
        raise RoutineReplayPrerequisiteError(
            result.error,
            "RoutinePlayer rejected a motion routine because gantry.is_homed() is false.",
        )
    raise RoutineReplayFailedError(
        result.error or "程序重放失败。",
        result.error or "RoutinePlayer reported an unsuccessful replay.",
    )


def register_routine_routes(
    app: FastAPI,
    registry: DeviceRegistry,
    gate: OperationGate,
    *,
    base_dir: str | Path = DEFAULT_ROUTINES_DIR,
) -> None:
    """挂载录制、程序文件与重放端点。"""

    routines_path = Path(base_dir)
    recorder = registry.routine_recorder
    abort_controller = _ReplayAbortController()
    app.state.routines_path = routines_path
    app.state.routine_abort_controller = abort_controller

    @app.post(
        "/api/routines/record/arm",
        response_model=RecordingStatus,
    )
    def arm_recording(request: ArmRecordingRequest) -> RecordingStatus:
        recorder.arm(request.name)
        return _recording_status(recorder)

    @app.post(
        "/api/routines/record/disarm",
        response_model=DisarmRecordingResponse,
    )
    def disarm_recording(
        request: EmptyRoutineRequest = EmptyRoutineRequest(),
    ) -> DisarmRecordingResponse:
        del request
        recorder.disarm()
        routine = recorder.to_routine()
        _atomic_write_routine(routine, routines_path)
        return DisarmRecordingResponse(
            recording=_recording_status(recorder),
            saved=_routine_summary(routine),
        )

    @app.get(
        "/api/routines/record",
        response_model=RecordingStatus,
    )
    def get_recording_status() -> RecordingStatus:
        return _recording_status(recorder)

    @app.get(
        "/api/routines",
        response_model=list[RoutineSummary],
    )
    def get_routines() -> list[RoutineSummary]:
        routines_path.mkdir(parents=True, exist_ok=True)
        summaries: list[RoutineSummary] = []
        for path in sorted(routines_path.glob("*.json")):
            try:
                summaries.append(_routine_summary(Routine.load(path)))
            except Exception:
                # 一个坏文件不许打挂整个列表（审查 P1）；留日志，
                # 文件仍可按文件名 DELETE 清理。
                _LOGGER.warning("跳过无法解析的程序文件 %s", path, exc_info=True)
        return summaries

    @app.post(
        "/api/routines/replay/abort",
        response_model=AbortReplayResponse,
    )
    def abort_replay(
        request: EmptyRoutineRequest = EmptyRoutineRequest(),
    ) -> AbortReplayResponse:
        del request
        requested, operation_id = abort_controller.request_abort()
        return AbortReplayResponse(
            abort_requested=requested,
            operation_id=operation_id,
        )

    @app.post(
        "/api/routines/{name}/replay",
        status_code=202,
        response_model=AcceptedOperation,
    )
    def replay_routine(
        name: str,
        request: EmptyRoutineRequest = EmptyRoutineRequest(),
    ) -> AcceptedOperation:
        del request
        routine = _load_routine(routines_path, name)
        backends = _player_backends(registry)
        operation = gate.try_start("routine", "replay")
        abort_event = abort_controller.begin(operation.id)
        _write_operation_progress(
            gate,
            operation.id,
            steps_completed=0,
            steps_total=len(routine.steps),
            step_label="",
        )

        def run_player(_: Operation) -> dict[str, object]:
            last_label = ""

            def publish_progress(
                completed: int,
                total: int,
                step: RoutineStep,
            ) -> None:
                nonlocal last_label
                last_label = step.label
                _write_operation_progress(
                    gate,
                    operation.id,
                    steps_completed=completed,
                    steps_total=total,
                    step_label=step.label,
                )

            def abort_aware_sleep(seconds: float) -> None:
                # wait 步必须能被中止打断：player 只在步骤边界查 abort_check，
                # 裸 time.sleep 会让"保温 300s"步内的中止再拖满整步（审查 P1）。
                abort_event.wait(seconds)

            try:
                player = RoutinePlayer(backends, sleep=abort_aware_sleep)
                result = player.run(
                    routine,
                    progress_cb=publish_progress,
                    abort_check=abort_event.is_set,
                )
                if not result.success:
                    _raise_replay_failure(routine, result)
                payload: dict[str, object] = result.model_dump(mode="json")
                payload["step_label"] = last_label
                return payload
            finally:
                abort_controller.finish(operation.id)

        try:
            gate.run(operation, run_player)
        except BaseException:
            abort_controller.finish(operation.id)
            raise
        return AcceptedOperation(operation_id=operation.id)

    @app.get(
        "/api/routines/{name}",
        response_model=Routine,
    )
    def get_routine(name: str) -> Routine:
        return _load_routine(routines_path, name)

    @app.delete(
        "/api/routines/{name}",
        response_model=DeleteRoutineResponse,
    )
    def delete_routine(name: str) -> DeleteRoutineResponse:
        path = _routine_path(routines_path, name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="routine not found")
        # 不先 load：坏文件也必须能删，否则只能 SSH 手删（审查 P1）。
        path.unlink()
        return DeleteRoutineResponse(deleted=True, name=name)

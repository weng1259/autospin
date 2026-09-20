"""全局 operation 门闸、后台执行记录与状态快照桥接。"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
import json
import threading
from typing import Literal, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing_extensions import TypedDict

from ..hardware.errors import L3Error, OperationConflictError
from .poller import PollerLike

_LOGGER = logging.getLogger("webapp.gate")


OperationStatus = Literal["running", "succeeded", "failed"]


class OperationError(BaseModel):
    """后台 operation 失败时保存的 Agent-readable 错误。"""

    error_code: str
    human_message: str
    agent_message: str
    severity: Literal["warning", "alarm"]
    recoverable: bool
    suggested_action: str
    suggested_action_zh: str


class Operation(BaseModel):
    """一个被全局门闸接纳的硬件操作。"""

    id: str
    device: str
    action: str
    status: OperationStatus
    started_at: datetime
    completed_at: datetime | None = None
    elapsed: float = 0.0
    result: object | None = None
    error: OperationError | None = None


class OperationStatusSnapshot(TypedDict):
    """W3.1 状态快照加最近一次完成 operation。"""

    seq: int
    ts: str
    devices: dict[str, dict[str, object]]
    last_operation: dict[str, object] | None


OperationTarget = Callable[[Operation], object]
OperationCompletedCallback = Callable[[Operation], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed(operation: Operation, now: datetime) -> float:
    end = operation.completed_at if operation.completed_at is not None else now
    return max(0.0, (end - operation.started_at).total_seconds())


def _l3_error_detail(exc: L3Error) -> OperationError:
    return OperationError(
        error_code=exc.error_code,
        human_message=exc.human_message,
        agent_message=exc.agent_message,
        severity=exc.severity,
        recoverable=exc.recoverable,
        suggested_action=exc.suggested_action,
        suggested_action_zh=exc.suggested_action_zh,
    )


def _internal_error_detail() -> OperationError:
    return OperationError(
        error_code="L3.INTERNAL_ERROR",
        human_message="服务内部错误。",
        agent_message=(
            "An unexpected server error occurred while running the operation. "
            "Inspect the server logs."
        ),
        severity="alarm",
        recoverable=False,
        suggested_action="Inspect server logs before retrying.",
        suggested_action_zh="查看服务端日志，确认原因后再重试。",
    )


def _estop_error_detail() -> OperationError:
    return OperationError(
        error_code="L3.OPERATION_ABORTED_BY_ESTOP",
        human_message="操作已被紧急停止终止。",
        agent_message="The running operation was aborted after the system emergency stop completed.",
        severity="alarm",
        recoverable=True,
        suggested_action="Verify that the machine is safe, then start a new operation.",
        suggested_action_zh="确认设备已处于安全状态后，再发起新的操作。",
    )


def operation_from_conflict(exc: OperationConflictError) -> Operation | None:
    """取出 ``try_start`` 固化在冲突异常上的当前 operation 快照。"""

    value: object = getattr(exc, "current_operation", None)
    return value if isinstance(value, Operation) else None


class OperationGate:
    """全机唯一 operation 门闸。

    v1 有意采用全局一次一个 operation，而不按设备并行：这是一台机器、一个
    操作者的调试面板，避免跨设备动作互撞比提高吞吐更重要。内部锁只保护
    test-and-set 与几行内存记录，绝不跨 backend / 硬件调用持有。
    """

    def __init__(
        self,
        *,
        history_size: int = 50,
        on_completed: OperationCompletedCallback | None = None,
    ) -> None:
        if history_size <= 0:
            raise ValueError("history_size must be greater than zero")
        self._lock = threading.Lock()
        self._current: Operation | None = None
        self._history: deque[Operation] = deque(maxlen=history_size)
        self._launched: set[str] = set()
        self._on_completed = on_completed

    def try_start(self, device: str, action: str) -> Operation:
        """原子占用全局门闸；已有 operation 时抛结构化冲突错误。"""

        with self._lock:
            if self._current is not None:
                current = self._snapshot(self._current)
                exc = OperationConflictError(
                    human_message=(
                        f"机器正忙：{current.device} · {current.action}"
                    ),
                    agent_message=(
                        "Operation gate is occupied by "
                        f"operation_id={current.id}, device={current.device}, "
                        f"action={current.action}, elapsed={current.elapsed:.3f}s."
                    ),
                )
                # errors.py 是任务卡禁改文件；把冲突时刻的稳定快照挂在既有
                # OperationConflictError 实例上，避免 handler 查询时的完成竞态。
                setattr(exc, "current_operation", current)
                raise exc

            operation = Operation(
                id=str(uuid4()),
                device=device,
                action=action,
                status="running",
                started_at=_now(),
            )
            self._current = operation
            return operation.model_copy(deep=True)

    def run(self, operation: Operation, target: OperationTarget) -> None:
        """为已占用门闸的 operation 启动一个 daemon 后台线程。"""

        with self._lock:
            current = self._current
            if current is None or current.id != operation.id:
                raise ValueError("operation is not the current gate owner")
            if operation.id in self._launched:
                raise ValueError("operation background thread already launched")
            self._launched.add(operation.id)
            target_operation = current.model_copy(deep=True)

        thread = threading.Thread(
            target=self._execute,
            args=(operation.id, target_operation, target),
            name=f"Operation-{operation.device}-{operation.action}-{operation.id[:8]}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            _LOGGER.exception(
                "operation %s %s/%s thread failed to start",
                operation.id, operation.device, operation.action,
            )
            self._complete(operation.id, result=None, error=_internal_error_detail())
            raise

    def submit(
        self,
        device: str,
        action: str,
        target: OperationTarget,
    ) -> Operation:
        """原子接纳并立即把 operation 交给独立后台线程执行。"""

        operation = self.try_start(device, action)
        self.run(operation, target)
        return operation

    def current(self) -> Operation | None:
        """返回当前 operation 的只读快照；空闲时返回 ``None``。"""

        with self._lock:
            if self._current is None:
                return None
            return self._snapshot(self._current)

    def get(self, operation_id: str) -> Operation | None:
        """按 id 查询当前或最近 50 条已完成 operation。"""

        with self._lock:
            if self._current is not None and self._current.id == operation_id:
                return self._snapshot(self._current)
            for operation in reversed(self._history):
                if operation.id == operation_id:
                    return operation.model_copy(deep=True)
        return None

    def abort_current_after_estop(self) -> Operation | None:
        """急停完成后终止当前记录并释放门闸。

        Python 无法强制结束正在执行硬件调用的线程，因此迟到的线程结果会由
        ``_complete`` 按 operation id 忽略。此方法只能在系统急停已经返回后调用，
        避免把尚未执行安全停机的设备提前标记为空闲。
        """

        with self._lock:
            operation = self._current
            if operation is None:
                return None

            completed_at = _now()
            operation.completed_at = completed_at
            operation.elapsed = _elapsed(operation, completed_at)
            operation.result = None
            operation.error = _estop_error_detail()
            operation.status = "failed"
            completed = operation.model_copy(deep=True)
            self._history.append(completed)
            self._current = None
            self._launched.discard(operation.id)

            if self._on_completed is not None:
                try:
                    self._on_completed(completed.model_copy(deep=True))
                except Exception:
                    _LOGGER.warning(
                        "operation estop completion callback failed",
                        exc_info=True,
                    )
            return completed

    @staticmethod
    def _snapshot(operation: Operation) -> Operation:
        return operation.model_copy(
            update={"elapsed": _elapsed(operation, _now())},
            deep=True,
        )

    def _execute(
        self,
        operation_id: str,
        operation: Operation,
        target: OperationTarget,
    ) -> None:
        try:
            result = target(operation)
        except L3Error as exc:
            # L3Error 是预期内的结构化失败，warning 级留痕即可。
            _LOGGER.warning(
                "operation %s %s/%s failed: %s",
                operation_id, operation.device, operation.action,
                exc.agent_message,
            )
            self._complete(
                operation_id,
                result=None,
                error=_l3_error_detail(exc),
            )
        except BaseException:
            # 错误详情告诉用户 "inspect the server logs"——栈必须真的进日志
            # （审查 P1：吞异常零日志 = 线上唯一诊断线索是一条自指空日志的记录）。
            _LOGGER.exception(
                "operation %s %s/%s crashed",
                operation_id, operation.device, operation.action,
            )
            self._complete(
                operation_id,
                result=None,
                error=_internal_error_detail(),
            )
        else:
            serializable_result: object = (
                result.model_dump(mode="json")
                if isinstance(result, BaseModel)
                else result
            )
            self._complete(
                operation_id,
                result=serializable_result,
                error=None,
            )

    def _complete(
        self,
        operation_id: str,
        *,
        result: object | None,
        error: OperationError | None,
    ) -> None:
        with self._lock:
            operation = self._current
            if operation is None or operation.id != operation_id:
                self._launched.discard(operation_id)
                return

            completed_at = _now()
            operation.completed_at = completed_at
            operation.elapsed = _elapsed(operation, completed_at)
            operation.result = result
            operation.error = error
            operation.status = "failed" if error is not None else "succeeded"
            completed = operation.model_copy(deep=True)
            self._history.append(completed)
            self._current = None
            self._launched.discard(operation_id)

            # 回调只允许写内存快照；仍在本锁内调用可保证连续 operation 的完成
            # 事件不会倒序。异常不能让已经完成的门闸重新占用。
            if self._on_completed is not None:
                try:
                    self._on_completed(completed.model_copy(deep=True))
                except Exception:
                    _LOGGER.warning(
                        "operation completion callback failed", exc_info=True
                    )


class OperationStatusPoller:
    """给 W3.1 poller 叠加 ``last_operation``（纯组合实现 PollerLike，
    不继承 StatusPoller——继承而不初始化基类是隐性雷，审查 P2）。"""

    def __init__(self, delegate: PollerLike) -> None:
        self._delegate = delegate
        self._operation_lock = threading.Lock()
        self._last_operation: Operation | None = None
        self._completion_seq = 0

    def record_completed(self, operation: Operation) -> None:
        """写入完成事件，并使下一帧 SSE 的 ``seq`` 立即前进。"""

        with self._operation_lock:
            self._last_operation = operation.model_copy(deep=True)
            self._completion_seq += 1

    def start(self) -> None:
        self._delegate.start()

    def stop(self) -> None:
        self._delegate.stop()

    def is_running(self) -> bool:
        return self._delegate.is_running()

    def snapshot(self) -> OperationStatusSnapshot:
        base = self._delegate.snapshot()
        with self._operation_lock:
            operation = (
                None
                if self._last_operation is None
                else self._last_operation.model_copy(deep=True)
            )
            completion_seq = self._completion_seq

        snapshot: dict[str, object] = dict(base)
        snapshot["seq"] = base["seq"] + completion_seq
        snapshot["last_operation"] = (
            None if operation is None else operation.model_dump(mode="json")
        )
        if operation is not None and operation.completed_at is not None:
            completed_at = operation.completed_at.isoformat()
            if completed_at > base["ts"]:
                snapshot["ts"] = completed_at
        return cast(OperationStatusSnapshot, snapshot)

    async def stream(self, request: Request) -> AsyncIterator[str]:
        """流式输出轮询变化或 operation 完成变化。"""

        last_seq = -1
        while not await request.is_disconnected():
            snapshot = self.snapshot()
            if snapshot["seq"] != last_seq:
                yield (
                    "data: "
                    + json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n\n"
                )
                last_seq = snapshot["seq"]
            await asyncio.sleep(0.05)


def register_operation_routes(app: FastAPI, gate: OperationGate) -> None:
    """挂载当前 operation 与历史查询端点。"""

    @app.get("/api/operations/current", response_model=Operation | None)
    def get_current_operation() -> Operation | None:
        return gate.current()

    @app.get("/api/operations/{operation_id}", response_model=Operation)
    def get_operation(operation_id: str) -> Operation:
        operation = gate.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return operation


def register_operation_status_routes(
    app: FastAPI,
    poller: OperationStatusPoller,
) -> None:
    """挂载保留 ``last_operation`` 的状态与 SSE 端点。"""

    @app.get("/api/status", response_model=OperationStatusSnapshot)
    def get_status() -> OperationStatusSnapshot:
        return poller.snapshot()

    @app.get("/api/status/stream")
    async def stream_status(request: Request) -> StreamingResponse:
        return StreamingResponse(
            poller.stream(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
class OperationExecutionError(L3Error):
    """Expected background-operation failure safe to expose in the UI."""

    error_code = "L3.OPERATION_EXECUTION_FAILED"
    severity = "alarm"
    recoverable = True
    suggested_action = "Check the operation detail and device state, then retry."
    suggested_action_zh = "检查失败详情及设备状态，恢复后重新运行。"

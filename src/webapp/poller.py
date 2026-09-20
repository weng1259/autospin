"""设备只读快照轮询与 SSE 状态流。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse

    from .registry import DeviceRegistry

_LOGGER = logging.getLogger("webapp.poller")


class PollerLike(Protocol):
    """状态源协议：路由/组合根只依赖这五个方法。

    OperationStatusPoller（gate.py）按此协议做纯组合包装，不继承
    StatusPoller——继承而不初始化基类是隐性 AttributeError 雷（审查 P2）。
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def snapshot(self) -> Mapping[str, Any]: ...
    def stream(self, request: Request) -> AsyncIterator[str]: ...


class StatusSnapshot(TypedDict):
    """一次完整轮询产生的 JSON-safe 内存快照。"""

    seq: int
    ts: str
    devices: dict[str, dict[str, object]]


StatusReader = Callable[[], BaseModel]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class StatusPoller:
    """后台轮询各 backend 的只读缓存方法，不在 Web 读路径访问硬件。"""

    def __init__(self, registry: DeviceRegistry, *, interval_s: float = 0.5) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be greater than zero")
        self._interval_s = interval_s
        self._readers = self._build_readers(registry)
        self._snapshot_lock = threading.Lock()
        self._snapshot: StatusSnapshot = {
            "seq": 0,
            "ts": _timestamp(),
            "devices": {},
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _build_readers(
        registry: DeviceRegistry,
    ) -> tuple[tuple[str, StatusReader], ...]:
        readers: list[tuple[str, StatusReader]] = []
        if registry.gantry is not None:
            readers.append(("gantry", registry.gantry.get_status))
        if registry.relay is not None:
            readers.append(("relay", registry.relay.get_state))
        if registry.gripper is not None:
            readers.append(("gripper", registry.gripper.get_state))
        if registry.heater is not None:
            readers.append(("heater", registry.heater.status))
        if registry.spincoater is not None:
            readers.append(("spincoater", registry.spincoater.status))
        if registry.pipette is not None:
            readers.append(("pipette", registry.pipette.status))
        if registry.linear_stage is not None:
            readers.append(("linear_stage", registry.linear_stage.status))
        return tuple(readers)

    def start(self) -> None:
        """启动单个 daemon 轮询线程；重复调用不重复创建。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="WebStatusPoller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """请求轮询退出并**有界**等待线程结束。

        join 必须带超时：某设备 status() 卡死在串口读时线程停不下来，
        不能让它拖住进程退出、更不能挡住 shutdown 停机（审查 P1）。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                _LOGGER.warning(
                    "status poller 2s 内未退出（大概率某设备 status() 阻塞在"
                    "串口读），放弃等待，daemon 线程随进程回收"
                )
        self._thread = None

    def is_running(self) -> bool:
        """返回生命周期线程是否仍存活。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self) -> StatusSnapshot:
        """只复制带锁内存快照；不调用任何 backend。"""
        with self._snapshot_lock:
            return deepcopy(self._snapshot)

    async def stream(self, request: Request) -> AsyncIterator[str]:
        """按 seq 变化输出 SSE 帧，客户端断开或任务取消即退出。

        这里选择短间隔轮询 seq：每个 SSE 客户端不创建线程，也不需要从 poller
        线程跨 event loop 投递 asyncio.Queue；读路径始终只碰内存快照。
        """
        last_seq = -1
        check_interval_s = min(self._interval_s, 0.05)
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
            await asyncio.sleep(check_interval_s)

    def _run(self) -> None:
        while not self._stop.is_set():
            devices: dict[str, dict[str, object]] = {}
            for name, reader in self._readers:
                try:
                    value = reader()
                    devices[name] = cast(
                        dict[str, object],
                        value.model_dump(mode="json"),
                    )
                except Exception as exc:  # 每台设备独立隔离，poller 不得死亡
                    devices[name] = {"error": str(exc) or type(exc).__name__}

            with self._snapshot_lock:
                self._snapshot = {
                    "seq": self._snapshot["seq"] + 1,
                    "ts": _timestamp(),
                    "devices": devices,
                }
            self._stop.wait(self._interval_s)


def register_status_routes(app: FastAPI, registry: DeviceRegistry) -> None:
    """把纯内存状态端点挂到指定应用实例。"""
    from fastapi import Request
    from fastapi.responses import StreamingResponse

    @app.get("/api/status")
    def get_status() -> Mapping[str, Any]:
        # PollerLike 协议返回 Mapping：真实类型是 StatusSnapshot 或带
        # last_operation 的 OperationStatusSnapshot（组合包装后）。
        return registry.poller.snapshot()

    @app.get("/api/status/stream")
    async def stream_status(request: Request) -> StreamingResponse:
        return StreamingResponse(
            registry.poller.stream(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

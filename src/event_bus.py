"""内存 pub/sub —— 给 Streamlit 实时面板和 Agent 长连接订阅事件。

设计要点：
- 进程内事件总线（不跨进程）。Phase 4+ 如需多进程，改用 redis pub/sub。
- 每个订阅者拿一个独立的 `queue.Queue`，订阅期间所有 publish 入队。
- `recent(n)` 返回最近 N 个事件用于 UI 首次渲染。
- 完全 thread-safe；publish 在 backend 线程，subscribe 在 UI 线程。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Event:
    """一条结构化事件。

    `phase` 取 `"started" | "completed" | "error" | "progress"`。
    `runlog` 只持久化 `completed` 和 `error`；`started/progress` 仅入 event_bus
    供 UI 实时显示。
    """

    event_id: str
    method: str
    phase: str
    timestamp: float = field(default_factory=time.time)
    params: dict[str, Any] = field(default_factory=dict)
    result: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_agent_message: Optional[str] = None
    duration_ms: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())


class EventBus:
    def __init__(self, history_max: int = 200, queue_max: int = 200):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event]] = []
        self._recent: deque[Event] = deque(maxlen=history_max)
        self._queue_max = queue_max

    def publish(self, event: Event) -> None:
        with self._lock:
            self._recent.append(event)
            dead: list[queue.Queue[Event]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # subscriber too slow; drop oldest in their queue
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except (queue.Empty, queue.Full):
                        dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def subscribe(self) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue(maxsize=self._queue_max)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def recent(self, limit: int = 50) -> list[Event]:
        with self._lock:
            return list(self._recent)[-limit:]


# 进程级单例（所有 backend 和 UI 共用同一个 bus）
EVENT_BUS = EventBus()

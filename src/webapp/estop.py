"""Web 急停路由：鉴权后无锁直达 SystemEstop。"""

# 安全禁令：此路由永远不得增加 operation/busy 门闸、应用层锁、队列或幂等
# 缓存；handler 必须无条件直达 registry.estop.halt_all()。
# 另一条同级禁令：webapp 里不得新增会长时间阻塞的 sync handler——所有 sync
# handler 与本端点共享 anyio 默认 40 线程池，堆满即饿死急停（长活全部走
# gate 的后台线程或 async def）。
from __future__ import annotations

from fastapi import FastAPI

from ..system_estop import EstopReport
from .gate import OperationGate
from .registry import DeviceRegistry


def register_estop_route(
    app: FastAPI,
    registry: DeviceRegistry,
    gate: OperationGate,
) -> None:
    """在其它业务路由与后续门闸之前注册独立急停端点。"""

    @app.post("/api/estop", response_model=EstopReport)
    def emergency_stop() -> EstopReport:
        abort_event = getattr(app.state, "experiment_abort_event", None)
        if abort_event is not None:
            abort_event.set()
        report = registry.estop.halt_all()
        gate.abort_current_after_estop()
        return report

"""系统级急停（W2）：一次调用停住全部运动设备，再处置热源。

设计不变量（源自 2026-07-16 师兄测试版审计的反面教材）：

- **顺序硬编码**：先停一切运动（龙门 → 旋涂 → 滑台 → 移液），最后才动
  加热台。测试版 `stop_experiment` 先全通道断继电器（Z 还在动就合抱闸）
  的顺序是事故路径，这里反过来。
- **每步独立隔离**：一台设备失联/抛错，绝不能挡住后面设备的急停；
  所有异常收进报告，最后一起看。
- **夹爪明确释放**：在 Gantry 停止后，通过 GripperBackend 请求 CH1 OFF；
  不绕过 RelayBackend，也不执行通用继电器 all-off。Z 刹车时序仍由
  `GantryBackend.abort_motion_immediate()` 自己管理（停止后锁回）。
- **无幂等缓存、无 dry_run**：急停永远真执行。
- **本模块不持有总线锁**：各 backend 自己走 `Rs485Bus.transaction()`；
  gantry 的 `abort_motion_immediate` 设计为不等 grbl 串口锁。

组合根（面板 / maestro / Agent 进程）构造一次 `SystemEstop`，把已连接的
backend 传进来；`install_signal_handlers()` 让 SIGINT/SIGTERM 也走同一条
急停路径（修测试版审计 P0：Linux 上无任何软件急停入口）。
"""
from __future__ import annotations

import signal
import time
from types import FrameType
from typing import Optional

from pydantic import BaseModel, Field

from .hardware.errors import L3Error
from .hardware.gantry_backend import GantryBackend
from .hardware.gripper_backend import GripperBackend
from .hardware.heater_backend import HeaterBackend
from .hardware.linearstage_backend import LinearStageBackend
from .hardware.pipette_backend import PipetteBackend
from .hardware.spincoater_backend import SpincoaterBackend


class EstopStepReport(BaseModel):
    """halt_all 中单台设备的处置结果。"""

    device: str
    action: str
    ok: bool
    skipped: bool = False
    error: Optional[str] = Field(
        default=None,
        description="该步失败时的人类可读错误（急停继续执行后续步骤）。",
    )


class EstopReport(BaseModel):
    """halt_all 的完整报告；ok=False 表示至少一步失败，需人工复核现场。"""

    ok: bool
    steps: list[EstopStepReport]
    duration_ms: float = Field(ge=0)


class SystemEstop:
    """全场急停执行器。未接入的设备传 None，对应步骤记 skipped。"""

    def __init__(
        self,
        *,
        gantry: Optional[GantryBackend] = None,
        gripper: Optional[GripperBackend] = None,
        spincoater: Optional[SpincoaterBackend] = None,
        linear_stage: Optional[LinearStageBackend] = None,
        pipette: Optional[PipetteBackend] = None,
        heater: Optional[HeaterBackend] = None,
    ) -> None:
        self._gantry = gantry
        self._gripper = gripper
        self._spincoater = spincoater
        self._linear_stage = linear_stage
        self._pipette = pipette
        self._heater = heater

    def halt_all(self, *, stop_heater: bool = True) -> EstopReport:
        """按固定顺序停住全部设备；任何一步失败都继续走完剩余步骤。"""
        started = time.monotonic()
        steps: list[EstopStepReport] = []

        def run(device: str, action: str, fn: object) -> None:
            if fn is None:
                steps.append(
                    EstopStepReport(
                        device=device, action=action, ok=True, skipped=True
                    )
                )
                return
            assert callable(fn)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 —— 急停必须吞住单步异常继续
                message = (
                    exc.human_message if isinstance(exc, L3Error) else repr(exc)
                )
                steps.append(
                    EstopStepReport(
                        device=device, action=action, ok=False, error=message
                    )
                )
                return
            steps.append(EstopStepReport(device=device, action=action, ok=True))

        gantry = self._gantry
        gripper = self._gripper
        spincoater = self._spincoater
        linear_stage = self._linear_stage
        pipette = self._pipette
        heater = self._heater

        # 1) 龙门：jog-cancel + 停运动，内含 Z 刹车锁回时序（设备自管）。
        run(
            "gantry",
            "abort_motion_immediate",
            None if gantry is None else gantry.abort_motion_immediate,
        )
        # 2) 夹爪：经共享 RelayBackend 释放（CH1 OFF）。
        run(
            "gripper",
            "emergency_release",
            None if gripper is None else gripper.emergency_release,
        )
        # 3) 旋涂：带刹车停转。
        run(
            "spincoater",
            "stop(use_brake=True)",
            None
            if spincoater is None
            else (lambda: spincoater.stop(use_brake=True)),
        )
        # 4) 滑台：立即停。
        run(
            "linear_stage",
            "stop",
            None if linear_stage is None else linear_stage.stop,
        )
        # 5) 移液：IMM_STOP。
        run("pipette", "stop", None if pipette is None else pipette.stop)
        # 5) 热源最后：SV 归零（运动都停稳了才轮到慢变量）。
        if stop_heater:
            run(
                "heater",
                "set_sv(0)",
                None if heater is None else (lambda: heater.set_sv(0.0)),
            )
        else:
            steps.append(
                EstopStepReport(
                    device="heater", action="set_sv(0)", ok=True, skipped=True
                )
            )

        return EstopReport(
            ok=all(s.ok for s in steps),
            steps=steps,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def install_signal_handlers(self) -> None:
        """把 SIGINT/SIGTERM 接到 halt_all（修 Linux 无软件急停入口）。

        只能在主线程调用。急停完成后恢复默认 handler 并重投递信号，
        让进程按正常语义退出（Ctrl+C 仍然是 Ctrl+C，只是先停机）。
        """

        def _handler(signum: int, frame: Optional[FrameType]) -> None:
            del frame
            self.halt_all()
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

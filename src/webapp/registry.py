"""Web 服务的设备组合根；本卡只接入无硬件副作用的 mock 模式。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..hardware.gantry_backend import GantryBackend
from ..hardware.gripper_backend import GripperBackend
from ..hardware.heater_backend import HeaterBackend
from ..hardware.linearstage_backend import LinearStageBackend
from ..hardware.pipette_backend import PipetteBackend
from ..hardware.relay_backend import RelayBackend
from ..hardware.spincoater_backend import SpincoaterBackend
from ..system_estop import SystemEstop
from .poller import PollerLike, StatusPoller


@dataclass(slots=True)
class DeviceRegistry:
    """七个 L3 backend 与系统急停的单进程组合根。"""

    gantry: Optional[GantryBackend] = None
    relay: Optional[RelayBackend] = None
    gripper: Optional[GripperBackend] = None
    heater: Optional[HeaterBackend] = None
    spincoater: Optional[SpincoaterBackend] = None
    pipette: Optional[PipetteBackend] = None
    linear_stage: Optional[LinearStageBackend] = None
    estop: SystemEstop = field(default_factory=SystemEstop)
    mock: bool = False
    poller: PollerLike = field(init=False)

    def __post_init__(self) -> None:
        self.poller = StatusPoller(self)

    @classmethod
    def from_mocks(cls) -> DeviceRegistry:
        """构造不打开串口、不实例化真实 backend 的骨架注册表。"""
        return cls(mock=True)

    @classmethod
    def from_config(cls) -> DeviceRegistry:
        """构造真实硬件组合根；接线留给后续任务卡。"""
        raise NotImplementedError(
            "DeviceRegistry.from_config() will be wired in a later W3 task."
        )

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
        """构造接入七个纯内存设备的 mock 注册表。"""
        from typing import cast

        from .mock_devices import (
            MockGantry,
            MockGripper,
            MockHeater,
            MockLinearStage,
            MockPipette,
            MockRelay,
            MockSpincoater,
        )

        gantry = cast(GantryBackend, MockGantry())
        relay = cast(RelayBackend, MockRelay())
        gripper = cast(GripperBackend, MockGripper())
        heater = cast(HeaterBackend, MockHeater())
        spincoater = cast(SpincoaterBackend, MockSpincoater())
        pipette = cast(PipetteBackend, MockPipette())
        linear_stage = cast(LinearStageBackend, MockLinearStage())
        estop = SystemEstop(
            gantry=gantry,
            spincoater=spincoater,
            linear_stage=linear_stage,
            pipette=pipette,
            heater=heater,
        )
        return cls(
            gantry=gantry,
            relay=relay,
            gripper=gripper,
            heater=heater,
            spincoater=spincoater,
            pipette=pipette,
            linear_stage=linear_stage,
            estop=estop,
            mock=True,
        )

    @classmethod
    def from_config(cls) -> DeviceRegistry:
        """构造真实硬件组合根；接线留给后续任务卡。"""
        raise NotImplementedError(
            "DeviceRegistry.from_config() will be wired in a later W3 task."
        )

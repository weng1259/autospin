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
from ..routine import DEFAULT_RECORDABLE, RecordingProxy, RoutineRecorder
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
    routine_recorder: RoutineRecorder = field(default_factory=RoutineRecorder)
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

        recorder = RoutineRecorder()
        raw_gantry = MockGantry()
        raw_relay = MockRelay()
        raw_gripper = MockGripper()
        raw_heater = MockHeater()
        raw_spincoater = MockSpincoater()
        raw_pipette = MockPipette()
        raw_linear_stage = MockLinearStage()

        gantry = cast(
            GantryBackend,
            RecordingProxy(raw_gantry, recorder, "gantry"),
        )
        relay = cast(
            RelayBackend,
            RecordingProxy(raw_relay, recorder, "relay"),
        )
        gripper = cast(
            GripperBackend,
            RecordingProxy(raw_gripper, recorder, "gripper"),
        )
        heater = cast(
            HeaterBackend,
            RecordingProxy(raw_heater, recorder, "heater"),
        )
        spincoater = cast(
            SpincoaterBackend,
            RecordingProxy(raw_spincoater, recorder, "spin"),
        )
        pipette = cast(
            PipetteBackend,
            RecordingProxy(
                raw_pipette,
                recorder,
                "pipette",
                DEFAULT_RECORDABLE["pipette"] | {"eject_tip"},
            ),
        )
        linear_stage = cast(
            LinearStageBackend,
            RecordingProxy(
                raw_linear_stage,
                recorder,
                "linear_stage",
                {"home", "move_to"},
            ),
        )
        estop = SystemEstop(
            # 急停直达未包装对象，不能把安全动作误录进 routine。
            gantry=cast(GantryBackend, raw_gantry),
            spincoater=cast(SpincoaterBackend, raw_spincoater),
            linear_stage=cast(LinearStageBackend, raw_linear_stage),
            pipette=cast(PipetteBackend, raw_pipette),
            heater=cast(HeaterBackend, raw_heater),
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
            routine_recorder=recorder,
        )

    @classmethod
    def from_config(cls) -> DeviceRegistry:
        """构造真实硬件组合根；接线留给后续任务卡。"""
        raise NotImplementedError(
            "DeviceRegistry.from_config() will be wired in a later W3 task."
        )

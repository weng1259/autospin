"""Web 服务的设备组合根；本卡只接入无硬件副作用的 mock 模式。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from ..hardware.gantry_backend import GantryBackend
from ..hardware.gripper_backend import GripperBackend
from ..hardware.heater_backend import HeaterBackend
from ..hardware.linearstage_backend import LinearStageBackend
from ..hardware.pipette_backend import PipetteBackend
from ..hardware.relay_backend import RelayBackend
from ..hardware.spincoater_backend import SpincoaterBackend
from ..hardware.serial_resources import SerialResourceManager
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
    resource_manager: SerialResourceManager = field(
        default_factory=SerialResourceManager
    )
    accepting_operations: bool = True
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
            gripper=cast(GripperBackend, raw_gripper),
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
        """Construct configured hardware backends without opening connections."""
        from ..config import get_config
        from ..hardware.autospinmotor_adapters import (
            HeaterControllerAdapter,
            LinearStageControllerAdapter,
            PipetteControllerAdapter,
            SpinMotorControllerAdapter,
        )
        from ..hardware.heater_backend import HeaterConfig
        from ..hardware.linearstage_backend import LinearStageConfig
        from ..hardware.pipette_backend import PipetteConfig
        from ..hardware.rs485_bus import get_bus
        from ..hardware.spincoater_backend import SpincoaterConfig

        config = get_config()
        resource_manager = SerialResourceManager()
        hardware = config.hardware
        if hardware is None:
            raise RuntimeError("constants.yaml is missing the required hardware section")
        devices = config.devices
        if devices is None:
            raise RuntimeError("devices.yaml is missing required device registrations")
        required_devices = {
            "spincoater",
            "heater",
            "pipette",
            "linear_stage",
            "relay",
            "gantry",
            "gripper",
        }
        missing_devices = sorted(required_devices - devices.keys())
        if missing_devices:
            raise RuntimeError(
                "devices.yaml is missing registrations: "
                + ", ".join(missing_devices)
            )
        disabled_devices = sorted(
            name for name in required_devices if not devices[name].enabled
        )
        if disabled_devices:
            raise RuntimeError(
                "production configuration disables required devices: "
                + ", ".join(disabled_devices)
            )
        if hardware.mock:
            return cls.from_mocks()

        try:
            bus = get_bus(
                hardware.rs485.port, resource_manager=resource_manager
            )
        except TypeError as exc:
            # Compatibility for injected one-argument bus factories in
            # no-hardware wiring tests.
            if "resource_manager" not in str(exc):
                raise
            bus = get_bus(hardware.rs485.port)

        spin_cfg = hardware.spincoater
        spin_backend_config = SpincoaterConfig(
            max_rpm=spin_cfg.max_rpm,
            acceleration_rpm_per_s=spin_cfg.acceleration_rpm_per_s,
            deceleration_rpm_per_s=spin_cfg.deceleration_rpm_per_s,
            baudrate=spin_cfg.baudrate,
            timeout_s=spin_cfg.timeout_s,
            control_register=spin_cfg.control_register,
            speed_set_register=spin_cfg.speed_set_register,
            fault_register=spin_cfg.fault_register,
            pole_pairs=spin_cfg.pole_pairs,
            speed_factor=spin_cfg.speed_factor,
        )
        spin_adapter = None
        if devices["spincoater"].backend == "verified_adapter":
            spin_adapter = SpinMotorControllerAdapter.from_verified_controller(
                port=spin_cfg.port,
                mock=False,
                bus=bus,
                slave_id=spin_cfg.slave_id,
                baudrate=spin_cfg.baudrate,
                timeout_s=spin_cfg.timeout_s,
                max_rpm=spin_cfg.max_rpm,
                pole_pairs=spin_cfg.pole_pairs,
            )
        spincoater = SpincoaterBackend(
            bus,
            unit_id=spin_cfg.slave_id,
            config=spin_backend_config,
            controller_adapter=spin_adapter,
        )

        heater_cfg = hardware.heater
        heater_backend_config = HeaterConfig(
            sv_max_c=heater_cfg.sv_max_c,
            baudrate=heater_cfg.baudrate,
            timeout_s=heater_cfg.timeout_s,
            pv_register=heater_cfg.pv_register,
            sv_register=heater_cfg.sv_register,
            srun_register=heater_cfg.srun_register,
            run_on_sv_write=heater_cfg.run_on_sv_write,
            scale=heater_cfg.scale,
        )
        heater_adapter = None
        if devices["heater"].backend == "verified_adapter":
            heater_adapter = HeaterControllerAdapter.from_verified_controller(
                port=heater_cfg.port,
                mock=False,
                bus=bus,
                slave_id=heater_cfg.slave_id,
                baudrate=heater_cfg.baudrate,
                timeout_s=heater_cfg.timeout_s,
                sv_max_c=heater_cfg.sv_max_c,
                pv_register=heater_cfg.pv_register,
                sv_register=heater_cfg.sv_register,
                srun_register=heater_cfg.srun_register,
                run_on_sv_write=heater_cfg.run_on_sv_write,
                scale=heater_cfg.scale,
            )
        heater = HeaterBackend(
            bus,
            unit_id=heater_cfg.slave_id,
            config=heater_backend_config,
            controller_adapter=heater_adapter,
        )

        pipette_cfg = hardware.pipette
        pipette_backend_config = PipetteConfig(
            max_volume_ul=pipette_cfg.max_volume_ul,
            baudrate=pipette_cfg.baudrate,
            timeout_s=pipette_cfg.timeout_s,
            home_timeout_s=pipette_cfg.home_timeout_s,
            action_timeout_s=pipette_cfg.action_timeout_s,
            poll_interval_s=pipette_cfg.poll_interval_s,
            speed_01rps=pipette_cfg.speed_01rps,
            accel_01rpss=pipette_cfg.accel_01rpss,
            decel_01rpss=pipette_cfg.decel_01rpss,
        )
        pipette_adapter = None
        if devices["pipette"].backend == "verified_adapter":
            pipette_adapter = PipetteControllerAdapter.from_verified_controller(
                port=pipette_cfg.port,
                mock=False,
                bus=bus,
                slave_id=pipette_cfg.slave_id,
                baudrate=pipette_cfg.baudrate,
                timeout_s=pipette_cfg.timeout_s,
                max_volume_ul=pipette_cfg.max_volume_ul,
            )
        pipette = PipetteBackend(
            bus,
            unit_id=pipette_cfg.slave_id,
            config=pipette_backend_config,
            controller_adapter=pipette_adapter,
        )

        linear_cfg = hardware.linear_stage
        linear_backend_config = LinearStageConfig(
            travel_mm=linear_cfg.travel_mm,
            baudrate=linear_cfg.baudrate,
            timeout_s=linear_cfg.timeout_s,
            lead_mm=linear_cfg.lead_mm,
            microsteps=linear_cfg.microsteps,
            motor_step_deg=linear_cfg.motor_step_deg,
            default_speed_rpm=linear_cfg.default_speed_rpm,
            default_acceleration=linear_cfg.default_acceleration,
            home_direction=linear_cfg.home_direction,
            home_speed_rpm=linear_cfg.home_speed_rpm,
            sensorless_timeout_ms=linear_cfg.sensorless_timeout_ms,
            collision_rpm=linear_cfg.collision_rpm,
            collision_current_ma=linear_cfg.collision_current_ma,
            collision_time_ms=linear_cfg.collision_time_ms,
            home_timeout_s=linear_cfg.home_timeout_s,
            move_timeout_s=linear_cfg.move_timeout_s,
            position_tolerance_mm=linear_cfg.position_tolerance_mm,
            poll_interval_s=linear_cfg.poll_interval_s,
        )
        linear_adapter = None
        if devices["linear_stage"].backend == "verified_adapter":
            linear_adapter = LinearStageControllerAdapter.from_verified_controller(
                port=linear_cfg.port,
                mock=False,
                bus=bus,
                address=linear_cfg.address,
                baudrate=linear_cfg.baudrate,
                timeout_s=linear_cfg.timeout_s,
                lead_mm=linear_cfg.lead_mm,
                microsteps=linear_cfg.microsteps,
                motor_step_deg=linear_cfg.motor_step_deg,
                travel_mm=linear_cfg.travel_mm,
                min_position_mm=linear_cfg.min_position_mm,
                max_position_mm=linear_cfg.max_position_mm,
                default_speed_rpm=linear_cfg.default_speed_rpm,
                default_acceleration=linear_cfg.default_acceleration,
                home_direction=linear_cfg.home_direction,
                home_speed_rpm=linear_cfg.home_speed_rpm,
            )
        linear_stage = LinearStageBackend(
            bus,
            address=linear_cfg.address,
            config=linear_backend_config,
            controller_adapter=linear_adapter,
        )

        relay_cfg = hardware.relay
        relay = RelayBackend(
            port=relay_cfg.port,
            baud=relay_cfg.baudrate,
            settle_s=relay_cfg.settle_s,
            channel_map=relay_cfg.channel_map,
            resource_manager=resource_manager,
        )

        gripper = None
        gripper_cfg = hardware.gripper
        if gripper_cfg is not None:
            try:
                gripper_channel = relay_cfg.channel_map[gripper_cfg.channel]
            except KeyError as exc:
                raise RuntimeError(
                    "Gripper relay channel "
                    f"{gripper_cfg.channel!r} is missing from relay.channel_map"
                ) from exc
            gripper = GripperBackend(
                relay,
                channel=gripper_channel,
                close_wait_s=gripper_cfg.close_wait_s,
                emergency_release=gripper_cfg.emergency_release,
            )

        gantry = None
        gantry_cfg = hardware.gantry
        if gantry_cfg is not None:
            gantry_config = config.model_copy(
                update={
                    "soft_limits": gantry_cfg.soft_limits,
                    "motion": config.motion.model_copy(
                        update={
                            "default_feed_mm_min": gantry_cfg.feed_rate_default,
                        }
                    ),
                }
            )
            gantry = GantryBackend(
                port=gantry_cfg.port,
                baud=gantry_cfg.baudrate,
                relay=relay,
                config=gantry_config,
                resource_manager=resource_manager,
            )

        estop = SystemEstop(
            gantry=gantry,
            gripper=gripper,
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
            mock=False,
            resource_manager=resource_manager,
        )

    def serial_diagnostics(self) -> list[dict[str, object]]:
        return self.resource_manager.diagnostics()

    def connect_experiment_hardware(self) -> list[str]:
        """Connect production hardware needed by a complete experiment.

        Relay is opened before the gantry/gripper path.  The gripper has no
        independent connection because it is driven through the relay.
        Backend ``connect`` methods are idempotent, so already-connected
        devices remain available.
        """
        if self.mock:
            return []
        connected: list[str] = []
        for name in (
            "relay",
            "gantry",
            "spincoater",
            "pipette",
            "linear_stage",
            "heater",
        ):
            backend = getattr(self, name)
            if backend is None:
                raise RuntimeError(f"required experiment device {name} is unavailable")
            try:
                backend.connect()
            except Exception as exc:
                raise RuntimeError(
                    f"automatic connection failed for {name}: {exc}"
                ) from exc
            connected.append(name)
        return connected

    def home_experiment_hardware(self) -> list[str]:
        """Home every experiment motion device in a deterministic sequence."""
        if self.mock:
            return []
        homed: list[str] = []
        for name in ("gantry", "pipette", "linear_stage"):
            backend = getattr(self, name)
            if backend is None:
                raise RuntimeError(f"required homing device {name} is unavailable")
            try:
                backend.home(
                    idempotency_key=f"multi-round-auto-home-{name}-{uuid4()}",
                    dry_run=False,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"automatic homing failed for {name}: {exc}"
                ) from exc
            homed.append(name)
        return homed

    def shutdown(self) -> None:
        """Stop accepting work, close ports, then release all ownership."""
        if not self.accepting_operations:
            return
        self.accepting_operations = False
        self.poller.stop()
        for backend in (
            self.gantry,
            self.spincoater,
            self.heater,
            self.pipette,
            self.linear_stage,
        ):
            if backend is not None:
                backend.close()
        if self.gantry is not None:
            self.gantry.dispose()
        if self.relay is not None:
            self.relay.dispose()
        # The one remaining owner is the shared RS485 bus.
        for item in tuple(self.resource_manager.diagnostics()):
            if item["owner"] == "Rs485Bus":
                from ..hardware.rs485_bus import get_bus

                get_bus(
                    str(item["resource"]),
                    resource_manager=self.resource_manager,
                ).dispose()

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.config as config_mod
import src.webapp.registry as registry_mod
from src.config import L3Config, load_config, load_device_metadata
from src.webapp import DeviceRegistry


CONFIG_TEXT = """
soft_limits:
  x_min_mm: -275.0
  x_max_mm: -5.0
  y_min_mm: -275.0
  y_max_mm: -5.0
  z_min_mm: -90.0
  z_max_mm: -5.0
motion:
  default_feed_mm_min: 2000
  max_feed_mm_min: 3000
  move_timeout_s: 60
  status_poll_interval_ms: 200
hardware:
  mock: false
  rs485:
    port: /dev/test-rs485
  spincoater:
    backend: verified_adapter
    port: /dev/test-rs485
    slave_id: 2
    baudrate: 9600
    timeout_s: 2.0
    max_rpm: 3000
    control_register: 32768
    speed_set_register: 32773
    fault_register: 32795
    pole_pairs: 4
    speed_factor: 2.5
  heater:
    backend: verified_adapter
    port: /dev/test-rs485
    slave_id: 3
    baudrate: 9600
    timeout_s: 3.0
    pv_register: 74
    sv_register: 0
    srun_register: 27
    run_on_sv_write: true
    scale: 10.0
    sv_max_c: 150
  pipette:
    backend: verified_adapter
    port: /dev/test-rs485
    slave_id: 1
    baudrate: 115200
    timeout_s: 2.0
    home_timeout_s: 30.0
    action_timeout_s: 10.0
    poll_interval_s: 0.1
    speed_01rps: 50
    accel_01rpss: 1250
    decel_01rpss: 1250
    max_volume_ul: 1000
  linear_stage:
    backend: verified_adapter
    port: /dev/test-rs485
    address: 4
    baudrate: 115200
    timeout_s: 0.5
    travel_mm: 100.0
    min_position_mm: 0.0
    max_position_mm: 100.0
    lead_mm: 2.0
    microsteps: 16
    motor_step_deg: 1.8
    default_speed_rpm: 2000
    default_acceleration: 150
    home_direction: 1
    home_speed_rpm: 300
    sensorless_timeout_ms: 10000
    collision_rpm: 300
    collision_current_ma: 800
    collision_time_ms: 60
    home_timeout_s: 35.0
    move_timeout_s: 15.0
    position_tolerance_mm: 0.25
    poll_interval_s: 0.1
  gantry:
    backend: verified_driver
    driver: grbl_controller
    port: /dev/test-gantry
    baudrate: 115200
    timeout_s: 2.0
    homing_enabled: true
    soft_limits:
      x_min_mm: -310.0
      x_max_mm: -5.0
      y_min_mm: -310.0
      y_max_mm: -5.0
      z_min_mm: -110.0
      z_max_mm: -5.0
    feed_rate_default: 300
    commands:
      home: "$H"
      unlock: "$X"
      emergency_stop: "\\x18"
  gripper:
    backend: relay
    channel: gripper
    close_wait_s: 1.0
    emergency_release: true
  relay:
    port: /dev/test-relay
    baudrate: 9600
    settle_s: 0.0
    channel_map:
      gripper: 1
      vacuum_valve: 3
      spin_power: 4
"""


class FakeBus:
    def __init__(self, port: str) -> None:
        self.port = port


class FakeAdapter:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    @classmethod
    def from_verified_controller(cls, **kwargs: Any) -> FakeAdapter:
        return cls(**kwargs)


def _write_config(tmp_path: Path, text: str = CONFIG_TEXT) -> Path:
    path = tmp_path / "constants.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_validates_unified_hardware_schema(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path))

    assert isinstance(cfg, L3Config)
    assert cfg.hardware is not None
    assert cfg.hardware.rs485.port == "/dev/test-rs485"
    assert cfg.hardware.spincoater.slave_id == 2
    assert cfg.hardware.heater.slave_id == 3
    assert cfg.hardware.pipette.max_volume_ul == 1000
    assert cfg.hardware.linear_stage.address == 4
    assert cfg.hardware.gantry.port == "/dev/test-gantry"
    assert cfg.hardware.gantry.soft_limits.x_min_mm == -310.0
    assert cfg.hardware.gantry.commands.emergency_stop == "\x18"
    assert cfg.hardware.gripper.backend == "relay"
    assert cfg.hardware.gripper.channel == "gripper"
    assert cfg.hardware.gripper.close_wait_s == 1.0
    assert cfg.hardware.gripper.emergency_release is True
    assert cfg.hardware.relay.channel_map["vacuum_valve"] == 3


def test_production_hardware_yaml_loads_gantry() -> None:
    cfg = load_config()

    assert cfg.hardware is not None
    gantry = cfg.hardware.gantry
    assert gantry.backend == "verified_driver"
    assert gantry.driver == "grbl_controller"
    assert gantry.port == "/dev/ttyUSB1"
    assert gantry.baudrate == 115200
    assert gantry.soft_limits.z_min_mm == -110.0
    assert gantry.commands.home == "$H"
    assert gantry.commands.unlock == "$X"
    assert gantry.commands.emergency_stop == "\x18"


def test_production_hardware_yaml_loads_gripper() -> None:
    cfg = load_config()

    assert cfg.hardware is not None
    gripper = cfg.hardware.gripper
    assert gripper is not None
    assert gripper.backend == "relay"
    assert gripper.channel == "gripper"
    assert gripper.close_wait_s == 1.0
    assert gripper.emergency_release is True
    assert cfg.hardware.relay.channel_map["gripper"] == 1


def test_devices_yaml_loads_completed_gantry_metadata() -> None:
    metadata = load_device_metadata()
    gantry = metadata.devices["gantry"]

    assert gantry.model == "grbl-Mega-5X"
    assert gantry.protocol == "GRBL"
    assert gantry.source == "AutoSpinmotorSystem/hardware/xyz_stage/"
    assert gantry.migration_status == "completed"


def test_devices_yaml_loads_completed_gripper_metadata() -> None:
    metadata = load_device_metadata()
    gripper = metadata.devices["gripper"]

    assert gripper.protocol == "DSTUR-T80 relay IO"
    assert gripper.source.startswith("AutoSpinmotorSystem/")
    assert gripper.migration_status == "completed"


def test_device_registry_from_config_supports_mock_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path, CONFIG_TEXT.replace("mock: false", "mock: true"))
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(config_mod, "_CONFIG", None)

    registry = DeviceRegistry.from_config()

    assert registry.mock is True
    assert registry.heater is not None
    assert registry.spincoater is not None
    assert registry.pipette is not None
    assert registry.linear_stage is not None


def test_device_registry_from_config_wires_phase_1_to_5_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(config_mod, "_CONFIG", None)
    buses: list[FakeBus] = []

    def fake_get_bus(port: str) -> FakeBus:
        bus = FakeBus(port)
        buses.append(bus)
        return bus

    monkeypatch.setattr("src.hardware.rs485_bus.get_bus", fake_get_bus)
    monkeypatch.setattr(
        "src.hardware.autospinmotor_adapters.SpinMotorControllerAdapter",
        FakeAdapter,
    )
    monkeypatch.setattr(
        "src.hardware.autospinmotor_adapters.HeaterControllerAdapter",
        FakeAdapter,
    )
    monkeypatch.setattr(
        "src.hardware.autospinmotor_adapters.PipetteControllerAdapter",
        FakeAdapter,
    )
    monkeypatch.setattr(
        "src.hardware.autospinmotor_adapters.LinearStageControllerAdapter",
        FakeAdapter,
    )

    registry = DeviceRegistry.from_config()

    assert registry.mock is False
    assert registry.gantry is not None
    assert registry.gripper is not None
    assert registry.relay is not None
    assert registry.heater is not None
    assert registry.spincoater is not None
    assert registry.pipette is not None
    assert registry.linear_stage is not None
    assert buses and buses[0].port == "/dev/test-rs485"
    assert registry.relay.port == "/dev/test-relay"
    assert registry.gantry.port == "/dev/test-gantry"
    assert registry.gantry.baud == 115200
    assert registry.gantry.config.soft_limits.x_min_mm == -310.0
    assert registry.gantry.config.motion.default_feed_mm_min == 300
    assert registry.gantry._relay is registry.relay  # type: ignore[attr-defined]
    assert registry.gripper._relay is registry.relay  # type: ignore[attr-defined]
    assert registry.gripper._channel == 1  # type: ignore[attr-defined]
    assert registry.gripper._close_wait_s == 1.0  # type: ignore[attr-defined]
    assert registry.relay._channel_map["spin_power"] == 4  # type: ignore[attr-defined]
    assert registry.estop._heater is registry.heater  # type: ignore[attr-defined]
    assert registry.estop._spincoater is registry.spincoater  # type: ignore[attr-defined]
    assert registry.estop._pipette is registry.pipette  # type: ignore[attr-defined]
    assert registry.estop._linear_stage is registry.linear_stage  # type: ignore[attr-defined]
    assert registry.estop._gantry is registry.gantry  # type: ignore[attr-defined]
    assert registry.estop._gripper is registry.gripper  # type: ignore[attr-defined]


def test_registry_real_constructor_uses_config_instead_of_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_config(tmp_path, CONFIG_TEXT.replace("mock: false", "mock: true"))
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(config_mod, "_CONFIG", None)

    registry = DeviceRegistry.from_config()

    assert isinstance(registry, DeviceRegistry)

"""从 constants.yaml 加载安全边界 / 运动默认值。

ADR-004 §原则 5：硬编码安全边界由配置文件提供，Agent 运行时不可改。

加载点：进程启动时 `CONFIG = load_config()`，模块级单例；GantryBackend 构造
时注入（参数形式，方便测试覆盖）。

`Position` 在 types.py 里**不**做 pydantic bounds 校验（读 grbl 状态时坐标可
任意）。软限位校验在 `move_to()` 方法入口做，抛 `SoftLimitExceededError`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .hardware.errors import SoftLimitExceededError
from .hardware.types import Position

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "constants.yaml"
DEFAULT_HARDWARE_CONFIG_PATH = _REPO_ROOT / "config" / "hardware.yaml"
DEFAULT_DEVICE_METADATA_PATH = _REPO_ROOT / "config" / "devices.yaml"


def _validated_device_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("serial port/device alias must not be empty")
    lowered = cleaned.lower()
    if any(token in lowered for token in ("todo", "placeholder", "changeme", "unknown")):
        raise ValueError(f"unsafe placeholder serial path: {value!r}")
    return cleaned


class SoftLimits(BaseModel):
    x_min_mm: float
    x_max_mm: float
    y_min_mm: float
    y_max_mm: float
    z_min_mm: float
    z_max_mm: float

    _EPSILON_MM = 0.01

    @model_validator(mode="after")
    def validate_ranges(self) -> "SoftLimits":
        for axis in ("x", "y", "z"):
            lo = getattr(self, f"{axis}_min_mm")
            hi = getattr(self, f"{axis}_max_mm")
            if lo >= hi:
                raise ValueError(f"{axis}_min_mm must be less than {axis}_max_mm")
        return self

    def contains(self, p: Position) -> bool:
        e = self._EPSILON_MM
        return (
            self.x_min_mm - e <= p.x_mm <= self.x_max_mm + e
            and self.y_min_mm - e <= p.y_mm <= self.y_max_mm + e
            and self.z_min_mm - e <= p.z_mm <= self.z_max_mm + e
        )

    def assert_contains(self, p: Position) -> None:
        e = self._EPSILON_MM
        for axis, v, lo, hi in (
            ("x", p.x_mm, self.x_min_mm, self.x_max_mm),
            ("y", p.y_mm, self.y_min_mm, self.y_max_mm),
            ("z", p.z_mm, self.z_min_mm, self.z_max_mm),
        ):
            if not (lo - e <= v <= hi + e):
                raise SoftLimitExceededError(
                    human_message=f"{axis.upper()} = {v} 超出 [{lo}, {hi}]，请调整坐标",
                    agent_message=(
                        f"Axis {axis}={v} mm outside soft limit "
                        f"[{lo}, {hi}]; clamp the target before retrying."
                    ),
                )


class MotionConfig(BaseModel):
    default_feed_mm_min: float = Field(..., gt=0)
    max_feed_mm_min: float = Field(..., gt=0)
    move_timeout_s: float = Field(..., gt=0)
    status_poll_interval_ms: int = Field(..., ge=50, le=1000)


class Rs485Config(BaseModel):
    port: str = Field(..., min_length=1)
    ownership: Literal["shared_reconfigurable"]
    reconnect: bool
    deployment_confirmation_required: bool

    @field_validator("port")
    @classmethod
    def reject_placeholder_port(cls, value: str) -> str:
        return _validated_device_path(value)


class SerialDeviceConfig(BaseModel):
    port: str = Field(..., min_length=1)
    baudrate: int = Field(..., gt=0)
    parity: Literal["N", "E", "O"]
    stop_bits: Literal[1, 2]
    timeout_s: float = Field(..., gt=0)
    retry_attempts: int = Field(..., ge=0, le=10)
    retry_delay_s: float = Field(..., ge=0)
    reconnect: bool
    ownership: Literal["shared_rs485", "dedicated", "dedicated_shared_backend"]

    @field_validator("port")
    @classmethod
    def reject_placeholder_port(cls, value: str) -> str:
        return _validated_device_path(value)


class SpincoaterHardwareConfig(SerialDeviceConfig):
    slave_id: int = Field(..., ge=1, le=247)
    max_rpm: float = Field(..., gt=0)
    acceleration_rpm_per_s: float = Field(default=500.0, ge=50.0, le=6000.0)
    deceleration_rpm_per_s: float = Field(default=500.0, ge=50.0, le=6000.0)
    max_rpm_confirmation_required: bool
    capability_source: str = Field(
        default="configuration-provided hardware capability",
        min_length=1,
    )
    control_register: int = Field(..., ge=0, le=0xFFFF)
    speed_set_register: int = Field(..., ge=0, le=0xFFFF)
    actual_speed_register: int = Field(..., ge=0, le=0xFFFF)
    fault_register: int = Field(..., ge=0, le=0xFFFF)
    bus_voltage_register: int = Field(..., ge=0, le=0xFFFF)
    pole_pairs: int = Field(..., gt=0)
    speed_factor: float = Field(..., gt=0)


class HeaterHardwareConfig(SerialDeviceConfig):
    slave_id: int = Field(..., ge=1, le=247)
    pv_register: int = Field(..., ge=0, le=0xFFFF)
    sv_register: int = Field(..., ge=0, le=0xFFFF)
    srun_register: int = Field(..., ge=0, le=0xFFFF)
    run_on_sv_write: bool
    scale: float = Field(..., gt=0)
    sv_max_c: float = Field(..., gt=0)
    sv_max_c_confirmation_required: bool


class PipetteHardwareConfig(SerialDeviceConfig):
    slave_id: int = Field(..., ge=1, le=247)
    home_timeout_s: float = Field(..., gt=0)
    action_timeout_s: float = Field(..., gt=0)
    poll_interval_s: float = Field(..., gt=0)
    speed_01rps: int = Field(..., ge=0, le=0xFFFF)
    accel_01rpss: int = Field(..., ge=0, le=0xFFFF)
    decel_01rpss: int = Field(..., ge=0, le=0xFFFF)
    min_volume_ul: float = Field(..., ge=0)
    max_volume_ul: float = Field(..., gt=0)

    @model_validator(mode="after")
    def validate_volume_range(self) -> "PipetteHardwareConfig":
        if self.min_volume_ul >= self.max_volume_ul:
            raise ValueError("min_volume_ul must be less than max_volume_ul")
        return self


class LinearStageHardwareConfig(SerialDeviceConfig):
    address: int = Field(..., ge=1, le=0xFF)
    travel_mm: float = Field(..., gt=0)
    min_position_mm: float
    max_position_mm: float
    position_range_confirmation_required: bool
    lead_mm: float = Field(..., gt=0)
    microsteps: int = Field(..., gt=0)
    motor_step_deg: float = Field(..., gt=0)
    default_speed_rpm: int = Field(..., ge=1, le=3000)
    default_acceleration: int = Field(..., ge=0, le=0xFF)
    home_direction: int = Field(..., ge=0, le=1)
    home_speed_rpm: int = Field(..., ge=0, le=300)
    sensorless_timeout_ms: int = Field(..., ge=0)
    collision_rpm: int = Field(..., ge=0, le=300)
    collision_current_ma: int = Field(..., ge=0, le=0xFFFF)
    collision_time_ms: int = Field(..., ge=0, le=0xFFFF)
    home_timeout_s: float = Field(..., gt=0)
    move_timeout_s: float = Field(..., gt=0)
    position_tolerance_mm: float = Field(..., gt=0)
    poll_interval_s: float = Field(..., gt=0)

    @model_validator(mode="after")
    def validate_position_range(self) -> "LinearStageHardwareConfig":
        if self.min_position_mm >= self.max_position_mm:
            raise ValueError("min_position_mm must be less than max_position_mm")
        if self.max_position_mm - self.min_position_mm < self.travel_mm:
            raise ValueError("linear-stage range must cover travel_mm")
        return self


class GantryCommandsConfig(BaseModel):
    home: str = "$H"
    unlock: str = "$X"
    emergency_stop: str = "\x18"


class GantryHardwareConfig(SerialDeviceConfig):
    deployment_confirmation_required: bool
    homing_enabled: bool = True
    soft_limits: SoftLimits
    feed_rate_default: float = Field(300.0, gt=0)
    feed_rate_confirmation_required: bool
    max_feed_mm_min: float = Field(..., gt=0)
    z_feed_mm_min: float = Field(1000.0, gt=0)
    boundary_feed_mm_min: float = Field(1000.0, gt=0)
    boundary_margin_mm: float = Field(20.0, gt=0)
    boundary_escape_mm: float = Field(10.0, gt=0)
    grbl_acceleration_mm_s2: float = Field(500.0, gt=0)
    move_timeout_s: float = Field(..., gt=0)
    status_poll_interval_ms: int = Field(..., ge=50, le=1000)
    commands: GantryCommandsConfig

    @model_validator(mode="after")
    def validate_verified_behavior(self) -> "GantryHardwareConfig":
        expected = (-310.0, -5.0, -310.0, -5.0, -110.0, -5.0)
        actual = (
            self.soft_limits.x_min_mm,
            self.soft_limits.x_max_mm,
            self.soft_limits.y_min_mm,
            self.soft_limits.y_max_mm,
            self.soft_limits.z_min_mm,
            self.soft_limits.z_max_mm,
        )
        if actual != expected:
            raise ValueError(f"gantry soft limits must equal verified limits {expected}")
        if self.feed_rate_default > self.max_feed_mm_min:
            raise ValueError("gantry feed_rate_default must not exceed max_feed_mm_min")
        if self.z_feed_mm_min > self.max_feed_mm_min:
            raise ValueError("gantry z_feed_mm_min must not exceed max_feed_mm_min")
        x_span = self.soft_limits.x_max_mm - self.soft_limits.x_min_mm
        y_span = self.soft_limits.y_max_mm - self.soft_limits.y_min_mm
        if self.boundary_escape_mm * 2 >= min(x_span, y_span):
            raise ValueError("gantry boundary_escape_mm is too large for XY travel")
        if (
            self.commands.home != "$H"
            or self.commands.unlock != "$X"
            or self.commands.emergency_stop != "\x18"
        ):
            raise ValueError("gantry commands must preserve verified $H/$X/Ctrl-X behavior")
        return self


class RelayHardwareConfig(SerialDeviceConfig):
    deployment_confirmation_required: bool
    settle_s: float = Field(..., ge=0.3)
    channel_map: dict[str, int] = Field(default_factory=dict)
    intentional_aliases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_channels(self) -> "RelayHardwareConfig":
        for name, channel in self.channel_map.items():
            if not name.strip() or not 1 <= channel <= 8:
                raise ValueError(f"invalid relay channel mapping {name!r}={channel}")
        by_channel: dict[int, list[str]] = {}
        for name, channel in self.channel_map.items():
            by_channel.setdefault(channel, []).append(name)
        for channel, names in by_channel.items():
            if len(names) < 2:
                continue
            canonical = names[0]
            if any(self.intentional_aliases.get(name) != canonical for name in names[1:]):
                raise ValueError(
                    f"relay channel {channel} is duplicated by {names}; "
                    "declare each non-canonical name in intentional_aliases"
                )
        return self


class GripperHardwareConfig(BaseModel):
    channel: str = Field("gripper", min_length=1)
    close_level: Literal[True]
    open_level: Literal[False]
    close_wait_s: float = Field(1.0, ge=0)
    emergency_release: Literal[True]


class HardwareConfig(BaseModel):
    mock: bool = False
    rs485: Rs485Config
    spincoater: SpincoaterHardwareConfig
    heater: HeaterHardwareConfig
    pipette: PipetteHardwareConfig
    linear_stage: LinearStageHardwareConfig
    gantry: GantryHardwareConfig | None = None
    gripper: GripperHardwareConfig | None = None
    relay: RelayHardwareConfig

    @model_validator(mode="after")
    def validate_serial_ownership(self) -> "HardwareConfig":
        shared = {
            "spincoater": self.spincoater,
            "heater": self.heater,
            "pipette": self.pipette,
            "linear_stage": self.linear_stage,
        }
        for name, device in shared.items():
            if device.port != self.rs485.port or device.ownership != "shared_rs485":
                raise ValueError(
                    f"{name} must use rs485.port with ownership=shared_rs485"
                )
        if self.gantry is not None and self.gantry.ownership != "dedicated":
            raise ValueError("gantry must have dedicated serial ownership")
        if self.relay.ownership != "dedicated_shared_backend":
            raise ValueError(
                "relay must use one dedicated_shared_backend instance"
            )
        return self


class DeviceMetadata(BaseModel):
    enabled: bool
    backend: Literal["direct", "verified_adapter", "verified_driver", "relay"]
    driver: Literal["grbl_controller"] | None = None
    model: str | None = None
    protocol: str | None = None
    source: str = Field(..., min_length=1)
    migration_status: Literal[
        "deferred", "code_migrated_hardware_unverified", "hardware_verified"
    ] | None = None


class DeviceMetadataConfig(BaseModel):
    devices: dict[str, DeviceMetadata]


class L3Config(BaseModel):
    soft_limits: SoftLimits
    motion: MotionConfig
    hardware: HardwareConfig | None = None
    devices: dict[str, DeviceMetadata] | None = None


def load_config(path: Optional[Path] = None) -> L3Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"configuration root must be a mapping: {p}")
    production_config = (_REPO_ROOT / "constants.yaml").resolve()
    if p.resolve() == production_config:
        with DEFAULT_HARDWARE_CONFIG_PATH.open("r", encoding="utf-8") as f:
            hardware_raw = yaml.safe_load(f)
        with DEFAULT_DEVICE_METADATA_PATH.open("r", encoding="utf-8") as f:
            devices_raw = yaml.safe_load(f)
        raw.update(hardware_raw)
        raw.update(devices_raw)
        gantry = raw["hardware"]["gantry"]
        raw["soft_limits"] = gantry["soft_limits"]
        raw["motion"] = {
            "default_feed_mm_min": gantry["feed_rate_default"],
            "max_feed_mm_min": gantry["max_feed_mm_min"],
            "move_timeout_s": gantry["move_timeout_s"],
            "status_poll_interval_ms": gantry["status_poll_interval_ms"],
        }
    return L3Config.model_validate(raw)


def load_device_metadata(
    path: Optional[Path] = None,
) -> DeviceMetadataConfig:
    p = Path(path) if path else DEFAULT_DEVICE_METADATA_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return DeviceMetadataConfig.model_validate(raw)


# 进程级单例（仅在 constants.yaml 存在且合法时懒加载）
_CONFIG: Optional[L3Config] = None


def get_config() -> L3Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG

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
from pydantic import BaseModel, Field

from .hardware.errors import SoftLimitExceededError
from .hardware.types import Position

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "constants.yaml"
DEFAULT_HARDWARE_CONFIG_PATH = _REPO_ROOT / "config" / "hardware.yaml"
DEFAULT_DEVICE_METADATA_PATH = _REPO_ROOT / "config" / "devices.yaml"


class SoftLimits(BaseModel):
    x_min_mm: float
    x_max_mm: float
    y_min_mm: float
    y_max_mm: float
    z_min_mm: float
    z_max_mm: float

    _EPSILON_MM = 0.01

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


class SpincoaterHardwareConfig(BaseModel):
    backend: Literal["direct", "verified_adapter"] = "verified_adapter"
    port: str = Field(..., min_length=1)
    slave_id: int = Field(..., ge=1, le=247)
    baudrate: int = Field(9600, gt=0)
    timeout_s: float = Field(2.0, gt=0)
    max_rpm: float = Field(..., gt=0)
    control_register: int = Field(0x8000, ge=0, le=0xFFFF)
    speed_set_register: int = Field(0x8005, ge=0, le=0xFFFF)
    fault_register: int = Field(0x801B, ge=0, le=0xFFFF)
    pole_pairs: int = Field(4, gt=0)
    speed_factor: float = Field(2.5, gt=0)


class HeaterHardwareConfig(BaseModel):
    backend: Literal["direct", "verified_adapter"] = "verified_adapter"
    port: str = Field(..., min_length=1)
    slave_id: int = Field(..., ge=1, le=247)
    baudrate: int = Field(9600, gt=0)
    timeout_s: float = Field(3.0, gt=0)
    pv_register: int = Field(74, ge=0, le=0xFFFF)
    sv_register: int = Field(0, ge=0, le=0xFFFF)
    srun_register: int = Field(27, ge=0, le=0xFFFF)
    run_on_sv_write: bool = True
    scale: float = Field(10.0, gt=0)
    sv_max_c: float = Field(..., gt=0)


class PipetteHardwareConfig(BaseModel):
    backend: Literal["direct", "verified_adapter"] = "verified_adapter"
    port: str = Field(..., min_length=1)
    slave_id: int = Field(..., ge=1, le=247)
    baudrate: int = Field(115200, gt=0)
    timeout_s: float = Field(2.0, gt=0)
    home_timeout_s: float = Field(30.0, gt=0)
    action_timeout_s: float = Field(10.0, gt=0)
    poll_interval_s: float = Field(0.1, gt=0)
    speed_01rps: int = Field(50, ge=0, le=0xFFFF)
    accel_01rpss: int = Field(1250, ge=0, le=0xFFFF)
    decel_01rpss: int = Field(1250, ge=0, le=0xFFFF)
    max_volume_ul: float = Field(..., gt=0)


class LinearStageHardwareConfig(BaseModel):
    backend: Literal["direct", "verified_adapter"] = "verified_adapter"
    port: str = Field(..., min_length=1)
    address: int = Field(..., ge=1, le=0xFF)
    baudrate: int = Field(115200, gt=0)
    timeout_s: float = Field(0.5, gt=0)
    travel_mm: float = Field(..., gt=0)
    min_position_mm: float = 0.0
    max_position_mm: float | None = None
    lead_mm: float = Field(2.0, gt=0)
    microsteps: int = Field(16, gt=0)
    motor_step_deg: float = Field(1.8, gt=0)
    default_speed_rpm: int = Field(2000, ge=1, le=3000)
    default_acceleration: int = Field(150, ge=0, le=0xFF)
    home_direction: int = Field(1, ge=0, le=1)
    home_speed_rpm: int = Field(300, ge=0, le=300)
    sensorless_timeout_ms: int = Field(10000, ge=0)
    collision_rpm: int = Field(300, ge=0, le=300)
    collision_current_ma: int = Field(800, ge=0, le=0xFFFF)
    collision_time_ms: int = Field(60, ge=0, le=0xFFFF)
    home_timeout_s: float = Field(35.0, gt=0)
    move_timeout_s: float = Field(15.0, gt=0)
    position_tolerance_mm: float = Field(0.25, gt=0)
    poll_interval_s: float = Field(0.1, gt=0)


class GantryCommandsConfig(BaseModel):
    home: str = "$H"
    unlock: str = "$X"
    emergency_stop: str = "\x18"


class GantryHardwareConfig(BaseModel):
    backend: Literal["verified_driver"] = "verified_driver"
    driver: Literal["grbl_controller"] = "grbl_controller"
    port: str = Field(..., min_length=1)
    baudrate: int = Field(115200, gt=0)
    timeout_s: float = Field(2.0, gt=0)
    homing_enabled: bool = True
    soft_limits: SoftLimits
    feed_rate_default: float = Field(300.0, gt=0)
    commands: GantryCommandsConfig


class RelayHardwareConfig(BaseModel):
    port: str = Field(..., min_length=1)
    baudrate: int = Field(9600, gt=0)
    settle_s: float = Field(0.3, ge=0)
    channel_map: dict[str, int] = Field(default_factory=dict)


class GripperHardwareConfig(BaseModel):
    backend: Literal["relay"] = "relay"
    channel: str = Field("gripper", min_length=1)
    close_wait_s: float = Field(1.0, ge=0)
    emergency_release: bool = True


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


class DeviceMetadata(BaseModel):
    model: str | None = None
    protocol: str | None = None
    source: str = Field(..., min_length=1)
    migration_status: Literal["deferred", "completed"] | None = None


class DeviceMetadataConfig(BaseModel):
    devices: dict[str, DeviceMetadata]


class L3Config(BaseModel):
    soft_limits: SoftLimits
    motion: MotionConfig
    hardware: HardwareConfig | None = None


def load_config(path: Optional[Path] = None) -> L3Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    production_config = (_REPO_ROOT / "constants.yaml").resolve()
    if p.resolve() == production_config:
        with DEFAULT_HARDWARE_CONFIG_PATH.open("r", encoding="utf-8") as f:
            hardware_raw = yaml.safe_load(f)
        raw.update(hardware_raw)
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

"""Experiment-facing Gantry backend backed by the verified GRBL driver."""
from __future__ import annotations

from typing import Optional

from ..config import L3Config
from .drivers.gantry import grbl_controller as _driver_module
from .drivers.gantry.grbl_controller import GrblController
from .relay_backend import RelayBackend
from .serial_resources import SerialResourceManager
from .types import (
    GrblSettingsSnapshot,
    GrblSettingsValidationResult,
    HomePlan,
    HomeResult,
    MachineStatus,
    MovePlan,
    MoveResult,
    Position,
    RecoveryResult,
)

# Compatibility exports for diagnostics that historically patched this module.
# Hardware behavior remains owned by the driver module.
GRBL_EXPECTED_SETTINGS = _driver_module.GRBL_EXPECTED_SETTINGS
_BRAKE_LOCK_GRACE_S = _driver_module._BRAKE_LOCK_GRACE_S
time = _driver_module.time


class GantryBackend:
    """L3 facade; all hardware behavior is delegated to ``GrblController``."""

    def __init__(
        self,
        port: str = "/dev/cu.wchusbserial110",
        baud: int = 115200,
        relay: Optional[RelayBackend] = None,
        config: Optional[L3Config] = None,
        *,
        driver: GrblController | None = None,
        resource_manager: SerialResourceManager | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "_driver",
            driver
            if driver is not None
            else GrblController(
                port=port,
                baud=baud,
                relay=relay,
                config=config,
                resource_manager=resource_manager,
            ),
        )

    @property
    def driver(self) -> GrblController:
        return self._driver

    def connect(self) -> None:
        self._driver.connect()

    def close(self) -> None:
        self._driver.disconnect()

    def disconnect(self) -> None:
        self._driver.disconnect()

    def dispose(self) -> None:
        self._driver.dispose()

    def get_status(self) -> MachineStatus:
        return self._driver.get_status()

    def get_position(self) -> Position:
        return self._driver.get_position()

    def is_connected(self) -> bool:
        return self._driver.is_connected()

    def is_homed(self) -> bool:
        return self._driver.is_homed()

    def home(
        self, *, idempotency_key: str, dry_run: bool = False
    ) -> HomeResult | HomePlan:
        return self._driver.home(
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    def move_to(
        self,
        target: Position,
        *,
        feed_mm_min: Optional[float] = None,
        wait_for_idle: bool = True,
        timeout_s: Optional[float] = None,
        dry_run: bool = False,
    ) -> MoveResult | MovePlan:
        return self._driver.move_to(
            target,
            feed_mm_min=feed_mm_min,
            wait_for_idle=wait_for_idle,
            timeout_s=timeout_s,
            dry_run=dry_run,
        )

    def jog(
        self,
        axis: str,
        distance_mm: float,
        feed_mm_min: float = 100.0,
    ) -> Position:
        return self._driver.jog(axis, distance_mm, feed_mm_min)

    def manual_jog_rel(
        self,
        dx_mm: float = 0.0,
        dy_mm: float = 0.0,
        dz_mm: float = 0.0,
        *,
        feed_mm_min: float = 300.0,
        timeout_s: float = 15.0,
    ) -> MachineStatus:
        return self._driver.manual_jog_rel(
            dx_mm,
            dy_mm,
            dz_mm,
            feed_mm_min=feed_mm_min,
            timeout_s=timeout_s,
        )

    def halt(self) -> MachineStatus:
        return self._driver.stop()

    def abort_motion_immediate(self) -> MachineStatus:
        return self._driver.emergency_stop()

    def soft_reset(self) -> None:
        self._driver.soft_reset()

    def unlock_alarm(self) -> MachineStatus:
        return self._driver.unlock()

    def get_grbl_settings(self) -> GrblSettingsSnapshot:
        return self._driver.get_grbl_settings()

    def validate_grbl_settings(self) -> GrblSettingsValidationResult:
        return self._driver.validate_grbl_settings()

    def repair_grbl_settings(self) -> GrblSettingsValidationResult:
        return self._driver.repair_grbl_settings()

    def get_homing_diagnostics(self) -> dict[str, object]:
        return self._driver.get_homing_diagnostics()

    def recover_from_alarm(
        self,
        *,
        idempotency_key: str,
        skip_rehome: bool = False,
    ) -> RecoveryResult:
        return self._driver.recover_from_alarm(
            idempotency_key=idempotency_key,
            skip_rehome=skip_rehome,
        )

    def set_z_brake_released(self, released: bool) -> MachineStatus:
        return self._driver.set_z_brake_released(released)

    def enter_manual_mode(
        self,
        *,
        release_xy: bool = True,
        release_z: bool = False,
    ) -> MachineStatus:
        return self._driver.enter_manual_mode(
            release_xy=release_xy,
            release_z=release_z,
        )

    def exit_manual_mode(self, *, rehome: bool = True) -> MachineStatus:
        return self._driver.exit_manual_mode(rehome=rehome)

    def start_move_async(
        self,
        target: Position,
        *,
        feed_mm_min: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._driver.start_move_async(
            target,
            feed_mm_min=feed_mm_min,
            timeout_s=timeout_s,
        )

    def is_move_in_progress(self) -> bool:
        return self._driver.is_move_in_progress()

    def consume_last_move_result(
        self,
    ) -> tuple[Optional[MoveResult], Optional[Exception]]:
        return self._driver.consume_last_move_result()

    def __getattr__(self, name: str) -> object:
        """Keep legacy diagnostics/tests working while internals live in driver."""
        return getattr(self._driver, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_driver":
            object.__setattr__(self, name, value)
            return
        if name.startswith("_") and "_driver" in self.__dict__:
            setattr(self._driver, name, value)
            return
        object.__setattr__(self, name, value)


for _method_name in (
    "connect",
    "close",
    "get_status",
    "get_position",
    "is_connected",
    "is_homed",
    "get_grbl_settings",
    "validate_grbl_settings",
    "repair_grbl_settings",
    "home",
    "move_to",
    "halt",
    "soft_reset",
    "unlock_alarm",
    "recover_from_alarm",
):
    getattr(GantryBackend, _method_name).__doc__ = getattr(
        GrblController, _method_name
    ).__doc__

for _method_name in ("home", "recover_from_alarm"):
    setattr(
        getattr(GantryBackend, _method_name),
        "__observable_ttl_s__",
        getattr(
            getattr(GrblController, _method_name),
            "__observable_ttl_s__",
        ),
    )

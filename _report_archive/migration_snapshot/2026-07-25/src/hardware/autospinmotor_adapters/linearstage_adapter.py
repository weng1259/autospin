"""Adapter for the verified AutoSpinmotorSystem Emm linear-stage controller."""
from __future__ import annotations

import math
import time
from typing import Any, Protocol

from ..linearstage_backend import (
    LinearStageActionResult,
    LinearStageCommunicationError,
    LinearStagePositionOutOfRangeError,
    LinearStageStatus,
)
from ..rs485_bus import Rs485Bus


class EmmLinearStageLike(Protocol):
    def connect(self) -> bool: ...
    def close(self) -> None: ...
    def home(self, *, direction: int | None = None, timeout_s: float = 30.0) -> bytes: ...
    def move_absolute(self, target_mm: float, **kwargs: Any) -> bytes: ...
    def move_relative(self, distance_mm: float, **kwargs: Any) -> bytes: ...
    def read_position_mm(self) -> float: ...
    def read_flags(self) -> int: ...
    def read_home_status(self) -> int: ...
    def stop(self) -> bytes: ...
    def reset_protection(self) -> bytes: ...
    def get_status(self, *, live: bool = False) -> dict[str, Any]: ...


class LinearStageControllerAdapter:
    """Translate verified EmmLinearStage behavior into autospin L3 results."""

    def __init__(
        self,
        controller: EmmLinearStageLike,
        *,
        bus: Rs485Bus | None = None,
        device_name: str = "linear_stage",
        baudrate: int = 115200,
        timeout_s: float = 0.5,
        min_position_mm: float = 0.0,
        max_position_mm: float = 100.0,
    ) -> None:
        self._controller = controller
        self._bus = bus
        self._device_name = device_name
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._min_position_mm = min_position_mm
        self._max_position_mm = max_position_mm
        self._connected = False
        self._homed = False
        self._position_mm: float | None = None

    @classmethod
    def from_verified_controller(
        cls,
        *,
        port: str,
        mock: bool = False,
        bus: Rs485Bus | None = None,
        address: int = 4,
        baudrate: int = 115200,
        timeout_s: float = 0.5,
        lead_mm: float = 2.0,
        microsteps: int = 16,
        motor_step_deg: float = 1.8,
        travel_mm: float = 100.0,
        min_position_mm: float = 0.0,
        max_position_mm: float | None = None,
        default_speed_rpm: int = 2000,
        default_acceleration: int = 150,
        home_direction: int = 1,
        home_speed_rpm: int = 300,
    ) -> LinearStageControllerAdapter:
        try:
            from ..drivers.linear_stage.linear_stage import (
                EmmLinearStage,
            )
        except ImportError as exc:
            raise LinearStageCommunicationError(
                human_message="无法导入已验证的 EmmLinearStage",
                agent_message=(
                    "Could not import src.hardware.drivers.linear_stage."
                    f"linear_stage.EmmLinearStage: {exc!r}."
                ),
            ) from exc

        controller = EmmLinearStage(
            port=port,
            address=address,
            baudrate=baudrate,
            timeout=timeout_s,
            lead_mm=lead_mm,
            microsteps=microsteps,
            motor_step_deg=motor_step_deg,
            travel_mm=travel_mm,
            min_position_mm=min_position_mm,
            max_position_mm=max_position_mm,
            default_speed_rpm=default_speed_rpm,
            default_acceleration=default_acceleration,
            home_direction=home_direction,
            home_speed_rpm=home_speed_rpm,
            mock=mock,
        )
        return cls(
            controller,
            bus=bus,
            baudrate=baudrate,
            timeout_s=timeout_s,
            min_position_mm=min_position_mm,
            max_position_mm=travel_mm if max_position_mm is None else max_position_mm,
        )

    def connect(self) -> None:
        with self._guard():
            try:
                ok = self._controller.connect()
            except Exception as exc:
                raise self._communication_error("connect", exc) from exc
        if not ok:
            raise LinearStageCommunicationError(
                human_message="丝杆滑台连接失败",
                agent_message="Verified EmmLinearStage.connect() returned False.",
            )
        self._connected = True
        self._homed = False
        self._position_mm = None

    def close(self) -> None:
        try:
            self._controller.close()
        finally:
            self._connected = False
            self._homed = False

    def home(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        del idempotency_key
        if dry_run:
            return self._result("home", 0.0, None, True, "Dry-run verified linear-stage home.")
        started = time.monotonic()
        with self._guard():
            try:
                self._controller.reset_protection()
                self._controller.home()
                self._position_mm = float(self._controller.read_position_mm())
            except Exception as exc:
                self._homed = False
                raise self._communication_error("home", exc) from exc
        self._homed = True
        return self._result("home", 0.0, self._position_mm, False, "Homed through verified EmmLinearStage.", started=started)

    def move_to(
        self,
        position_mm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        del idempotency_key
        target = self._validate_position(position_mm)
        if dry_run:
            return self._result("move_to", target, None, True, "Dry-run verified linear-stage absolute move.")
        started = time.monotonic()
        with self._guard():
            try:
                self._controller.move_absolute(target)
                self._position_mm = float(self._controller.read_position_mm())
            except Exception as exc:
                raise self._communication_error("move_to", exc) from exc
        return self._result("move_to", target, self._position_mm, False, "Moved absolute through verified EmmLinearStage.", started=started)

    def move_relative(
        self,
        delta_mm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        del idempotency_key
        if not math.isfinite(delta_mm):
            raise LinearStagePositionOutOfRangeError(
                human_message=f"丝杆滑台相对位移 {delta_mm!r} mm 无效",
                agent_message=f"Requested relative move {delta_mm!r} mm is not finite.",
            )
        target = None if self._position_mm is None else self._position_mm + float(delta_mm)
        if dry_run:
            return self._result("move_to", target, None, True, "Dry-run verified linear-stage relative move.")
        started = time.monotonic()
        with self._guard():
            try:
                self._controller.move_relative(float(delta_mm))
                self._position_mm = float(self._controller.read_position_mm())
            except Exception as exc:
                raise self._communication_error("move_relative", exc) from exc
        return self._result("move_to", target, self._position_mm, False, "Moved relative through verified EmmLinearStage.", started=started)

    def stop(self) -> LinearStageActionResult:
        started = time.monotonic()
        with self._guard():
            try:
                self._controller.stop()
            except Exception as exc:
                raise self._communication_error("stop", exc) from exc
        return self._result("stop", None, self._position_mm, False, "Stopped through verified EmmLinearStage.", started=started)

    def reset_protection(self) -> LinearStageActionResult:
        started = time.monotonic()
        with self._guard():
            try:
                self._controller.reset_protection()
            except Exception as exc:
                raise self._communication_error("reset_protection", exc) from exc
        return self._result("stop", None, self._position_mm, False, "Reset Emm protection through verified controller.", started=started)

    def status(self) -> LinearStageStatus:
        try:
            raw = self._controller.get_status(live=False)
        except Exception as exc:
            raise self._communication_error("status", exc) from exc
        position = raw.get("actual_position_mm", raw.get("commanded_position_mm", self._position_mm))
        self._position_mm = None if position is None else float(position)
        return LinearStageStatus(
            connected=bool(raw.get("connected", self._connected)),
            homed=self._homed,
            moving=False,
            position_mm=self._position_mm,
            flags_raw=raw.get("flags_raw"),
            home_status_raw=raw.get("home_status_raw"),
        )

    def _validate_position(self, position_mm: float) -> float:
        if (
            not math.isfinite(position_mm)
            or position_mm < self._min_position_mm
            or position_mm > self._max_position_mm
        ):
            raise LinearStagePositionOutOfRangeError(
                human_message=f"丝杆滑台目标 {position_mm!r} mm 超出安全范围",
                agent_message=(
                    f"Requested linear-stage position {position_mm!r} mm is outside "
                    f"[{self._min_position_mm:g}, {self._max_position_mm:g}]."
                ),
            )
        return float(position_mm)

    def _guard(self):
        if self._bus is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._bus.guard(self._device_name, self._baudrate, timeout_s=self._timeout_s)

    def _communication_error(self, action: str, exc: Exception) -> LinearStageCommunicationError:
        return LinearStageCommunicationError(
            human_message=f"丝杆滑台 {action} 通信失败",
            agent_message=(
                f"Verified EmmLinearStage failed during {action}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    @staticmethod
    def _result(
        action: str,
        target_position_mm: float | None,
        final_position_mm: float | None,
        dry_run: bool,
        description: str,
        *,
        started: float | None = None,
    ) -> LinearStageActionResult:
        return LinearStageActionResult(
            success=True,
            action=action,  # type: ignore[arg-type]
            target_position_mm=target_position_mm,
            final_position_mm=final_position_mm,
            dry_run=dry_run,
            action_description=description,
            duration_ms=0.0 if started is None else (time.monotonic() - started) * 1000.0,
            event_id="",
        )

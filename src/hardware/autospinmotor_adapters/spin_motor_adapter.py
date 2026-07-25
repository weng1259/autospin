"""Adapter for the verified AutoSpinmotorSystem DBLS400 MotorController."""
from __future__ import annotations

import math
import time
from typing import Any, Protocol

from ..errors import ConnectionError as L3ConnectionError
from ..rs485_bus import Rs485Bus
from ..spincoater_backend import (
    SpinActionResult,
    SpinStatus,
    SpincoaterCommunicationError,
    SpincoaterRpmOutOfRangeError,
)


class MotorControllerLike(Protocol):
    def connect(self) -> bool: ...
    def close(self) -> None: ...
    def shutdown(self) -> None: ...
    def start(
        self, direction: str = "forward", wait_for_stop: bool = True
    ) -> tuple[bool, str]: ...
    def stop(self, use_brake: bool = True) -> bool: ...
    def set_speed(self, rpm: float, **kwargs: Any) -> tuple[bool, str]: ...
    def get_actual_speed(self) -> float: ...
    def get_status(self) -> dict[str, Any]: ...


class SpinMotorControllerAdapter:
    """Translate verified MotorController behavior into autospin L3 results."""

    def __init__(
        self,
        controller: MotorControllerLike,
        *,
        bus: Rs485Bus | None = None,
        device_name: str = "spincoater",
        baudrate: int = 9600,
        timeout_s: float = 2.0,
        max_rpm: float = 3000.0,
    ) -> None:
        self._controller = controller
        self._bus = bus
        self._device_name = device_name
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._max_rpm = max_rpm
        self._connected = False

    @classmethod
    def from_verified_controller(
        cls,
        *,
        port: str,
        mock: bool = False,
        bus: Rs485Bus | None = None,
        baudrate: int = 9600,
        timeout_s: float = 2.0,
        max_rpm: float = 3000.0,
    ) -> SpinMotorControllerAdapter:
        """Construct from the bundled verified AutoSpinmotorSystem controller."""
        try:
            from autospin_system.hardware.spin_motor.motor_controller import (
                MotorController,
            )
        except ImportError as exc:
            raise SpincoaterCommunicationError(
                human_message="无法导入已验证的旋涂电机 MotorController",
                agent_message=(
                    "Could not import autospin_system.hardware.spin_motor."
                    f"motor_controller.MotorController: {exc!r}."
                ),
            ) from exc

        controller = MotorController(port=port, mock=mock)
        return cls(
            controller,
            bus=bus,
            baudrate=baudrate,
            timeout_s=timeout_s,
            max_rpm=max_rpm,
        )

    def connect(self) -> None:
        with self._guard():
            try:
                ok = self._controller.connect()
            except Exception as exc:
                raise self._communication_error("connect", exc) from exc
        if not ok:
            raise SpincoaterCommunicationError(
                human_message="旋涂电机连接失败",
                agent_message="Verified MotorController.connect() returned False.",
            )
        self._connected = True

    def close(self) -> None:
        try:
            self._controller.close()
        finally:
            self._connected = False

    def shutdown(self) -> None:
        try:
            self._controller.shutdown()
        finally:
            self._connected = False

    def start(
        self,
        rpm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SpinActionResult:
        del idempotency_key
        command_value = self._validate_rpm(rpm)
        if dry_run:
            return SpinActionResult(
                success=True,
                action="start",
                target_rpm=rpm,
                command_value=command_value,
                brake_engaged=False,
                dry_run=True,
                action_description=(
                    "Dry-run MotorController-backed DBLS400 start; no bytes sent."
                ),
                duration_ms=0.0,
                event_id="",
            )

        started = time.monotonic()
        with self._guard():
            try:
                start_ok, start_message = self._controller.start(
                    direction="forward",
                    wait_for_stop=True,
                )
                if not start_ok:
                    raise SpincoaterCommunicationError(
                        human_message="旋涂电机启动失败",
                        agent_message=(
                            "Verified MotorController.start(wait_for_stop=True) "
                            f"returned False: {start_message}"
                        ),
                    )
                speed_ok, speed_message = self._controller.set_speed(rpm)
                if not speed_ok:
                    raise SpincoaterCommunicationError(
                        human_message="旋涂电机设速失败",
                        agent_message=(
                            "Verified MotorController.set_speed() returned "
                            f"False: {speed_message}"
                        ),
                    )
            except L3ConnectionError:
                raise
            except Exception as exc:
                raise self._communication_error("start", exc) from exc

        return SpinActionResult(
            success=True,
            action="start",
            target_rpm=rpm,
            command_value=command_value,
            brake_engaged=False,
            dry_run=False,
            action_description=(
                "Started through verified MotorController with wait_for_stop=True."
            ),
            duration_ms=(time.monotonic() - started) * 1000.0,
            event_id="",
        )

    def stop(
        self,
        *,
        use_brake: bool = True,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SpinActionResult:
        del idempotency_key
        if dry_run:
            return SpinActionResult(
                success=True,
                action="stop",
                target_rpm=0.0,
                command_value=0,
                brake_engaged=use_brake,
                dry_run=True,
                action_description=(
                    "Dry-run MotorController-backed DBLS400 stop; no bytes sent."
                ),
                duration_ms=0.0,
                event_id="",
            )

        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.stop(use_brake=use_brake)
            except Exception as exc:
                raise self._communication_error("stop", exc) from exc
        if not ok:
            raise SpincoaterCommunicationError(
                human_message="旋涂电机停止失败",
                agent_message=(
                    "Verified MotorController.stop() returned False with "
                    f"use_brake={use_brake}."
                ),
            )

        return SpinActionResult(
            success=True,
            action="stop",
            target_rpm=0.0,
            command_value=0,
            brake_engaged=use_brake,
            dry_run=False,
            action_description="Stopped through verified MotorController.",
            duration_ms=(time.monotonic() - started) * 1000.0,
            event_id="",
        )

    def status(self) -> SpinStatus:
        try:
            raw = self._controller.get_status()
        except Exception as exc:
            raise self._communication_error("status", exc) from exc

        actual_rpm: float | None
        try:
            actual_rpm = float(self._controller.get_actual_speed())
        except Exception:
            actual_rpm = None

        connected = bool(raw.get("connected", self._connected))
        running = bool(raw.get("running", raw.get("is_running", False)))
        target = raw.get("target_speed", raw.get("target_rpm", actual_rpm))
        target_rpm = float(target) if target is not None else actual_rpm
        return SpinStatus(
            connected=connected,
            running=running,
            target_rpm=target_rpm,
            brake_engaged=raw.get("brake_engaged"),
            fault_register=raw.get("fault_register"),
            fault_bits=list(raw.get("fault_bits", [])),
        )

    def read_fault(self) -> SpinStatus:
        return self.status()

    def _validate_rpm(self, rpm: float) -> int:
        if not math.isfinite(rpm) or rpm <= 0 or rpm > self._max_rpm:
            raise SpincoaterRpmOutOfRangeError(
                human_message=f"旋涂目标转速 {rpm!r} RPM 超出安全范围",
                agent_message=(
                    f"Requested spin speed {rpm!r} RPM is outside "
                    f"(0, {self._max_rpm:g}]."
                ),
            )
        return int(round(rpm))

    def _guard(self):
        if self._bus is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._bus.guard(
            self._device_name,
            self._baudrate,
            timeout_s=self._timeout_s,
        )

    def _communication_error(self, action: str, exc: Exception) -> SpincoaterCommunicationError:
        return SpincoaterCommunicationError(
            human_message=f"旋涂电机 {action} 通信失败",
            agent_message=(
                f"Verified MotorController failed during {action}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

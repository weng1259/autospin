"""Tests for wrapping the verified AutoSpinmotorSystem MotorController."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from src.hardware.autospinmotor_adapters.spin_motor_adapter import (
    SpinMotorControllerAdapter,
)
from src.hardware.rs485_bus import Rs485Bus
from src.hardware.spincoater_backend import (
    SpincoaterBackend,
    SpincoaterCommunicationError,
    SpincoaterConfig,
)
from src.system_estop import SystemEstop


class FakeGuardBus:
    def __init__(self) -> None:
        self.guards: list[tuple[str, int, float]] = []

    @contextmanager
    def guard(
        self,
        device: str,
        baudrate: int,
        *,
        timeout_s: float = 1.0,
    ) -> Iterator[None]:
        self.guards.append((device, baudrate, timeout_s))
        yield


class FakeMotorController:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.connected = False
        self.running = False
        self.target_speed = 0.0
        self.actual_speed = 0.0
        self.connect_ok = True
        self.start_ok = True
        self.set_speed_ok = True
        self.stop_ok = True

    def connect(self) -> bool:
        self.calls.append(("connect",))
        self.connected = self.connect_ok
        return self.connect_ok

    def close(self) -> None:
        self.calls.append(("close",))
        self.connected = False

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        self.connected = False
        self.running = False

    def start(
        self, direction: str = "forward", wait_for_stop: bool = True
    ) -> tuple[bool, str]:
        self.calls.append(("start", direction, wait_for_stop))
        self.running = self.start_ok
        return self.start_ok, "started" if self.start_ok else "start failed"

    def set_speed(self, rpm: float, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append(("set_speed", rpm, kwargs))
        if self.set_speed_ok:
            self.target_speed = rpm
            self.actual_speed = rpm
        return self.set_speed_ok, "speed set" if self.set_speed_ok else "speed failed"

    def stop(self, use_brake: bool = True) -> bool:
        self.calls.append(("stop", use_brake))
        if self.stop_ok:
            self.running = False
            self.target_speed = 0.0
        return self.stop_ok

    def get_actual_speed(self) -> float:
        self.calls.append(("get_actual_speed",))
        return self.actual_speed

    def get_status(self) -> dict[str, Any]:
        self.calls.append(("get_status",))
        return {
            "connected": self.connected,
            "running": self.running,
            "target_speed": self.target_speed,
        }


def _adapter(
    controller: FakeMotorController,
    bus: FakeGuardBus | None = None,
) -> SpinMotorControllerAdapter:
    return SpinMotorControllerAdapter(
        controller,
        bus=cast(Rs485Bus, bus) if bus is not None else None,
        baudrate=9600,
        timeout_s=2.0,
        max_rpm=3000.0,
    )


def test_adapter_start_preserves_verified_controller_order() -> None:
    controller = FakeMotorController()
    bus = FakeGuardBus()
    adapter = _adapter(controller, bus)

    adapter.connect()
    result = adapter.start(1200.0, idempotency_key="adapter-start-1200")

    assert result.success is True
    assert result.target_rpm == 1200.0
    assert controller.calls == [
        ("connect",),
        ("start", "forward", True),
        ("set_speed", 1200.0, {}),
    ]
    assert bus.guards == [
        ("spincoater", 9600, 2.0),
        ("spincoater", 9600, 2.0),
    ]


def test_adapter_stop_preserves_brake_flag() -> None:
    controller = FakeMotorController()
    adapter = _adapter(controller)
    adapter.connect()

    result = adapter.stop(use_brake=True, idempotency_key="adapter-stop-brake")

    assert result.success is True
    assert result.brake_engaged is True
    assert ("stop", True) in controller.calls


def test_adapter_failed_start_is_structured_error() -> None:
    controller = FakeMotorController()
    controller.start_ok = False
    adapter = _adapter(controller)
    adapter.connect()

    with pytest.raises(SpincoaterCommunicationError) as exc_info:
        adapter.start(500.0, idempotency_key="adapter-start-fails")

    assert "wait_for_stop=True" in exc_info.value.agent_message
    assert ("set_speed", 500.0, {}) not in controller.calls


def test_spincoater_backend_can_use_motorcontroller_adapter() -> None:
    controller = FakeMotorController()
    adapter = _adapter(controller)
    backend = SpincoaterBackend(
        cast(Rs485Bus, object()),
        unit_id=2,
        config=SpincoaterConfig(max_rpm=3000.0),
        controller_adapter=adapter,
    )

    backend.connect()
    result = backend.start(800.0, idempotency_key="backend-adapter-start")
    status = backend.status()

    assert result.success is True
    assert status.connected is True
    assert status.running is True
    assert status.target_rpm == 800.0
    assert ("start", "forward", True) in controller.calls
    assert ("set_speed", 800.0, {}) in controller.calls


def test_system_estop_keeps_spincoater_brake_default_with_adapter() -> None:
    controller = FakeMotorController()
    adapter = _adapter(controller)
    backend = SpincoaterBackend(
        cast(Rs485Bus, object()),
        unit_id=2,
        config=SpincoaterConfig(max_rpm=3000.0),
        controller_adapter=adapter,
    )
    backend.connect()

    report = SystemEstop(spincoater=backend).halt_all()

    assert report.ok is True
    assert ("stop", True) in controller.calls

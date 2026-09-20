"""Tests for wrapping the verified AutoSpinmotorSystem HeatingStageController."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from src.hardware.autospinmotor_adapters.heater_adapter import HeaterControllerAdapter
from src.hardware.heater_backend import (
    HeaterBackend,
    HeaterCommunicationError,
    HeaterConfig,
    HeaterSetpointOutOfRangeError,
)
from src.hardware.rs485_bus import Rs485Bus
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


class FakeHeatingStageController:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.connected = False
        self.pv = 24.5
        self.sv: float | None = None
        self.connect_ok = True
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

    def read_pv(self) -> float:
        self.calls.append(("read_pv",))
        return self.pv

    def read_sv(self) -> float:
        self.calls.append(("read_sv",))
        return 0.0 if self.sv is None else self.sv

    def write_sv(self, temp_c: float) -> None:
        self.calls.append(("write_sv", temp_c))
        self.sv = temp_c

    def run(self) -> None:
        self.calls.append(("run",))

    def stop(self) -> bool:
        self.calls.append(("stop",))
        return self.stop_ok

    def get_status(self) -> dict[str, Any]:
        self.calls.append(("get_status",))
        return {
            "connected": self.connected,
            "pv": self.pv,
            "sv": self.sv,
        }


def _adapter(
    controller: FakeHeatingStageController,
    bus: FakeGuardBus | None = None,
) -> HeaterControllerAdapter:
    return HeaterControllerAdapter(
        controller,
        bus=cast(Rs485Bus, bus) if bus is not None else None,
        baudrate=9600,
        timeout_s=3.0,
        sv_max_c=150.0,
    )


def test_adapter_connect_and_read_pv_use_guarded_verified_controller() -> None:
    controller = FakeHeatingStageController()
    bus = FakeGuardBus()
    adapter = _adapter(controller, bus)

    adapter.connect()
    status = adapter.read_pv()

    assert status.connected is True
    assert status.pv_c == 24.5
    assert controller.calls == [
        ("connect",),
        ("read_pv",),
        ("get_status",),
    ]
    assert bus.guards == [
        ("heater", 9600, 3.0),
        ("heater", 9600, 3.0),
    ]


def test_adapter_set_sv_preserves_controller_write_sv() -> None:
    controller = FakeHeatingStageController()
    adapter = _adapter(controller)
    adapter.connect()

    result = adapter.set_sv(80.0, idempotency_key="adapter-heater-80")

    assert result.success is True
    assert result.target_sv_c == 80.0
    assert ("write_sv", 80.0) in controller.calls
    assert controller.sv == 80.0


def test_adapter_out_of_range_sv_is_structured_error_and_sends_no_write() -> None:
    controller = FakeHeatingStageController()
    adapter = _adapter(controller)

    with pytest.raises(HeaterSetpointOutOfRangeError):
        adapter.set_sv(150.1, idempotency_key="adapter-heater-too-hot")

    assert not any(call[0] == "write_sv" for call in controller.calls)


def test_adapter_connect_failure_is_structured_error() -> None:
    controller = FakeHeatingStageController()
    controller.connect_ok = False
    adapter = _adapter(controller)

    with pytest.raises(HeaterCommunicationError) as exc_info:
        adapter.connect()

    assert "connect() returned False" in exc_info.value.agent_message


def test_heater_backend_can_use_controller_adapter() -> None:
    controller = FakeHeatingStageController()
    adapter = _adapter(controller)
    backend = HeaterBackend(
        cast(Rs485Bus, object()),
        unit_id=3,
        config=HeaterConfig(sv_max_c=150.0),
        controller_adapter=adapter,
    )

    backend.connect()
    result = backend.set_sv(65.0, idempotency_key="backend-heater-adapter-65")
    status = backend.status()

    assert result.success is True
    assert status.connected is True
    assert status.pv_c == 24.5
    assert status.sv_c == 65.0
    assert ("write_sv", 65.0) in controller.calls


def test_system_estop_sets_adapter_backed_heater_sv_to_zero() -> None:
    controller = FakeHeatingStageController()
    adapter = _adapter(controller)
    backend = HeaterBackend(
        cast(Rs485Bus, object()),
        unit_id=3,
        config=HeaterConfig(sv_max_c=150.0),
        controller_adapter=adapter,
    )
    backend.connect()

    report = SystemEstop(heater=backend).halt_all()

    assert report.ok is True
    assert ("write_sv", 0.0) in controller.calls

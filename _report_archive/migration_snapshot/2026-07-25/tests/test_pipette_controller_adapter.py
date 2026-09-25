from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from src.hardware.autospinmotor_adapters import PipetteControllerAdapter
from src.hardware.pipette_backend import (
    PipetteBackend,
    PipetteConfig,
    PipetteTipMissingError,
    PipetteVolumeOutOfRangeError,
)
from src.hardware.rs485_bus import Rs485Bus


class FakeBus:
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


class FakeController:
    max_volume = 1000

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.connected = False
        self.initialized = False
        self.tip = True
        self.position = 0

    def connect(self, initialize: bool = True) -> bool:
        self.calls.append(("connect", (), {"initialize": initialize}))
        self.connected = True
        return True

    def close(self) -> None:
        self.calls.append(("close", (), {}))
        self.connected = False

    def shutdown(self) -> None:
        self.calls.append(("shutdown", (), {}))
        self.connected = False

    def home(self, timeout: float = 30) -> bool:
        self.calls.append(("home", (timeout,), {}))
        self.initialized = True
        return True

    def aspirate(self, volume: int, detect_mask: int = 0) -> bool:
        self.calls.append(("aspirate", (volume, detect_mask), {}))
        if not self.tip:
            raise RuntimeError("Tip missing")
        return True

    def dispense(self, volume: int) -> bool:
        self.calls.append(("dispense", (volume,), {}))
        return True

    def blowout(self) -> bool:
        self.calls.append(("blowout", (), {}))
        return True

    def tip_eject(self) -> bool:
        self.calls.append(("tip_eject", (), {}))
        self.tip = False
        return True

    def liquid_detect(self, timeout: float = 10) -> bool:
        self.calls.append(("liquid_detect", (timeout,), {}))
        return True

    def stop(self) -> bool:
        self.calls.append(("stop", (), {}))
        return True

    def reset(self) -> bool:
        self.calls.append(("reset", (), {}))
        self.initialized = True
        return True

    def get_status(self, *, live: bool = False) -> dict[str, Any]:
        self.calls.append(("get_status", (), {"live": live}))
        return {
            "is_initialized": self.initialized,
            "status_word": 0,
            "homed": self.initialized,
            "tip_present": self.tip,
            "position": self.position,
            "driver_fault": False,
        }


def test_adapter_maps_verified_pipette_controller_and_uses_bus_guard() -> None:
    controller = FakeController()
    bus = FakeBus()
    adapter = PipetteControllerAdapter(
        controller,
        bus=cast(Rs485Bus, bus),
        max_volume_ul=1000,
    )

    adapter.connect()
    home = adapter.home(idempotency_key="home")
    aspirate = adapter.aspirate(50.4, idempotency_key="asp")
    eject = adapter.tip_eject(idempotency_key="eject")

    assert home.action == "home"
    assert aspirate.action == "aspirate"
    assert aspirate.volume_ul == 50.4
    assert eject.action == "eject_tip"
    assert ("aspirate", (50, 0), {}) in controller.calls
    assert bus.guards[:4] == [
        ("pipette", 115200, 2.0),
        ("pipette", 115200, 2.0),
        ("pipette", 115200, 2.0),
        ("pipette", 115200, 2.0),
    ]


def test_adapter_dry_run_and_volume_guard_do_not_call_controller() -> None:
    controller = FakeController()
    adapter = PipetteControllerAdapter(controller, max_volume_ul=1000)

    dry = adapter.aspirate(25, idempotency_key="dry", dry_run=True)

    assert dry.dry_run is True
    assert not any(call[0] == "aspirate" for call in controller.calls)
    with pytest.raises(PipetteVolumeOutOfRangeError):
        adapter.dispense(1001, idempotency_key="too-large")


def test_backend_can_route_through_pipette_adapter() -> None:
    controller = FakeController()
    adapter = PipetteControllerAdapter(controller)
    backend = PipetteBackend(
        cast(Rs485Bus, object()),
        unit_id=1,
        config=PipetteConfig(max_volume_ul=1000),
        controller_adapter=adapter,
    )

    backend.connect()
    assert backend.get_status().connected is True
    assert backend.home(idempotency_key="home").success is True
    assert backend.tip_eject(idempotency_key="tip").success is True
    assert backend.blowout(idempotency_key="blow").success is True
    assert backend.liquid_detect(idempotency_key="liq").success is True
    assert backend.reset().success is True
    backend.disconnect()
    assert backend.status().connected is False


def test_adapter_maps_tip_missing_to_structured_error() -> None:
    controller = FakeController()
    controller.tip = False
    adapter = PipetteControllerAdapter(controller)

    with pytest.raises(PipetteTipMissingError):
        adapter.aspirate(50, idempotency_key="missing")

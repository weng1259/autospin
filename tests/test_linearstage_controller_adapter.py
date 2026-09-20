from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from src.hardware.autospinmotor_adapters import LinearStageControllerAdapter
from src.hardware.linearstage_backend import (
    LinearStageBackend,
    LinearStageConfig,
    LinearStagePositionOutOfRangeError,
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


class FakeStage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.connected = False
        self.position = 0.0

    def connect(self) -> bool:
        self.calls.append(("connect", (), {}))
        self.connected = True
        return True

    def close(self) -> None:
        self.calls.append(("close", (), {}))
        self.connected = False

    def home(self, *, direction: int | None = None, timeout_s: float = 30.0) -> bytes:
        self.calls.append(("home", (), {"direction": direction, "timeout_s": timeout_s}))
        self.position = 0.0
        return b"home"

    def move_absolute(self, target_mm: float, **kwargs: Any) -> bytes:
        self.calls.append(("move_absolute", (target_mm,), kwargs))
        self.position = target_mm
        return b"abs"

    def move_relative(self, distance_mm: float, **kwargs: Any) -> bytes:
        self.calls.append(("move_relative", (distance_mm,), kwargs))
        self.position += distance_mm
        return b"rel"

    def read_position_mm(self) -> float:
        self.calls.append(("read_position_mm", (), {}))
        return self.position

    def read_flags(self) -> int:
        self.calls.append(("read_flags", (), {}))
        return 0x03

    def read_home_status(self) -> int:
        self.calls.append(("read_home_status", (), {}))
        return 0

    def stop(self) -> bytes:
        self.calls.append(("stop", (), {}))
        return b"stop"

    def reset_protection(self) -> bytes:
        self.calls.append(("reset_protection", (), {}))
        return b"reset"

    def get_status(self, *, live: bool = False) -> dict[str, Any]:
        self.calls.append(("get_status", (), {"live": live}))
        return {
            "connected": self.connected,
            "commanded_position_mm": self.position,
            "flags_raw": 0x03,
            "home_status_raw": 0,
        }


def test_adapter_maps_verified_linear_stage_and_uses_bus_guard() -> None:
    stage = FakeStage()
    bus = FakeBus()
    adapter = LinearStageControllerAdapter(
        stage,
        bus=cast(Rs485Bus, bus),
        max_position_mm=100,
    )

    adapter.connect()
    home = adapter.home(idempotency_key="home")
    move = adapter.move_to(10, idempotency_key="move")
    rel = adapter.move_relative(-2, idempotency_key="rel")
    stop = adapter.stop()

    assert home.action == "home"
    assert move.final_position_mm == 10
    assert rel.final_position_mm == 8
    assert stop.action == "stop"
    assert ("move_absolute", (10.0,), {}) in stage.calls
    assert ("move_relative", (-2.0,), {}) in stage.calls
    assert all(guard == ("linear_stage", 115200, 0.5) for guard in bus.guards)


def test_adapter_dry_run_and_range_guard_do_not_call_controller() -> None:
    stage = FakeStage()
    adapter = LinearStageControllerAdapter(stage, max_position_mm=100)

    dry = adapter.move_to(20, idempotency_key="dry", dry_run=True)

    assert dry.dry_run is True
    assert not any(call[0] == "move_absolute" for call in stage.calls)
    with pytest.raises(LinearStagePositionOutOfRangeError):
        adapter.move_to(101, idempotency_key="range")


def test_backend_can_route_through_linearstage_adapter() -> None:
    stage = FakeStage()
    adapter = LinearStageControllerAdapter(stage, max_position_mm=100)
    backend = LinearStageBackend(
        cast(Rs485Bus, object()),
        address=4,
        config=LinearStageConfig(travel_mm=100),
        controller_adapter=adapter,
    )

    backend.connect()
    backend.home(idempotency_key="home")
    assert backend.move_absolute(15, idempotency_key="abs").final_position_mm == 15
    assert backend.move_relative(5, idempotency_key="rel").final_position_mm == 20
    assert ("move_absolute", (20.0,), {}) in stage.calls
    assert backend.get_position() == 20
    assert backend.reset_protection().success is True
    backend.shutdown()
    assert backend.get_status().connected is False


def test_backend_routes_move_over_50_mm_as_one_direct_command() -> None:
    stage = FakeStage()
    adapter = LinearStageControllerAdapter(stage, max_position_mm=100)
    backend = LinearStageBackend(
        cast(Rs485Bus, object()),
        address=4,
        config=LinearStageConfig(travel_mm=100),
        controller_adapter=adapter,
    )

    backend.connect()
    backend.home(idempotency_key="home-before-direct-move")
    stage.position = 80.0

    result = backend.move_to(10.0, idempotency_key="direct-80-to-10")

    absolute_targets = [
        call[1][0] for call in stage.calls if call[0] == "move_absolute"
    ]
    assert absolute_targets == [10.0]
    assert result.target_position_mm == 10.0
    assert result.final_position_mm == 10.0

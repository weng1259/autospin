"""HeaterBackend protocol and safety tests; all I/O uses in-memory fakes."""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest

from src.hardware.errors import L3Error
from src.hardware.heater_backend import (
    HeaterBackend,
    HeaterCommunicationError,
    HeaterConfig,
    HeaterSetpointOutOfRangeError,
)
from src.hardware.rs485_bus import Rs485Bus


# Register source required by W1.1:
# autospin_system/hardware/heating_stage/heating_stage_controller.py:26-31
# defines PV=74, SV=0, Srun=27 and scale=10; lines 133-146 define the two
# writes performed by set_sv (SV first, then Srun=0).
PV_READ_REQUEST = bytes.fromhex("03 03 00 4A 00 01 A4 3E")
PV_24_1_RESPONSE = bytes.fromhex("03 03 02 00 F1 00 00")
SV_80_WRITE_REQUEST = bytes.fromhex("03 06 00 00 03 20 89 00")
SRUN_WRITE_REQUEST = bytes.fromhex("03 06 00 1B 00 00 F8 2F")


class FakeSerial:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = deque(responses)
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        if not self.responses:
            return b""
        response = self.responses.popleft()
        assert len(response) <= size
        return response


class FakeBus:
    def __init__(self, responses: list[bytes]) -> None:
        self.serial = FakeSerial(responses)
        self.connect_count = 0
        self.close_count = 0
        self.transactions: list[tuple[str, int, float]] = []

    def connect(self) -> None:
        self.connect_count += 1

    def close(self) -> None:
        self.close_count += 1

    @contextmanager
    def transaction(
        self,
        device: str,
        baudrate: int,
        *,
        timeout_s: float = 1.0,
    ) -> Iterator[FakeSerial]:
        self.transactions.append((device, baudrate, timeout_s))
        yield self.serial


def _backend(fake_bus: FakeBus, *, sv_max_c: float = 150.0) -> HeaterBackend:
    return HeaterBackend(
        cast(Rs485Bus, fake_bus),
        unit_id=3,
        config=HeaterConfig(sv_max_c=sv_max_c),
    )


def test_set_sv_frames_match_ai516p_reference() -> None:
    """SV=80.0 uses register 0/raw 800, followed by Srun register 27/raw 0."""
    fake_bus = FakeBus(
        [PV_24_1_RESPONSE, SV_80_WRITE_REQUEST, SRUN_WRITE_REQUEST]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.set_sv(80.0, idempotency_key="heater-frame-80")

    assert fake_bus.serial.writes == [
        PV_READ_REQUEST,
        SV_80_WRITE_REQUEST,
        SRUN_WRITE_REQUEST,
    ]
    assert fake_bus.transactions == [
        ("heater", 9600, 3.0),
        ("heater", 9600, 3.0),
        ("heater", 9600, 3.0),
    ]
    assert result.success is True
    assert result.target_sv_c == 80.0
    assert result.dry_run is False
    assert "register 0" in result.action_description
    assert "Srun=0" in result.action_description
    assert backend.status().sv_c == 80.0


def test_sv_above_hard_limit_is_structured_error_and_sends_no_bytes() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus, sv_max_c=150.0)

    with pytest.raises(HeaterSetpointOutOfRangeError) as exc_info:
        backend.set_sv(150.1, idempotency_key="heater-limit-150-1")

    error = exc_info.value
    assert isinstance(error, L3Error)
    assert error.error_code == "L3.HEATER_SV_OUT_OF_RANGE"
    assert error.human_message
    assert error.agent_message
    assert error.suggested_action_zh
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


def test_same_idempotency_key_only_sends_set_sv_once() -> None:
    fake_bus = FakeBus(
        [PV_24_1_RESPONSE, SV_80_WRITE_REQUEST, SRUN_WRITE_REQUEST]
    )
    backend = _backend(fake_bus)
    backend.connect()

    first = backend.set_sv(80.0, idempotency_key="heater-idem-set-80")
    second = backend.set_sv(80.0, idempotency_key="heater-idem-set-80")

    assert second == first
    assert fake_bus.serial.writes == [
        PV_READ_REQUEST,
        SV_80_WRITE_REQUEST,
        SRUN_WRITE_REQUEST,
    ]


def test_set_sv_dry_run_validates_and_sends_no_bytes() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    result = backend.set_sv(
        80.0,
        idempotency_key="heater-dry-run-80",
        dry_run=True,
    )

    assert result.success is True
    assert result.dry_run is True
    assert result.target_sv_c == 80.0
    assert "raw=800" in result.action_description
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


def test_crc_error_is_structured_and_not_swallowed() -> None:
    bad_crc = PV_24_1_RESPONSE[:-1] + b"\x01"
    fake_bus = FakeBus([bad_crc])
    backend = _backend(fake_bus)

    with pytest.raises(HeaterCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3Error)
    assert exc_info.value.error_code == "L3.HEATER_COMMUNICATION"
    assert "CRC" in exc_info.value.human_message
    assert exc_info.value.suggested_action_zh
    assert backend.status().connected is False


def test_timeout_is_structured_and_not_returned_as_false() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    with pytest.raises(HeaterCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3Error)
    assert exc_info.value.error_code == "L3.HEATER_COMMUNICATION"
    assert "响应超时" in exc_info.value.human_message
    assert backend.status().connected is False


def test_connect_is_idempotent_and_status_is_cached_without_bus_io() -> None:
    fake_bus = FakeBus([PV_24_1_RESPONSE])
    backend = _backend(fake_bus)

    backend.connect()
    backend.connect()
    transactions_after_connect = list(fake_bus.transactions)
    first = backend.status()
    second = backend.status()

    assert fake_bus.connect_count == 1
    assert fake_bus.transactions == transactions_after_connect
    assert first.connected is True
    assert first.pv_c == 24.1
    assert first.timestamp is not None
    assert first.last_update_ms_ago is not None
    assert second.last_update_ms_ago is not None
    assert second.last_update_ms_ago >= first.last_update_ms_ago

"""PipetteBackend protocol and safety tests; all I/O uses in-memory fakes."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast

import pytest

from src.hardware.errors import ConnectionError as L3ConnectionError
from src.hardware.errors import L3Error
from src.hardware.pipette_backend import (
    PipetteActionTimeoutError,
    PipetteBackend,
    PipetteCommunicationError,
    PipetteConfig,
    PipetteNotHomedError,
    PipetteTipMissingError,
    PipetteVolumeOutOfRangeError,
)
from src.hardware.rs485_bus import Rs485Bus


# Frame sources required by W1.3 (fixed Pi SoR commit 1d26520):
# - AutoSpinmotorSystem/hardware/pipette/pipette_controller.py:59-89 defines the
#   corrected one-based actions and 50/1250/1250 motion defaults;
# - lines 170-180 atomically read signed POS_H/POS_L;
# - lines 253-292 define IDLE->HOME, homed polling, and timeout IMM_STOP;
# - lines 294-366 and 378-385 define aspirate/dispense/eject register writes;
# - driver_communication.py:90-141 defines standard Modbus holding-register
#   writes and input-register reads.  Literals below independently pin the CRC.
STATUS_READ_REQUEST = bytes.fromhex("01 04 00 00 00 03 B0 0B")
POSITION_READ_REQUEST = bytes.fromhex("01 04 00 03 00 02 81 CB")
TIP_READ_REQUEST = bytes.fromhex("01 04 00 0D 00 01 A0 09")
HOME_STATE_READ_REQUEST = bytes.fromhex("01 04 00 00 00 02 71 CB")
STATUS_WORD_READ_REQUEST = bytes.fromhex("01 04 00 00 00 01 31 CA")
ASPIRATE_STATE_READ_REQUEST = bytes.fromhex("01 04 00 09 00 01 E1 C8")
DISPENSE_STATE_READ_REQUEST = bytes.fromhex("01 04 00 0A 00 01 11 C8")

VELOCITY_50_WRITE_REQUEST = bytes.fromhex("01 06 00 03 00 32 F8 1F")
ACCEL_1250_WRITE_REQUEST = bytes.fromhex("01 06 00 04 04 E2 4A 82")
DECEL_1250_WRITE_REQUEST = bytes.fromhex("01 06 00 05 04 E2 1B 42")
IDLE_WRITE_REQUEST = bytes.fromhex("01 06 00 00 00 00 89 CA")
HOME_WRITE_REQUEST = bytes.fromhex("01 06 00 00 00 01 48 0A")
IMM_STOP_WRITE_REQUEST = bytes.fromhex("01 06 00 00 00 08 88 0C")
ASPIRATE_WRITE_REQUEST = bytes.fromhex("01 06 00 00 00 0A 09 CD")
DISPENSE_WRITE_REQUEST = bytes.fromhex("01 06 00 00 00 0B C8 0D")
EJECT_TIP_WRITE_REQUEST = bytes.fromhex("01 06 00 00 00 0C 89 CF")
VOLUME_50_WRITE_REQUEST = bytes.fromhex(
    "01 10 00 06 00 02 04 00 00 00 32 F2 50"
)
VOLUME_50_WRITE_RESPONSE = bytes.fromhex("01 10 00 06 00 02 A1 C9")

SNAPSHOT_READY_RESPONSE = bytes.fromhex("01 04 06 00 00 00 01 00 00 31 53")
SNAPSHOT_UNHOMED_RESPONSE = bytes.fromhex("01 04 06 00 00 00 00 00 00 60 93")
POSITION_ZERO_RESPONSE = bytes.fromhex("01 04 04 00 00 00 00 FB 84")
POSITION_NEGATIVE_ONE_RESPONSE = bytes.fromhex("01 04 04 FF FF FF FF FA 10")
TIP_PRESENT_RESPONSE = bytes.fromhex("01 04 02 00 01 78 F0")
TIP_ABSENT_RESPONSE = bytes.fromhex("01 04 02 00 00 B9 30")
HOME_ACTIVE_RESPONSE = bytes.fromhex("01 04 04 00 04 00 00 BA 45")
HOME_DONE_RESPONSE = bytes.fromhex("01 04 04 00 00 00 01 3A 44")
HOME_INCOMPLETE_RESPONSE = bytes.fromhex("01 04 04 00 00 00 00 FB 84")
ACTION_ACTIVE_RESPONSE = bytes.fromhex("01 04 02 00 01 78 F0")
ACTION_IDLE_RESPONSE = bytes.fromhex("01 04 02 00 00 B9 30")
STATUS_ACTIVE_RESPONSE = bytes.fromhex("01 04 02 00 04 B8 F3")
STATUS_IDLE_RESPONSE = ACTION_IDLE_RESPONSE


ResponseFactory = Callable[[bytes, int], bytes]


class FakeSerial:
    def __init__(
        self,
        responses: list[bytes],
        *,
        response_factory: ResponseFactory | None = None,
    ) -> None:
        self.responses = deque(responses)
        self.response_factory = response_factory
        self.writes: list[bytes] = []
        self._last_write: bytes | None = None

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        self._last_write = data
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        if self.responses:
            response = self.responses.popleft()
        elif self.response_factory is not None:
            assert self._last_write is not None
            response = self.response_factory(self._last_write, size)
        else:
            return b""
        assert len(response) <= size
        return response


class FakeBus:
    def __init__(
        self,
        responses: list[bytes],
        *,
        response_factory: ResponseFactory | None = None,
    ) -> None:
        self.serial = FakeSerial(responses, response_factory=response_factory)
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


def _backend(
    fake_bus: FakeBus,
    *,
    max_volume_ul: float = 1000.0,
    home_timeout_s: float = 0.05,
    action_timeout_s: float = 0.05,
) -> PipetteBackend:
    return PipetteBackend(
        cast(Rs485Bus, fake_bus),
        unit_id=1,
        config=PipetteConfig(
            max_volume_ul=max_volume_ul,
            home_timeout_s=home_timeout_s,
            action_timeout_s=action_timeout_s,
            poll_interval_s=0.001,
        ),
    )


def _ready_snapshot_responses(*, tip_present: bool = True) -> list[bytes]:
    return [
        SNAPSHOT_READY_RESPONSE,
        POSITION_ZERO_RESPONSE,
        TIP_PRESENT_RESPONSE if tip_present else TIP_ABSENT_RESPONSE,
    ]


def test_home_frames_include_corrected_code_and_reviewed_motion_defaults() -> None:
    fake_bus = FakeBus(
        [
            SNAPSHOT_UNHOMED_RESPONSE,
            POSITION_ZERO_RESPONSE,
            TIP_PRESENT_RESPONSE,
            VELOCITY_50_WRITE_REQUEST,
            ACCEL_1250_WRITE_REQUEST,
            DECEL_1250_WRITE_REQUEST,
            IDLE_WRITE_REQUEST,
            HOME_WRITE_REQUEST,
            HOME_ACTIVE_RESPONSE,
            HOME_DONE_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.home(idempotency_key="pipette-home-frame-corrected")

    assert fake_bus.serial.writes == [
        STATUS_READ_REQUEST,
        POSITION_READ_REQUEST,
        TIP_READ_REQUEST,
        VELOCITY_50_WRITE_REQUEST,
        ACCEL_1250_WRITE_REQUEST,
        DECEL_1250_WRITE_REQUEST,
        IDLE_WRITE_REQUEST,
        HOME_WRITE_REQUEST,
        HOME_STATE_READ_REQUEST,
        HOME_STATE_READ_REQUEST,
    ]
    assert all(
        transaction == ("pipette", 115200, 2.0)
        for transaction in fake_bus.transactions
    )
    assert result.success is True
    assert result.action == "home"
    assert result.dry_run is False
    assert backend.status().homed is True
    assert backend.status().position_steps == 0


def test_aspirate_frame_matches_reference_volume_and_action_code() -> None:
    fake_bus = FakeBus(
        _ready_snapshot_responses()
        + [
            TIP_PRESENT_RESPONSE,
            VOLUME_50_WRITE_RESPONSE,
            ASPIRATE_WRITE_REQUEST,
            ACTION_ACTIVE_RESPONSE,
            ACTION_IDLE_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.aspirate(50.0, idempotency_key="pipette-aspirate-frame-50")

    assert fake_bus.serial.writes == [
        STATUS_READ_REQUEST,
        POSITION_READ_REQUEST,
        TIP_READ_REQUEST,
        TIP_READ_REQUEST,
        VOLUME_50_WRITE_REQUEST,
        ASPIRATE_WRITE_REQUEST,
        ASPIRATE_STATE_READ_REQUEST,
        ASPIRATE_STATE_READ_REQUEST,
    ]
    assert result.action == "aspirate"
    assert result.volume_ul == 50.0
    assert result.success is True


def test_dispense_frame_matches_reference_volume_and_action_code() -> None:
    fake_bus = FakeBus(
        _ready_snapshot_responses()
        + [
            TIP_PRESENT_RESPONSE,
            VOLUME_50_WRITE_RESPONSE,
            DISPENSE_WRITE_REQUEST,
            ACTION_ACTIVE_RESPONSE,
            ACTION_IDLE_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.dispense(50.0, idempotency_key="pipette-dispense-frame-50")

    assert fake_bus.serial.writes == [
        STATUS_READ_REQUEST,
        POSITION_READ_REQUEST,
        TIP_READ_REQUEST,
        TIP_READ_REQUEST,
        VOLUME_50_WRITE_REQUEST,
        DISPENSE_WRITE_REQUEST,
        DISPENSE_STATE_READ_REQUEST,
        DISPENSE_STATE_READ_REQUEST,
    ]
    assert result.action == "dispense"
    assert result.volume_ul == 50.0
    assert result.success is True


def test_dispense_tolerates_one_transient_tip_absence_before_command() -> None:
    fake_bus = FakeBus(
        _ready_snapshot_responses()
        + [
            TIP_ABSENT_RESPONSE,
            TIP_PRESENT_RESPONSE,
            VOLUME_50_WRITE_RESPONSE,
            DISPENSE_WRITE_REQUEST,
            ACTION_ACTIVE_RESPONSE,
            ACTION_IDLE_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.dispense(50.0, idempotency_key="pipette-transient-tip")

    assert fake_bus.serial.writes.count(TIP_READ_REQUEST) == 3
    assert DISPENSE_WRITE_REQUEST in fake_bus.serial.writes
    assert result.success is True


def test_eject_tip_frame_waits_until_tip_absent_and_idle() -> None:
    fake_bus = FakeBus(
        _ready_snapshot_responses()
        + [
            TIP_PRESENT_RESPONSE,
            EJECT_TIP_WRITE_REQUEST,
            STATUS_ACTIVE_RESPONSE,
            TIP_PRESENT_RESPONSE,
            STATUS_IDLE_RESPONSE,
            TIP_ABSENT_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.eject_tip(idempotency_key="pipette-eject-frame")

    assert fake_bus.serial.writes == [
        STATUS_READ_REQUEST,
        POSITION_READ_REQUEST,
        TIP_READ_REQUEST,
        TIP_READ_REQUEST,
        EJECT_TIP_WRITE_REQUEST,
        STATUS_WORD_READ_REQUEST,
        TIP_READ_REQUEST,
        STATUS_WORD_READ_REQUEST,
        TIP_READ_REQUEST,
    ]
    assert result.action == "eject_tip"
    assert result.success is True
    assert backend.status().tip_present is False


def test_volume_above_hard_limit_is_structured_error_and_sends_no_bytes() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus, max_volume_ul=1000.0)

    with pytest.raises(PipetteVolumeOutOfRangeError) as exc_info:
        backend.aspirate(1000.1, idempotency_key="pipette-limit-1000-1")

    error = exc_info.value
    assert isinstance(error, L3Error)
    assert error.error_code == "L3.PIPETTE_VOLUME_OUT_OF_RANGE"
    assert error.human_message
    assert error.agent_message
    assert error.suggested_action_zh
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


def test_aspirate_before_home_is_structured_error_without_action_bytes() -> None:
    fake_bus = FakeBus(
        [SNAPSHOT_UNHOMED_RESPONSE, POSITION_ZERO_RESPONSE, TIP_PRESENT_RESPONSE]
    )
    backend = _backend(fake_bus)
    backend.connect()
    writes_after_connect = list(fake_bus.serial.writes)

    with pytest.raises(PipetteNotHomedError) as exc_info:
        backend.aspirate(50.0, idempotency_key="pipette-unhomed-aspirate")

    assert exc_info.value.error_code == "L3.PIPETTE_NOT_HOMED"
    assert "home" in exc_info.value.suggested_action
    assert fake_bus.serial.writes == writes_after_connect


def test_missing_tip_is_structured_error_before_liquid_motion() -> None:
    fake_bus = FakeBus(
        _ready_snapshot_responses(tip_present=False)
        + [TIP_ABSENT_RESPONSE, TIP_ABSENT_RESPONSE, TIP_ABSENT_RESPONSE]
    )
    backend = _backend(fake_bus)
    backend.connect()

    with pytest.raises(PipetteTipMissingError) as exc_info:
        backend.dispense(50.0, idempotency_key="pipette-tip-missing")

    assert exc_info.value.error_code == "L3.PIPETTE_TIP_MISSING"
    assert VOLUME_50_WRITE_REQUEST not in fake_bus.serial.writes
    assert DISPENSE_WRITE_REQUEST not in fake_bus.serial.writes


def test_home_timeout_sends_imm_stop_then_raises_structured_error() -> None:
    def response_factory(request: bytes, _size: int) -> bytes:
        if request == IMM_STOP_WRITE_REQUEST:
            return IMM_STOP_WRITE_REQUEST
        assert request == HOME_STATE_READ_REQUEST
        return HOME_INCOMPLETE_RESPONSE

    fake_bus = FakeBus(
        [
            SNAPSHOT_UNHOMED_RESPONSE,
            POSITION_ZERO_RESPONSE,
            TIP_PRESENT_RESPONSE,
            VELOCITY_50_WRITE_REQUEST,
            ACCEL_1250_WRITE_REQUEST,
            DECEL_1250_WRITE_REQUEST,
            IDLE_WRITE_REQUEST,
            HOME_WRITE_REQUEST,
        ],
        response_factory=response_factory,
    )
    backend = _backend(fake_bus, home_timeout_s=0.005)
    backend.connect()

    with pytest.raises(PipetteActionTimeoutError) as exc_info:
        backend.home(idempotency_key="pipette-home-timeout-stop")

    assert exc_info.value.error_code == "L3.PIPETTE_ACTION_TIMEOUT"
    assert exc_info.value.action == "home"
    assert IMM_STOP_WRITE_REQUEST in fake_bus.serial.writes
    assert fake_bus.serial.writes[-1] == IMM_STOP_WRITE_REQUEST
    assert backend.status().connected is True


def test_same_idempotency_key_only_sends_aspirate_once() -> None:
    fake_bus = FakeBus(
        _ready_snapshot_responses()
        + [
            TIP_PRESENT_RESPONSE,
            VOLUME_50_WRITE_RESPONSE,
            ASPIRATE_WRITE_REQUEST,
            ACTION_ACTIVE_RESPONSE,
            ACTION_IDLE_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()

    first = backend.aspirate(50.0, idempotency_key="pipette-idem-aspirate-50")
    second = backend.aspirate(50.0, idempotency_key="pipette-idem-aspirate-50")

    assert second == first
    assert fake_bus.serial.writes.count(VOLUME_50_WRITE_REQUEST) == 1
    assert fake_bus.serial.writes.count(ASPIRATE_WRITE_REQUEST) == 1


def test_all_actions_dry_run_validate_without_bus_io() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    results = [
        backend.home(idempotency_key="pipette-dry-home", dry_run=True),
        backend.aspirate(
            50.0,
            idempotency_key="pipette-dry-aspirate",
            dry_run=True,
        ),
        backend.dispense(
            50.0,
            idempotency_key="pipette-dry-dispense",
            dry_run=True,
        ),
        backend.eject_tip(
            idempotency_key="pipette-dry-eject",
            dry_run=True,
        ),
    ]

    assert all(result.success and result.dry_run for result in results)
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


def test_connect_atomically_decodes_signed_32_bit_position() -> None:
    fake_bus = FakeBus(
        [SNAPSHOT_READY_RESPONSE, POSITION_NEGATIVE_ONE_RESPONSE, TIP_PRESENT_RESPONSE]
    )
    backend = _backend(fake_bus)

    backend.connect()

    assert backend.status().position_steps == -1
    assert fake_bus.serial.writes == [
        STATUS_READ_REQUEST,
        POSITION_READ_REQUEST,
        TIP_READ_REQUEST,
    ]


def test_crc_error_is_l3_connection_error_and_not_swallowed() -> None:
    bad_crc = SNAPSHOT_READY_RESPONSE[:-1] + b"\x00"
    fake_bus = FakeBus([bad_crc])
    backend = _backend(fake_bus)

    with pytest.raises(PipetteCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3ConnectionError)
    assert exc_info.value.error_code == "L3.PIPETTE_COMMUNICATION"
    assert "CRC" in exc_info.value.human_message
    assert backend.status().connected is False


def test_serial_timeout_is_l3_connection_error_and_not_returned_as_false() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    with pytest.raises(PipetteCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3ConnectionError)
    assert exc_info.value.error_code == "L3.PIPETTE_COMMUNICATION"
    assert "响应超时" in exc_info.value.human_message
    assert backend.status().connected is False


def test_connect_is_idempotent_and_status_is_cached_without_bus_io() -> None:
    fake_bus = FakeBus(_ready_snapshot_responses())
    backend = _backend(fake_bus)

    backend.connect()
    backend.connect()
    transactions_after_connect = list(fake_bus.transactions)
    first = backend.status()
    second = backend.status()

    assert fake_bus.connect_count == 1
    assert fake_bus.transactions == transactions_after_connect
    assert first.connected is True
    assert first.homed is True
    assert first.tip_present is True
    assert first.timestamp is not None
    assert first.last_update_ms_ago is not None
    assert second.last_update_ms_ago is not None
    assert second.last_update_ms_ago >= first.last_update_ms_ago


def test_stop_writes_imm_stop_single_frame_and_resets_homed() -> None:
    """急停级 stop：单帧 IMM_STOP、homed 复位（W2）。"""
    fake_bus = FakeBus(
        [
            SNAPSHOT_UNHOMED_RESPONSE,
            POSITION_ZERO_RESPONSE,
            TIP_PRESENT_RESPONSE,
            VELOCITY_50_WRITE_REQUEST,
            ACCEL_1250_WRITE_REQUEST,
            DECEL_1250_WRITE_REQUEST,
            IDLE_WRITE_REQUEST,
            HOME_WRITE_REQUEST,
            HOME_ACTIVE_RESPONSE,
            HOME_DONE_RESPONSE,
            IMM_STOP_WRITE_REQUEST,
        ]
    )
    backend = _backend(fake_bus)
    backend.connect()
    backend.home(idempotency_key="pipette-home-before-estop-stop")
    assert backend.status().homed is True
    writes_before = len(fake_bus.serial.writes)

    result = backend.stop()

    assert fake_bus.serial.writes[writes_before:] == [IMM_STOP_WRITE_REQUEST]
    assert result.action == "stop"
    assert result.success is True
    assert backend.status().homed is False

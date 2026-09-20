"""LinearStageBackend protocol/safety tests; all I/O uses in-memory fakes."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast

import pytest

import src.hardware.linearstage_backend as linearstage_module
from src.hardware.errors import ConnectionError as L3ConnectionError
from src.hardware.errors import L3Error, MachineNotHomedError
from src.hardware.linearstage_backend import (
    LinearStageActionTimeoutError,
    LinearStageBackend,
    LinearStageCommunicationError,
    LinearStageConfig,
    LinearStageFaultError,
    LinearStageHomingError,
    LinearStageNotHomedError,
    LinearStagePositionOutOfRangeError,
)
from src.hardware.rs485_bus import Rs485Bus


# Byte sources required by W1.4, copied independently from
# docs/references/bro-linear-stage-20260716/linear_stage.py:
# - lines 108-150: address/function/payload/0x6B framing;
# - lines 174-184: position, flags, and home-status queries;
# - lines 200-240: immediate stop and sensorless-home configuration;
# - lines 242-267: enable, trigger, and home-status cycle;
# - lines 269-302: direction/speed/acceleration/pulse 0xFD payload.
FLAGS_QUERY = bytes.fromhex("04 3A 6B")
POSITION_QUERY = bytes.fromhex("04 36 6B")
HOME_STATUS_QUERY = bytes.fromhex("04 3B 6B")
HOME_CONFIG_FRAME = bytes.fromhex(
    "04 4C AE 00 02 01 01 2C 00 00 27 10 01 2C 03 20 00 3C 00 6B"
)
ENABLE_FRAME = bytes.fromhex("04 F3 AB 01 00 6B")
HOME_TRIGGER_FRAME = bytes.fromhex("04 9A 02 00 6B")
RESET_PROTECTION_FRAME = bytes.fromhex("04 0E 52 6B")
MOVE_TO_10_FROM_ZERO_FRAME = bytes.fromhex(
    "04 FD 00 07 D0 96 00 00 3E 80 00 00 6B"
)
MOVE_BACK_10_TO_ZERO_FRAME = bytes.fromhex(
    "04 FD 01 07 D0 96 00 00 3E 80 00 00 6B"
)
MOVE_FORWARD_40_FRAME = bytes.fromhex(
    "04 FD 00 07 D0 96 00 00 FA 00 00 00 6B"
)
MOVE_FORWARD_80_FRAME = bytes.fromhex(
    "04 FD 00 07 D0 96 00 01 F4 00 00 00 6B"
)
STOP_FRAME = bytes.fromhex("04 FE 98 00 6B")

FLAGS_DISABLED_IN_POSITION_RESPONSE = bytes.fromhex("04 3A 02 6B")
FLAGS_READY_RESPONSE = bytes.fromhex("04 3A 03 6B")
FLAGS_STALLED_RESPONSE = bytes.fromhex("04 3A 07 6B")
POSITION_ZERO_RESPONSE = bytes.fromhex("04 36 00 00 00 00 00 6B")
POSITION_TEN_RESPONSE = bytes.fromhex("04 36 00 00 05 00 00 6B")
POSITION_TEN_POINT_625_RESPONSE = bytes.fromhex("04 36 00 00 05 50 00 6B")
POSITION_EIGHT_POINT_FIVE_RESPONSE = bytes.fromhex("04 36 00 00 04 40 00 6B")
POSITION_FORTY_RESPONSE = bytes.fromhex("04 36 00 00 14 00 00 6B")
POSITION_EIGHTY_RESPONSE = bytes.fromhex("04 36 00 00 28 00 00 6B")
HOME_CONFIG_ACK = bytes.fromhex("04 4C 02 6B")
MOVE_ACK = bytes.fromhex("04 FD 02 6B")
HOME_ACTIVE_RESPONSE = bytes.fromhex("04 3B 04 6B")
HOME_DONE_RESPONSE = bytes.fromhex("04 3B 00 6B")
HOME_FAILED_RESPONSE = bytes.fromhex("04 3B 08 6B")


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

    def reset_input_buffer(self) -> None:
        return None

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        self._last_write = data
        return len(data)

    def flush(self) -> None:
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
        self.writes_per_transaction: list[int] = []
        self.active_transactions = 0

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
        assert self.active_transactions == 0
        self.transactions.append((device, baudrate, timeout_s))
        writes_before = len(self.serial.writes)
        self.active_transactions += 1
        try:
            yield self.serial
        finally:
            self.active_transactions -= 1
            self.writes_per_transaction.append(
                len(self.serial.writes) - writes_before
            )


def _backend(
    fake_bus: FakeBus,
    *,
    travel_mm: float = 100.0,
    home_timeout_s: float = 0.05,
    move_timeout_s: float = 0.05,
) -> LinearStageBackend:
    return LinearStageBackend(
        cast(Rs485Bus, fake_bus),
        address=4,
        config=LinearStageConfig(
            travel_mm=travel_mm,
            home_timeout_s=home_timeout_s,
            move_timeout_s=move_timeout_s,
            poll_interval_s=0.0005,
        ),
    )


def _connect_responses() -> list[bytes]:
    return [FLAGS_DISABLED_IN_POSITION_RESPONSE, POSITION_ZERO_RESPONSE]


def _home_responses() -> list[bytes]:
    return [
        HOME_CONFIG_ACK,
        FLAGS_READY_RESPONSE,
        HOME_ACTIVE_RESPONSE,
        HOME_DONE_RESPONSE,
        POSITION_ZERO_RESPONSE,  # 归零后闭环假设校验读 0x36（审查 P2-2）
    ]


def _connect_and_home(
    backend: LinearStageBackend,
    *,
    key: str,
) -> None:
    backend.connect()
    result = backend.home(idempotency_key=key)
    assert result.success is True
    assert backend.status().homed is True


def test_connect_reads_reference_flags_and_position_and_status_is_cached() -> None:
    fake_bus = FakeBus(_connect_responses())
    backend = _backend(fake_bus)

    backend.connect()
    backend.connect()
    transactions_after_connect = list(fake_bus.transactions)
    first = backend.status()
    second = backend.status()
    backend.close()

    assert fake_bus.serial.writes == [FLAGS_QUERY, POSITION_QUERY]
    assert fake_bus.connect_count == 1
    assert fake_bus.close_count == 0
    assert fake_bus.transactions == transactions_after_connect
    assert first.connected is True
    assert first.homed is False
    assert first.position_mm == 0.0
    assert first.flags_raw == 0x02
    assert first.enabled is False
    assert first.in_position is True
    assert second.last_update_ms_ago is not None
    assert first.last_update_ms_ago is not None
    assert second.last_update_ms_ago >= first.last_update_ms_ago
    assert backend.status().connected is False


def test_home_frames_match_reference_and_poll_sleeps_release_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bus = FakeBus(_connect_responses() + _home_responses())
    backend = _backend(fake_bus)
    sleep_observations: list[int] = []

    def assert_bus_released(_seconds: float) -> None:
        sleep_observations.append(fake_bus.active_transactions)

    monkeypatch.setattr(linearstage_module.time, "sleep", assert_bus_released)
    _connect_and_home(backend, key="linear-home-reference-frames")

    assert fake_bus.serial.writes == [
        FLAGS_QUERY,
        POSITION_QUERY,
        RESET_PROTECTION_FRAME,
        HOME_CONFIG_FRAME,
        ENABLE_FRAME,
        FLAGS_QUERY,
        HOME_TRIGGER_FRAME,
        HOME_STATUS_QUERY,
        HOME_STATUS_QUERY,
        POSITION_QUERY,
    ]
    assert sleep_observations
    assert sleep_observations == [0] * len(sleep_observations)
    assert fake_bus.writes_per_transaction == [1] * len(fake_bus.transactions)
    status = backend.status()
    assert status.homed is True
    assert status.position_mm == 0.0
    assert status.home_status_raw == 0


def test_move_frame_waits_for_position_and_flags_in_separate_transactions() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_TEN_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_TEN_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_TEN_RESPONSE,
            FLAGS_READY_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-move-frame")
    writes_before_move = len(fake_bus.serial.writes)

    result = backend.move_to(10.0, idempotency_key="linear-move-reference-frame")

    assert fake_bus.serial.writes[writes_before_move:] == [
        POSITION_QUERY,
        ENABLE_FRAME,
        FLAGS_QUERY,
        MOVE_TO_10_FROM_ZERO_FRAME,
        POSITION_QUERY,
        FLAGS_QUERY,
        POSITION_QUERY,
        FLAGS_QUERY,
        POSITION_QUERY,
        FLAGS_QUERY,
    ]
    assert fake_bus.writes_per_transaction == [1] * len(fake_bus.transactions)
    assert result.action == "move_to"
    assert result.target_position_mm == 10.0
    assert result.final_position_mm == 10.0
    assert result.success is True
    assert backend.status().position_mm == 10.0


def test_move_accepts_observed_point_625_mm_offset_with_point_seven_tolerance() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_TEN_POINT_625_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_TEN_POINT_625_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_TEN_POINT_625_RESPONSE,
            FLAGS_READY_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-tolerance-boundary")

    result = backend.move_to(10.0, idempotency_key="linear-move-tolerance-boundary")

    assert result.success is True
    assert result.target_position_mm == 10.0
    assert result.final_position_mm == 10.625
    assert backend.status().moving is False


def test_move_supports_explicit_contact_tolerance() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-contact-move")

    result = backend.move_to(
        10.0,
        idempotency_key="linear-contact-move",
        position_tolerance_mm=2.0,
    )

    assert result.success is True
    assert result.target_position_mm == 10.0
    assert result.final_position_mm == 8.5


def test_native_backend_sends_one_direct_move_over_50_mm() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_EIGHTY_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_EIGHTY_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_EIGHTY_RESPONSE,
            FLAGS_READY_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-native-direct-move")

    result = backend.move_to(80.0, idempotency_key="linear-native-direct-0-to-80")

    assert fake_bus.serial.writes.count(MOVE_FORWARD_80_FRAME) == 1
    assert MOVE_FORWARD_40_FRAME not in fake_bus.serial.writes
    assert result.target_position_mm == 80.0
    assert result.final_position_mm == 80.0
    assert result.success is True


def test_next_absolute_move_uses_legacy_commanded_origin_not_encoder_undershoot() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            # Command 0 -> 10, but observe the known 1.5 mm undershoot.
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
            # The return must still command nominal 10 -> 0, as the legacy
            # relative Emm driver did, rather than observed 8.5 -> 0.
            POSITION_EIGHT_POINT_FIVE_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-commanded-origin")

    backend.move_to(
        10.0,
        idempotency_key="linear-commanded-origin-out",
        position_tolerance_mm=2.0,
    )
    backend.move_to(0.0, idempotency_key="linear-commanded-origin-return")

    assert MOVE_BACK_10_TO_ZERO_FRAME in fake_bus.serial.writes


def test_stop_frame_matches_reference_and_has_no_idempotency_cache() -> None:
    fake_bus = FakeBus(_connect_responses())
    backend = _backend(fake_bus)
    backend.connect()

    first = backend.stop()
    second = backend.stop()

    assert fake_bus.serial.writes[-2:] == [STOP_FRAME, STOP_FRAME]
    assert first.action == second.action == "stop"
    assert first.success and second.success


def test_move_before_home_is_machine_not_homed_error_with_zero_motion_bytes() -> None:
    fake_bus = FakeBus(_connect_responses())
    backend = _backend(fake_bus)
    backend.connect()
    writes_after_connect = list(fake_bus.serial.writes)

    with pytest.raises(LinearStageNotHomedError) as exc_info:
        backend.move_to(10.0, idempotency_key="linear-unhomed-move")

    assert isinstance(exc_info.value, MachineNotHomedError)
    assert isinstance(exc_info.value, L3Error)
    assert exc_info.value.error_code == "L3.LINEAR_STAGE_NOT_HOMED"
    assert "home" in exc_info.value.suggested_action
    assert fake_bus.serial.writes == writes_after_connect


def test_home_failure_resets_previously_true_homed_flag_and_next_move_is_rejected() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [HOME_CONFIG_ACK, FLAGS_READY_RESPONSE, HOME_FAILED_RESPONSE]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-first-home-succeeds")

    with pytest.raises(LinearStageHomingError) as exc_info:
        backend.home(idempotency_key="linear-second-home-fails")

    assert exc_info.value.error_code == "L3.LINEAR_STAGE_HOMING_FAILED"
    assert backend.status().homed is False
    writes_after_failed_home = list(fake_bus.serial.writes)
    with pytest.raises(LinearStageNotHomedError):
        backend.move_to(10.0, idempotency_key="linear-move-after-home-failure")
    assert fake_bus.serial.writes == writes_after_failed_home


def test_home_timeout_sends_stop_last_and_keeps_homed_false() -> None:
    def response_factory(request: bytes, _size: int) -> bytes:
        assert request == HOME_STATUS_QUERY
        return HOME_DONE_RESPONSE

    fake_bus = FakeBus(
        _connect_responses() + [HOME_CONFIG_ACK, FLAGS_READY_RESPONSE],
        response_factory=response_factory,
    )
    backend = _backend(fake_bus, home_timeout_s=0.003)
    backend.connect()

    with pytest.raises(LinearStageActionTimeoutError) as exc_info:
        backend.home(idempotency_key="linear-home-timeout-stop")

    assert exc_info.value.action == "home"
    assert fake_bus.serial.writes[-1] == STOP_FRAME
    assert backend.status().homed is False


def test_move_timeout_sends_stop_as_last_frame_and_raises_structured_error() -> None:
    def response_factory(request: bytes, _size: int) -> bytes:
        if request == POSITION_QUERY:
            return POSITION_ZERO_RESPONSE
        assert request == FLAGS_QUERY
        return FLAGS_READY_RESPONSE

    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [POSITION_ZERO_RESPONSE, FLAGS_READY_RESPONSE, MOVE_ACK],
        response_factory=response_factory,
    )
    backend = _backend(fake_bus, move_timeout_s=0.003)
    _connect_and_home(backend, key="linear-home-before-move-timeout")

    with pytest.raises(LinearStageActionTimeoutError) as exc_info:
        backend.move_to(10.0, idempotency_key="linear-move-timeout-stop")

    assert exc_info.value.error_code == "L3.LINEAR_STAGE_ACTION_TIMEOUT"
    assert exc_info.value.action == "move_to"
    assert fake_bus.serial.writes[-1] == STOP_FRAME
    assert backend.status().moving is False


def test_move_stall_flag_sends_stop_last_and_raises_decoded_fault() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_ZERO_RESPONSE,
            FLAGS_STALLED_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-stall")

    with pytest.raises(LinearStageFaultError) as exc_info:
        backend.move_to(10.0, idempotency_key="linear-move-stall-stop")

    error = exc_info.value
    assert error.error_code == "L3.LINEAR_STAGE_FAULT"
    assert error.flags_raw == 0x07
    assert "motor_stalled" in error.fault_reasons
    assert fake_bus.serial.writes[-1] == STOP_FRAME


@pytest.mark.parametrize("position_mm", [-0.001, 100.001, float("nan"), float("inf")])
def test_out_of_travel_is_rejected_with_zero_bus_bytes(position_mm: float) -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    with pytest.raises(LinearStagePositionOutOfRangeError) as exc_info:
        backend.move_to(position_mm, idempotency_key=f"linear-range-{position_mm!r}")

    assert exc_info.value.error_code == "L3.LINEAR_STAGE_POSITION_OUT_OF_RANGE"
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


def test_same_idempotency_key_sends_move_command_only_once() -> None:
    fake_bus = FakeBus(
        _connect_responses()
        + _home_responses()
        + [
            POSITION_ZERO_RESPONSE,
            FLAGS_READY_RESPONSE,
            MOVE_ACK,
            POSITION_TEN_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_TEN_RESPONSE,
            FLAGS_READY_RESPONSE,
            POSITION_TEN_RESPONSE,
            FLAGS_READY_RESPONSE,
        ]
    )
    backend = _backend(fake_bus)
    _connect_and_home(backend, key="linear-home-before-idempotent-move")

    first = backend.move_to(10.0, idempotency_key="linear-idempotent-move-10")
    second = backend.move_to(10.0, idempotency_key="linear-idempotent-move-10")

    assert second == first
    assert fake_bus.serial.writes.count(MOVE_TO_10_FROM_ZERO_FRAME) == 1


def test_home_and_move_dry_run_use_zero_transactions() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    home_result = backend.home(
        idempotency_key="linear-dry-home",
        dry_run=True,
    )
    move_result = backend.move_to(
        10.0,
        idempotency_key="linear-dry-move",
        dry_run=True,
    )

    assert home_result.success and home_result.dry_run
    assert move_result.success and move_result.dry_run
    assert move_result.target_position_mm == 10.0
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


@pytest.mark.parametrize(
    "responses",
    [
        [bytes.fromhex("04 3A 02 00")],
        [FLAGS_DISABLED_IN_POSITION_RESPONSE, bytes.fromhex("04 36 00")],
    ],
    ids=["bad-fixed-tail", "short-position-read"],
)
def test_bad_tail_or_short_read_is_l3_connection_error_and_not_swallowed(
    responses: list[bytes],
) -> None:
    fake_bus = FakeBus(responses)
    backend = _backend(fake_bus)

    with pytest.raises(LinearStageCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3ConnectionError)
    assert exc_info.value.error_code == "L3.LINEAR_STAGE_COMMUNICATION"
    assert backend.status().connected is False


def test_home_position_counter_not_zero_raises_and_keeps_not_homed() -> None:
    """归零报完成但 0x36 计数未回零 → 立即结构化失败，homed 保持 False（审查 P2-2）。"""
    responses = [
        HOME_CONFIG_ACK,
        FLAGS_READY_RESPONSE,
        HOME_ACTIVE_RESPONSE,
        HOME_DONE_RESPONSE,
        POSITION_TEN_RESPONSE,
    ]
    fake_bus = FakeBus(_connect_responses() + responses)
    backend = _backend(fake_bus)
    backend.connect()

    with pytest.raises(LinearStageHomingError) as exc_info:
        backend.home(idempotency_key="linear-home-counter-not-zero")

    assert exc_info.value.position_mm == 10.0
    assert backend.status().homed is False

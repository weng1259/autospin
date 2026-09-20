"""SpincoaterBackend protocol and safety tests; all I/O uses in-memory fakes."""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest

from src.hardware.errors import ConnectionError as L3ConnectionError
from src.hardware.errors import L3Error
from src.hardware.rs485_bus import Rs485Bus
from src.hardware.spincoater_backend import (
    SpincoaterBackend,
    SpincoaterCommunicationError,
    SpincoaterConfig,
    SpincoaterFaultError,
    SpincoaterRpmOutOfRangeError,
)


# Frame sources required by W1.2:
# - AutoSpinmotorSystem/hardware/spin_motor/driver_communication.py:141-159 and
#   164-180 define DBLS400 reads and low-byte-first register-value writes;
# - AutoSpinmotorSystem/hardware/spin_motor/motor_controller.py:95-119,192-226
#   define the 0x0409 CCW start word and start-then-speed sequence;
# - motor_controller.py:205-213 define 0x040C brake / 0x0408 coast stop;
# - motor_controller.py:215-226 set_speed() writes raw RPM to 0x8005 (no
#   conversion); speed_factor=2.5 (:228-239) only decodes 0x8018 readback.
FAULT_READ_REQUEST = bytes.fromhex("02 03 80 1B 00 01 DD FE")
FAULT_ZERO_RESPONSE = bytes.fromhex("02 03 02 00 00 FC 44")
RUN_STATE_05_RESPONSE = bytes.fromhex("02 03 02 00 05 3C 47")
FAULT_STALL_HALL_RESPONSE = bytes.fromhex("02 03 02 05 00 FF 14")
START_CCW_REQUEST = bytes.fromhex("02 06 80 00 09 04 A7 AA")
SPEED_100_RPM_REQUEST = bytes.fromhex("02 06 80 05 64 00 9A F8")
STOP_WITH_BRAKE_REQUEST = bytes.fromhex("02 06 80 00 0C 04 A4 FA")
STOP_WITHOUT_BRAKE_REQUEST = bytes.fromhex("02 06 80 00 08 04 A6 3A")


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


def _backend(
    fake_bus: FakeBus,
    *,
    max_rpm: float = 3000.0,
    acceleration_rpm_per_s: float = 500.0,
    deceleration_rpm_per_s: float = 500.0,
) -> SpincoaterBackend:
    return SpincoaterBackend(
        cast(Rs485Bus, fake_bus),
        unit_id=2,
        config=SpincoaterConfig(
            max_rpm=max_rpm,
            acceleration_rpm_per_s=acceleration_rpm_per_s,
            deceleration_rpm_per_s=deceleration_rpm_per_s,
        ),
    )


def test_start_frames_match_dbls400_reference_and_speed_factor() -> None:
    """100 RPM writes raw command 100 to 0x8005 with fixed CCW control word."""
    fake_bus = FakeBus(
        [FAULT_ZERO_RESPONSE, START_CCW_REQUEST, SPEED_100_RPM_REQUEST]
    )
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.start(100.0, idempotency_key="spin-frame-100")

    assert fake_bus.serial.writes == [
        FAULT_READ_REQUEST,
        START_CCW_REQUEST,
        SPEED_100_RPM_REQUEST,
    ]
    assert fake_bus.transactions == [
        ("spincoater", 9600, 0.5),
        ("spincoater", 9600, 0.5),
        ("spincoater", 9600, 0.5),
    ]
    assert result.success is True
    assert result.action == "start"
    assert result.target_rpm == 100.0
    assert result.command_value == 100
    assert result.direction == "ccw"
    assert result.brake_engaged is False
    assert result.dry_run is False
    assert "raw RPM" in result.action_description
    assert backend.status().running is True


def test_software_acceleration_ramps_speed_without_guessing_drive_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(FakeBus([]), acceleration_rpm_per_s=500.0)
    writes: list[tuple[int, int]] = []
    monkeypatch.setattr(backend, "_write_single_register", lambda register, value: writes.append((register, value)))
    monkeypatch.setattr("src.hardware.spincoater_backend.time.sleep", lambda _: None)
    with backend._state_lock:
        backend._connected = True

    result = backend.start(500.0, idempotency_key="spin-ramp-500")

    assert writes[0] == (0x8000, 0x0409)
    assert writes[1:] == [
        (0x8005, 100),
        (0x8005, 200),
        (0x8005, 300),
        (0x8005, 400),
        (0x8005, 500),
    ]
    assert result.target_rpm == 500.0


def test_acceleration_setting_is_exposed_in_status() -> None:
    backend = _backend(FakeBus([]))

    result = backend.set_acceleration(350.0)

    assert result.acceleration_rpm_per_s == 350.0
    assert backend.status().acceleration_rpm_per_s == 350.0


def test_deceleration_setting_is_exposed_in_status() -> None:
    backend = _backend(FakeBus([]))

    result = backend.set_deceleration(300.0)

    assert result.deceleration_rpm_per_s == 300.0
    assert backend.status().deceleration_rpm_per_s == 300.0


def test_normal_stop_ramps_to_zero_before_applying_brake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(FakeBus([]), deceleration_rpm_per_s=500.0)
    writes: list[tuple[int, int]] = []
    monkeypatch.setattr(
        backend,
        "_write_single_register",
        lambda register, value: writes.append((register, value)),
    )
    monkeypatch.setattr("src.hardware.spincoater_backend.time.sleep", lambda _: None)
    with backend._state_lock:
        backend._connected = True
        backend._running = True
        backend._target_rpm = 500.0

    result = backend.stop(use_brake=True, idempotency_key="ramped-stop")

    assert writes == [
        (0x8005, 400),
        (0x8005, 300),
        (0x8005, 200),
        (0x8005, 100),
        (0x8005, 0),
        (0x8000, 0x040C),
    ]
    assert result.brake_engaged is True


def test_emergency_stop_bypasses_deceleration_ramp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(FakeBus([]))
    writes: list[tuple[int, int]] = []
    monkeypatch.setattr(
        backend,
        "_write_single_register",
        lambda register, value: writes.append((register, value)),
    )
    with backend._state_lock:
        backend._connected = True
        backend._running = True
        backend._target_rpm = 3000.0

    backend.stop(use_brake=True, ramp_down=False)

    assert writes == [(0x8000, 0x040C)]


def test_immediate_stop_marks_active_speed_ramp_aborted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(FakeBus([]))
    monkeypatch.setattr(backend, "_write_single_register", lambda *_: None)
    with backend._state_lock:
        backend._connected = True
        backend._running = True
        backend._target_rpm = 1000.0

    backend.stop(use_brake=True, ramp_down=False)

    assert backend._ramp_abort.is_set()
    with pytest.raises(L3Error, match="急停中断"):
        backend._ramp_speed(1000.0, 0.0)


def test_rpm_above_hard_limit_is_structured_error_and_sends_no_bytes() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus, max_rpm=3000.0)

    with pytest.raises(SpincoaterRpmOutOfRangeError) as exc_info:
        backend.start(3000.1, idempotency_key="spin-limit-3000-1")

    error = exc_info.value
    assert isinstance(error, L3Error)
    assert error.error_code == "L3.SPINCOATER_RPM_OUT_OF_RANGE"
    assert error.human_message
    assert error.agent_message
    assert error.suggested_action_zh
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


@pytest.mark.parametrize(
    ("use_brake", "expected_frame", "expected_brake"),
    [
        (True, STOP_WITH_BRAKE_REQUEST, True),
        (False, STOP_WITHOUT_BRAKE_REQUEST, False),
    ],
)
def test_stop_brake_and_coast_paths_use_different_control_words(
    use_brake: bool,
    expected_frame: bytes,
    expected_brake: bool,
) -> None:
    fake_bus = FakeBus([FAULT_ZERO_RESPONSE, expected_frame])
    backend = _backend(fake_bus)
    backend.connect()

    result = backend.stop(
        use_brake=use_brake,
        idempotency_key=f"spin-stop-brake-{use_brake}",
    )

    assert fake_bus.serial.writes == [FAULT_READ_REQUEST, expected_frame]
    assert result.action == "stop"
    assert result.target_rpm == 0.0
    assert result.brake_engaged is expected_brake
    assert backend.status().brake_engaged is expected_brake


def test_same_idempotency_key_only_sends_start_once() -> None:
    fake_bus = FakeBus(
        [FAULT_ZERO_RESPONSE, START_CCW_REQUEST, SPEED_100_RPM_REQUEST]
    )
    backend = _backend(fake_bus)
    backend.connect()

    first = backend.start(100.0, idempotency_key="spin-idem-start-100")
    second = backend.start(100.0, idempotency_key="spin-idem-start-100")

    assert second == first
    assert fake_bus.serial.writes == [
        FAULT_READ_REQUEST,
        START_CCW_REQUEST,
        SPEED_100_RPM_REQUEST,
    ]


def test_start_and_stop_dry_run_validate_without_bus_io() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    start_result = backend.start(
        100.0,
        idempotency_key="spin-dry-start-100",
        dry_run=True,
    )
    stop_result = backend.stop(
        idempotency_key="spin-dry-stop",
        dry_run=True,
    )

    assert start_result.dry_run is True
    assert start_result.command_value == 100
    assert stop_result.dry_run is True
    assert stop_result.brake_engaged is True
    assert fake_bus.transactions == []
    assert fake_bus.serial.writes == []


def test_nonzero_fault_register_raises_decoded_structured_error() -> None:
    fake_bus = FakeBus([FAULT_STALL_HALL_RESPONSE])
    backend = _backend(fake_bus)

    with pytest.raises(SpincoaterFaultError) as exc_info:
        backend.connect()

    error = exc_info.value
    assert isinstance(error, L3Error)
    assert error.error_code == "L3.SPINCOATER_FAULT"
    assert error.fault_register == 0x0005
    assert any("bit0" in bit and "堵转" in bit for bit in error.fault_bits)
    assert any("bit2" in bit and "霍尔异常" in bit for bit in error.fault_bits)
    assert "0x0005" in error.agent_message
    assert error.suggested_action_zh
    cached = backend.status()
    assert cached.connected is True
    assert cached.fault_register == 0x0005
    assert cached.fault_bits == list(error.fault_bits)

    writes_after_fault_read = list(fake_bus.serial.writes)
    with pytest.raises(SpincoaterFaultError):
        backend.start(100.0, idempotency_key="spin-block-known-fault")
    assert fake_bus.serial.writes == writes_after_fault_read

    with pytest.raises(SpincoaterFaultError):
        backend.connect()
    assert fake_bus.connect_count == 1
    assert fake_bus.serial.writes == writes_after_fault_read


def test_high_byte_run_state_is_not_reported_as_fault() -> None:
    fake_bus = FakeBus([RUN_STATE_05_RESPONSE])
    backend = _backend(fake_bus)

    backend.connect()

    status = backend.status()
    assert status.connected is True
    assert status.fault_register == 0x0500
    assert status.fault_bits == []


def test_crc_error_is_l3_connection_error_and_not_swallowed() -> None:
    bad_crc = FAULT_ZERO_RESPONSE[:-1] + b"\x45"
    fake_bus = FakeBus([bad_crc])
    backend = _backend(fake_bus)

    with pytest.raises(SpincoaterCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3ConnectionError)
    assert exc_info.value.error_code == "L3.SPINCOATER_COMMUNICATION"
    assert "CRC" in exc_info.value.human_message
    assert backend.status().connected is False


def test_timeout_is_l3_connection_error_and_not_returned_as_false() -> None:
    fake_bus = FakeBus([])
    backend = _backend(fake_bus)

    with pytest.raises(SpincoaterCommunicationError) as exc_info:
        backend.connect()

    assert isinstance(exc_info.value, L3ConnectionError)
    assert exc_info.value.error_code == "L3.SPINCOATER_COMMUNICATION"
    assert "响应超时" in exc_info.value.human_message
    assert backend.status().connected is False


def test_connect_is_idempotent_and_status_is_cached_without_bus_io() -> None:
    fake_bus = FakeBus([FAULT_ZERO_RESPONSE])
    backend = _backend(fake_bus)

    backend.connect()
    backend.connect()
    transactions_after_connect = list(fake_bus.transactions)
    first = backend.status()
    second = backend.status()

    assert fake_bus.connect_count == 1
    assert fake_bus.transactions == transactions_after_connect
    assert first.connected is True
    assert first.fault_register == 0
    assert first.timestamp is not None
    assert first.last_update_ms_ago is not None
    assert second.last_update_ms_ago is not None
    assert second.last_update_ms_ago >= first.last_update_ms_ago


def test_public_docstrings_declare_no_angle_feedback_limit() -> None:
    class_doc = SpincoaterBackend.__doc__ or ""
    start_doc = SpincoaterBackend.start.__doc__ or ""
    stop_doc = SpincoaterBackend.stop.__doc__ or ""

    for doc in (class_doc, start_doc, stop_doc):
        assert "无角度反馈" in doc
        assert "不能定向停转" in doc


def test_run_state_high_byte_is_not_a_fault() -> None:
    """0x801B 高字节=运行状态：正常旋转期间非零不得判故障（2026-07-16 审查修正）。"""
    run_state_only = bytes.fromhex("02 03 02 00 01 3D 84")
    fake_bus = FakeBus([run_state_only])
    backend = _backend(fake_bus)
    backend.connect()
    status = backend.status()
    assert status.fault_register == 0x0100
    assert backend._ensure_no_known_fault() is None  # 不抛

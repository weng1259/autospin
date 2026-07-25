"""Rs485Bus tests; every serial operation uses an in-memory fake."""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import serial

from src.hardware import rs485_bus as bus_module
from src.hardware.errors import ConnectionError as L3ConnectionError
from src.hardware.rs485_bus import Rs485Bus, get_bus


class FakeSerial:
    """Small pyserial stand-in with observable lifecycle and baud changes."""

    def __init__(self) -> None:
        self.port: str | None = None
        self.is_open = False
        self.open_count = 0
        self.close_count = 0
        self.writes: list[bytes] = []
        self.baudrate_changes: list[int] = []
        self.open_error: serial.SerialException | None = None
        self.write_error: serial.SerialException | None = None
        self._baudrate = 9600
        self._writes_lock = threading.Lock()

    @property
    def baudrate(self) -> int:
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        self._baudrate = value
        self.baudrate_changes.append(value)

    def open(self) -> None:
        self.open_count += 1
        if self.open_error is not None:
            raise self.open_error
        self.is_open = True

    def close(self) -> None:
        self.close_count += 1
        self.is_open = False

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            error = self.write_error
            self.write_error = None
            raise error
        with self._writes_lock:
            self.writes.append(data)
        return len(data)


class FakeSerialFactory:
    def __init__(self, serial_port: FakeSerial) -> None:
        self.serial_port = serial_port
        self.call_count = 0

    def __call__(self) -> FakeSerial:
        self.call_count += 1
        return self.serial_port


@pytest.fixture(autouse=True)
def _clear_bus_registry() -> Iterator[None]:
    with bus_module._BUSES_LOCK:
        bus_module._BUSES.clear()
    yield
    with bus_module._BUSES_LOCK:
        bus_module._BUSES.clear()


@pytest.fixture
def fake_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeSerial, FakeSerialFactory]:
    serial_port = FakeSerial()
    factory = FakeSerialFactory(serial_port)
    monkeypatch.setattr(bus_module.serial, "Serial", factory)
    return serial_port, factory


def _connected_bus(port: Path) -> Rs485Bus:
    bus = Rs485Bus(str(port))
    bus.connect()
    return bus


def test_get_bus_realpath_aliases_share_one_instance(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    target = tmp_path / "physical-rs485-port"
    target.touch()
    alias_a = tmp_path / "adapter-a"
    alias_b = tmp_path / "adapter-b"
    try:
        alias_a.symlink_to(target)
        alias_b.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable in this environment: {exc}")

    bus_a = get_bus(str(alias_a))
    bus_b = get_bus(str(alias_b))

    assert bus_a is bus_b
    assert bus_a.port == os.path.realpath(alias_a)
    assert fake_serial[1].call_count == 0


def test_transactions_are_mutually_exclusive_between_threads(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    serial_port, _ = fake_serial
    bus = _connected_bus(tmp_path / "shared-port")
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def worker(label: str) -> None:
        try:
            start.wait()
            for index in range(50):
                marker = f"{label}:{index}"
                with bus.transaction(label, 9600) as connection:
                    connection.write(f"{marker}:begin".encode())
                    time.sleep(0.0001)
                    connection.write(f"{marker}:end".encode())
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("pipette",)),
        threading.Thread(target=worker, args=("heater",)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(serial_port.writes) == 200
    for offset in range(0, len(serial_port.writes), 2):
        begin = serial_port.writes[offset].decode()
        end = serial_port.writes[offset + 1].decode()
        assert begin.endswith(":begin")
        assert end == f"{begin.removesuffix(':begin')}:end"


def test_baudrate_changes_only_when_requested_value_changes(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    serial_port, _ = fake_serial
    bus = _connected_bus(tmp_path / "shared-port")

    transactions = [
        ("pipette", 9600),
        ("pipette", 9600),
        ("spinmotor", 19200),
        ("heater", 19200),
        ("pipette", 9600),
    ]
    for device, baudrate in transactions:
        with bus.transaction(device, baudrate):
            pass

    assert serial_port.baudrate_changes == [19200, 9600]


def test_connection_stays_open_across_one_hundred_transactions(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    serial_port, factory = fake_serial
    bus = _connected_bus(tmp_path / "shared-port")

    for _ in range(100):
        with bus.transaction("pipette", 115200) as connection:
            assert connection is serial_port
    bus.connect()

    assert factory.call_count == 1
    assert serial_port.open_count == 1
    assert serial_port.close_count == 0
    assert serial_port.is_open is True


def test_transaction_exception_releases_lock_immediately(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    bus = _connected_bus(tmp_path / "shared-port")

    with pytest.raises(RuntimeError, match="test failure"):
        with bus.transaction("pipette", 9600):
            raise RuntimeError("test failure")

    acquired = threading.Event()
    errors: list[BaseException] = []

    def take_next_transaction() -> None:
        try:
            with bus.transaction("heater", 19200):
                acquired.set()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=take_next_transaction, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert acquired.is_set()
    assert not thread.is_alive()
    assert errors == []


def test_open_failure_is_l3_connection_error(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    serial_port, _ = fake_serial
    serial_port.open_error = serial.SerialException("adapter busy")
    bus = Rs485Bus(str(tmp_path / "busy-port"))

    with pytest.raises(L3ConnectionError) as exc_info:
        bus.connect()

    assert "无法打开" in exc_info.value.human_message
    assert "adapter busy" in exc_info.value.agent_message


def test_mid_transaction_disconnect_is_l3_connection_error(
    tmp_path: Path,
    fake_serial: tuple[FakeSerial, FakeSerialFactory],
) -> None:
    serial_port, _ = fake_serial
    bus = _connected_bus(tmp_path / "shared-port")
    serial_port.write_error = serial.SerialException("adapter detached")

    with pytest.raises(L3ConnectionError) as exc_info:
        with bus.transaction("spinmotor", 19200) as connection:
            connection.write(b"command")

    assert "通信中断" in exc_info.value.human_message
    assert "adapter detached" in exc_info.value.agent_message
    assert serial_port.is_open is False


def test_transaction_applies_timeout_and_exclusive(
    fake_serial: tuple[FakeSerial, FakeSerialFactory], tmp_path: Path
) -> None:
    """审查补丁回归：exclusive 独占锁 + 事务级读写超时（默认 1.0s、可覆写）。"""
    serial_port, _ = fake_serial
    bus = _connected_bus(tmp_path / "bus")
    assert getattr(serial_port, "exclusive", None) is True
    with bus.transaction("heater", baudrate=9600) as port:
        assert port.timeout == 1.0
        assert port.write_timeout == 1.0
    with bus.transaction("heater", baudrate=9600, timeout_s=0.2) as port:
        assert port.timeout == 0.2
        assert port.write_timeout == 0.2


def test_guard_mode_serializes_without_opening_serial(
    fake_serial: tuple[FakeSerial, FakeSerialFactory], tmp_path: Path
) -> None:
    serial_port, factory = fake_serial
    bus = Rs485Bus(str(tmp_path / "shared-port"))

    with bus.guard("spincoater", baudrate=9600, timeout_s=2.0):
        assert factory.call_count == 0
        assert serial_port.open_count == 0

    assert factory.call_count == 0
    assert serial_port.open_count == 0


def test_guard_exception_releases_lock(
    fake_serial: tuple[FakeSerial, FakeSerialFactory], tmp_path: Path
) -> None:
    bus = Rs485Bus(str(tmp_path / "shared-port"))

    with pytest.raises(RuntimeError, match="legacy failure"):
        with bus.guard("spincoater", baudrate=9600):
            raise RuntimeError("legacy failure")

    acquired = threading.Event()

    def take_next_guard() -> None:
        with bus.guard("heater", baudrate=9600):
            acquired.set()

    thread = threading.Thread(target=take_next_guard, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert acquired.is_set()
    assert not thread.is_alive()

"""Shared, process-local manager for a half-duplex RS485 bus.

One physical bus owns one persistent pyserial instance and one transaction
lock.  Port aliases are canonicalised with :func:`os.path.realpath` so every
device sharing the same USB adapter also shares the same lock.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import serial

from .errors import ConnectionError as L3ConnectionError


class Rs485Bus:
    """One physical RS485 bus backed by one persistent pyserial instance.

    The lock is process-local and serialises threads only.  This class does
    not provide priority, pre-emption, asynchronous access, or a cross-process
    lock; every user of a physical adapter must live in the same process and
    obtain the bus through :func:`get_bus`.
    """

    def __init__(self, port: str) -> None:
        self.port = os.path.realpath(port)
        self._serial: serial.Serial | None = None
        self._transaction_lock = threading.Lock()

    @contextmanager
    def guard(
        self,
        device: str,
        baudrate: int,
        *,
        timeout_s: float = 1.0,
    ) -> Iterator[None]:
        """Serialize access for a legacy controller that owns its serial port.

        Phase 1 keeps verified AutoSpinmotorSystem drivers intact. Some of
        those drivers still open their own pyserial object, so this guard
        deliberately does not open or reconfigure :attr:`port`; it only holds
        the same process-local lock used by :meth:`transaction` and records the
        requested serial settings for tests/diagnostics.
        """
        del device, baudrate, timeout_s
        with self._transaction_lock:
            yield

    def connect(self) -> None:
        """Open the adapter once and keep it open; repeated calls are no-ops."""
        with self._transaction_lock:
            if self._serial is None:
                try:
                    self._serial = serial.Serial()
                    self._serial.port = self.port
                    # POSIX 内核级独占锁：防第二个进程双开同一 tty 往半双工
                    # 总线插包（师兄栈审计 2026-07-16 的真实事故形态）。
                    self._serial.exclusive = True
                except (serial.SerialException, OSError) as exc:
                    self._serial = None
                    raise L3ConnectionError(
                        human_message=f"无法初始化 RS485 串口 {self.port}",
                        agent_message=(
                            f"Failed to initialise the RS485 serial adapter at "
                            f"{self.port!r}: {exc!r}. Check the adapter and port."
                        ),
                    ) from exc

            serial_port = self._serial
            if serial_port.is_open:
                return

            try:
                serial_port.open()
            except (serial.SerialException, OSError) as exc:
                raise L3ConnectionError(
                    human_message=f"无法打开 RS485 串口 {self.port}",
                    agent_message=(
                        f"Failed to open the RS485 serial adapter at {self.port!r}: "
                        f"{exc!r}. Check that the port exists and is not held by "
                        "another process."
                    ),
                ) from exc

    def close(self) -> None:
        """Close the persistent adapter; repeated calls are safe."""
        with self._transaction_lock:
            serial_port = self._serial
            if serial_port is None or not serial_port.is_open:
                return
            try:
                serial_port.close()
            except (serial.SerialException, OSError) as exc:
                raise L3ConnectionError(
                    human_message=f"关闭 RS485 串口 {self.port} 失败",
                    agent_message=(
                        f"Failed to close the RS485 serial adapter at "
                        f"{self.port!r}: {exc!r}."
                    ),
                ) from exc

    @contextmanager
    def transaction(
        self,
        device: str,
        baudrate: int,
        *,
        timeout_s: float = 1.0,
    ) -> Iterator[serial.Serial]:
        """Hold the bus for exactly one command-and-response exchange.

        一次事务 = 一次“发令 + 收回应”。禁止在事务内做长轮询；轮询循环必须在
        每次迭代时单独打开一个 transaction，让总线上的其它设备获得执行机会。
        The adapter must already be opened with :meth:`connect`.

        ``timeout_s`` 是持锁期间单次读/写的硬上限：pyserial 默认 timeout=None
        会永久阻塞，一个不回话的设备就能挂死整条总线（锁在手里放不掉）。
        """
        with self._transaction_lock:
            serial_port = self._serial
            if serial_port is None or not serial_port.is_open:
                raise L3ConnectionError(
                    human_message=f"RS485 串口未连接，无法访问设备 {device}",
                    agent_message=(
                        f"RS485 device {device!r} cannot start a transaction on "
                        f"{self.port!r}: the bus is disconnected. Call connect() "
                        "before retrying."
                    ),
                )

            try:
                if serial_port.baudrate != baudrate:
                    serial_port.baudrate = baudrate
                if getattr(serial_port, "timeout", None) != timeout_s:
                    serial_port.timeout = timeout_s
                if getattr(serial_port, "write_timeout", None) != timeout_s:
                    serial_port.write_timeout = timeout_s
                yield serial_port
                if not serial_port.is_open:
                    raise serial.SerialException(
                        "serial adapter closed during the transaction"
                    )
            except (serial.SerialException, OSError) as exc:
                self._drop_connection_after_failure(serial_port)
                raise L3ConnectionError(
                    human_message=f"RS485 设备 {device} 通信中断",
                    agent_message=(
                        f"RS485 transaction for device {device!r} on "
                        f"{self.port!r} at {baudrate} baud failed: {exc!r}. "
                        "Reconnect the bus before retrying."
                    ),
                ) from exc

    @staticmethod
    def _drop_connection_after_failure(serial_port: serial.Serial) -> None:
        """Best-effort close after an I/O failure, without masking that failure."""
        try:
            serial_port.close()
        except (serial.SerialException, OSError):
            pass


_BUSES: dict[str, Rs485Bus] = {}
_BUSES_LOCK = threading.Lock()


def get_bus(port: str) -> Rs485Bus:
    """Return the process-local singleton for a canonical physical port."""
    canonical_port = os.path.realpath(port)
    with _BUSES_LOCK:
        bus = _BUSES.get(canonical_port)
        if bus is None:
            bus = Rs485Bus(canonical_port)
            _BUSES[canonical_port] = bus
        return bus

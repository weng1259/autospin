"""Owned, dynamically reconfigured half-duplex RS485 bus."""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import serial

from .errors import ConnectionError as L3ConnectionError
from .serial_resources import (
    ResourceHandle,
    SerialResourceManager,
    get_serial_resource_manager,
)


class Rs485Bus:
    """One owner and one transaction lease for one physical RS485 adapter."""

    def __init__(
        self,
        port: str,
        *,
        resource_manager: SerialResourceManager | None = None,
    ) -> None:
        self.port = os.path.realpath(port)
        self._resource_manager = (
            resource_manager or get_serial_resource_manager()
        )
        self._resource_handle: ResourceHandle = self._resource_manager.register(
            self.port, "Rs485Bus"
        )
        self._serial: serial.Serial | None = None
        self._clients: set[str] = set()

    @contextmanager
    def guard(
        self,
        device: str,
        baudrate: int,
        *,
        timeout_s: float = 1.0,
    ) -> Iterator[None]:
        """Compatibility lease for a legacy controller.

        Production registration no longer selects legacy adapters. This method
        remains for isolated compatibility tests and guarantees serialization,
        but it does not authorize a second persistent physical connection.
        """
        del baudrate, timeout_s
        with self._resource_manager.transaction(
            self._resource_handle, f"{device}.legacy_guard"
        ):
            yield

    def connect(self, client: str = "anonymous") -> None:
        with self._resource_manager.transaction(
            self._resource_handle, "connect"
        ):
            if self._serial is not None and self._serial.is_open:
                self._clients.add(client)
                return
            try:
                self._serial = self._resource_manager.open_serial(
                    self._resource_handle,
                    baudrate=9600,
                    timeout=1.0,
                    exclusive=True,
                )
                self._clients.add(client)
            except (serial.SerialException, OSError) as exc:
                self._serial = None
                self._resource_manager.mark_disconnected(
                    self._resource_handle, exc
                )
                raise L3ConnectionError(
                    human_message=f"无法打开 RS485 串口 {self.port}",
                    agent_message=(
                        f"Failed to open owned RS485 adapter {self.port!r}: "
                        f"{exc!r}."
                    ),
                ) from exc

    def close(self, client: str = "anonymous", *, force: bool = False) -> None:
        with self._resource_manager.transaction(
            self._resource_handle, "close"
        ):
            self._clients.discard(client)
            if self._clients and not force:
                return
            serial_port = self._serial
            self._serial = None
            if serial_port is None:
                self._resource_manager.mark_disconnected(self._resource_handle)
                return
            try:
                if serial_port.is_open:
                    serial_port.close()
                self._resource_manager.mark_disconnected(self._resource_handle)
            except (serial.SerialException, OSError) as exc:
                self._resource_manager.mark_disconnected(
                    self._resource_handle, exc
                )
                raise L3ConnectionError(
                    human_message=f"关闭 RS485 串口 {self.port} 失败",
                    agent_message=f"Failed to close {self.port!r}: {exc!r}.",
                ) from exc

    @contextmanager
    def transaction(
        self,
        device: str,
        baudrate: int,
        *,
        timeout_s: float = 1.0,
    ) -> Iterator[serial.Serial]:
        """Reconfigure under the lease, exchange, then leave the bus idle."""
        with self._resource_manager.transaction(
            self._resource_handle, f"{device}.transaction"
        ):
            serial_port = self._serial
            if serial_port is None or not serial_port.is_open:
                raise L3ConnectionError(
                    human_message=f"RS485 串口未连接，无法访问设备 {device}",
                    agent_message=(
                        f"RS485 device {device!r} cannot use {self.port!r}: "
                        "the owned bus is disconnected."
                    ),
                )
            try:
                # Different-baud devices share the adapter only while this
                # transaction lease is held.
                if serial_port.baudrate != baudrate:
                    serial_port.baudrate = baudrate
                serial_port.timeout = timeout_s
                serial_port.write_timeout = timeout_s
                yield serial_port
                if not serial_port.is_open:
                    raise serial.SerialException(
                        "serial adapter closed during transaction"
                    )
            except (serial.SerialException, OSError) as exc:
                try:
                    serial_port.close()
                except (serial.SerialException, OSError):
                    pass
                self._serial = None
                self._resource_manager.mark_disconnected(
                    self._resource_handle, exc
                )
                raise L3ConnectionError(
                    human_message=f"RS485 设备 {device} 通信中断",
                    agent_message=(
                        f"RS485 transaction for {device!r} on {self.port!r} "
                        f"at {baudrate} baud failed: {exc!r}; reconnect through "
                        "the same owner before retrying."
                    ),
                ) from exc

    def dispose(self) -> None:
        self.close(force=True)
        self._resource_handle.release()

    def diagnostics(self) -> list[dict[str, object]]:
        return self._resource_manager.diagnostics()


_BUSES: dict[tuple[int, str], Rs485Bus] = {}
_BUSES_LOCK = threading.Lock()


def get_bus(
    port: str,
    *,
    resource_manager: SerialResourceManager | None = None,
) -> Rs485Bus:
    manager = resource_manager or get_serial_resource_manager()
    canonical = os.path.realpath(port)
    key = (id(manager), canonical)
    with _BUSES_LOCK:
        bus = _BUSES.get(key)
        if bus is None or bus._resource_handle.released:
            bus = Rs485Bus(canonical, resource_manager=manager)
            _BUSES[key] = bus
        return bus

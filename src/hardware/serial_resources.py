"""Single-process ownership and transaction control for physical serial ports."""
from __future__ import annotations

import os
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterator

import serial


class ResourceOwnershipError(RuntimeError):
    pass


class ResourceState(StrEnum):
    CREATED = "created"
    REGISTERED = "registered"
    AVAILABLE = "available"
    CONNECTED = "connected"
    BUSY = "busy"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass(slots=True, weakref_slot=True)
class ResourceHandle:
    resource: str
    owner: str
    _manager_ref: weakref.ReferenceType["SerialResourceManager"]
    released: bool = False

    def release(self) -> None:
        manager = self._manager_ref()
        if manager is not None and not self.released:
            manager.release(self)


@dataclass(slots=True)
class _ResourceRecord:
    resource: str
    owner: str
    handle_ref: weakref.ReferenceType[ResourceHandle]
    state: ResourceState = ResourceState.REGISTERED
    transaction_lock: threading.Lock = field(default_factory=threading.Lock)
    current_operation: str | None = None
    last_operation: str | None = None
    last_error: str | None = None
    last_success_monotonic: float | None = None
    connected: bool = False


class SerialResourceManager:
    """Canonical-port owner registry plus per-resource transaction locks."""

    def __init__(
        self,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._serial_factory = serial_factory
        self._registry_lock = threading.RLock()
        self._records: dict[str, _ResourceRecord] = {}

    @staticmethod
    def canonical(resource: str) -> str:
        return os.path.realpath(resource)

    def register(self, resource: str, owner: str) -> ResourceHandle:
        canonical = self.canonical(resource)
        with self._registry_lock:
            existing = self._records.get(canonical)
            if existing is not None:
                live = existing.handle_ref()
                if live is not None and not live.released:
                    raise ResourceOwnershipError(
                        f"serial resource {canonical!r} already owned by "
                        f"{existing.owner!r}; rejected duplicate owner {owner!r}"
                    )
                self._records.pop(canonical, None)
            handle = ResourceHandle(canonical, owner, weakref.ref(self))
            self._records[canonical] = _ResourceRecord(
                resource=canonical,
                owner=owner,
                handle_ref=weakref.ref(handle),
                state=ResourceState.AVAILABLE,
            )
            return handle

    def release(self, handle: ResourceHandle) -> None:
        with self._registry_lock:
            record = self._require(handle)
            if record.current_operation is not None:
                raise ResourceOwnershipError(
                    f"cannot release busy resource {handle.resource!r}"
                )
            record.state = ResourceState.DISCONNECTED
            handle.released = True
            self._records.pop(handle.resource, None)

    @contextmanager
    def transaction(
        self,
        handle: ResourceHandle,
        operation: str,
    ) -> Iterator[None]:
        record = self._require(handle)
        record.transaction_lock.acquire()
        try:
            record.state = ResourceState.BUSY
            record.current_operation = operation
            record.last_operation = operation
            yield
        except Exception as exc:
            record.state = ResourceState.ERROR
            record.last_error = f"{type(exc).__name__}: {exc}"
            record.connected = False
            raise
        else:
            if record.state not in {
                ResourceState.DISCONNECTED,
                ResourceState.ERROR,
            }:
                record.state = (
                    ResourceState.CONNECTED
                    if record.connected
                    else ResourceState.AVAILABLE
                )
            if record.state is not ResourceState.ERROR:
                record.last_error = None
            record.last_success_monotonic = time.monotonic()
        finally:
            record.current_operation = None
            record.transaction_lock.release()

    def open_serial(
        self,
        handle: ResourceHandle,
        *,
        baudrate: int,
        timeout: float,
        **kwargs: Any,
    ) -> Any:
        record = self._require(handle)
        factory = self._serial_factory or serial.Serial
        try:
            serial_port = factory(
                handle.resource,
                baudrate,
                timeout=timeout,
                **kwargs,
            )
        except TypeError as exc:
            # Existing in-memory bus fakes model pyserial's deferred-open
            # construction. Keep that seam without weakening production
            # ownership or the exclusive open used by real pyserial.
            if "unexpected keyword" not in str(exc) and "positional" not in str(exc):
                raise
            serial_port = factory()
            serial_port.port = handle.resource
            if getattr(serial_port, "baudrate", None) != baudrate:
                serial_port.baudrate = baudrate
            serial_port.timeout = timeout
            serial_port.write_timeout = kwargs.get("write_timeout", timeout)
            serial_port.exclusive = kwargs.get("exclusive", False)
            serial_port.open()
        record.connected = True
        record.state = ResourceState.CONNECTED
        return serial_port

    def mark_disconnected(
        self,
        handle: ResourceHandle,
        error: Exception | None = None,
    ) -> None:
        record = self._require(handle)
        record.connected = False
        record.state = ResourceState.ERROR if error is not None else ResourceState.DISCONNECTED
        if error is not None:
            record.last_error = f"{type(error).__name__}: {error}"

    def diagnostics(self) -> list[dict[str, Any]]:
        with self._registry_lock:
            result: list[dict[str, Any]] = []
            for record in self._records.values():
                result.append(
                    {
                        "resource": record.resource,
                        "owner": record.owner,
                        "state": record.state.value,
                        "connected": record.connected,
                        "locked": record.current_operation is not None,
                        "current_operation": record.current_operation,
                        "last_operation": record.last_operation,
                        "last_error": record.last_error,
                        "last_success_monotonic": record.last_success_monotonic,
                    }
                )
            return sorted(result, key=lambda item: item["resource"])

    def _require(self, handle: ResourceHandle) -> _ResourceRecord:
        if handle.released:
            raise ResourceOwnershipError(
                f"serial resource handle for {handle.resource!r} was released"
            )
        with self._registry_lock:
            record = self._records.get(handle.resource)
            if record is None or record.handle_ref() is not handle:
                raise ResourceOwnershipError(
                    f"invalid owner handle for {handle.resource!r}"
                )
            return record


_GLOBAL_MANAGER = SerialResourceManager()


def get_serial_resource_manager() -> SerialResourceManager:
    return _GLOBAL_MANAGER

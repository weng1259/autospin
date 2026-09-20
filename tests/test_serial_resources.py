from __future__ import annotations

import threading
import time

import pytest

from src.hardware.serial_resources import (
    ResourceOwnershipError,
    SerialResourceManager,
)


class FakeSerial:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.is_open = True

    def close(self) -> None:
        self.is_open = False


def test_duplicate_physical_owner_is_rejected() -> None:
    manager = SerialResourceManager(FakeSerial)
    owner = manager.register("/dev/shared", "first")

    with pytest.raises(ResourceOwnershipError, match="already owned"):
        manager.register("/dev/shared", "second")

    owner.release()
    replacement = manager.register("/dev/shared", "second")
    assert replacement.owner == "second"


def test_transaction_lock_serializes_and_failure_releases() -> None:
    manager = SerialResourceManager(FakeSerial)
    handle = manager.register("/dev/shared", "bus")
    entered: list[str] = []

    def operation(name: str) -> None:
        with manager.transaction(handle, name):
            entered.append(f"{name}:start")
            time.sleep(0.02)
            entered.append(f"{name}:end")

    first = threading.Thread(target=operation, args=("a",))
    second = threading.Thread(target=operation, args=("b",))
    first.start()
    second.start()
    first.join()
    second.join()
    assert entered in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    )

    with pytest.raises(ValueError):
        with manager.transaction(handle, "failure"):
            raise ValueError("boom")
    with manager.transaction(handle, "recovery"):
        pass
    assert manager.diagnostics()[0]["last_operation"] == "recovery"


def test_reconnect_reuses_owner_handle() -> None:
    manager = SerialResourceManager(FakeSerial)
    handle = manager.register("/dev/reconnect", "relay")
    first = manager.open_serial(handle, baudrate=9600, timeout=1.0)
    first.close()
    manager.mark_disconnected(handle)
    second = manager.open_serial(handle, baudrate=9600, timeout=1.0)

    assert second is not first
    assert manager.diagnostics()[0]["owner"] == "relay"
    assert manager.diagnostics()[0]["state"] == "connected"

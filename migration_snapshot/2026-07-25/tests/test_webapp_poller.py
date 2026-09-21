"""W3.1 状态轮询、内存读路径与 SSE；全测试只使用假 backend。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import cast

from fastapi import Request
from fastapi.testclient import TestClient
import pytest

from src.hardware.heater_backend import HeaterBackend, HeaterStatus
from src.webapp import DeviceRegistry, create_app
from src.webapp.poller import StatusPoller, StatusSnapshot


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class _FlakyHeater:
    def __init__(self) -> None:
        self.allow_success = threading.Event()
        self.calls = 0

    def status(self) -> HeaterStatus:
        self.calls += 1
        if not self.allow_success.is_set():
            raise RuntimeError("fake heater status failed")
        return HeaterStatus(connected=True, pv_c=23.5, sv_c=40.0)


class _BlockingHeater:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self) -> HeaterStatus:
        self.entered.set()
        self.release.wait()
        return HeaterStatus(connected=True)


class _CountingHeater:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0

    def status(self) -> HeaterStatus:
        with self._lock:
            self._calls += 1
            pv_c = float(self._calls)
        return HeaterStatus(connected=True, pv_c=pv_c)


class _DisconnectableRequest:
    disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def _wait_for_snapshot(
    poller: StatusPoller,
    predicate: object,
    *,
    timeout_s: float = 1.0,
) -> StatusSnapshot:
    assert callable(predicate)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshot = poller.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.005)
    pytest.fail("status snapshot did not reach the expected state")


def _web_poller_threads() -> int:
    return sum(
        thread.name == "WebStatusPoller" for thread in threading.enumerate()
    )


def test_status_endpoint_reports_errors_and_poller_continues() -> None:
    heater = _FlakyHeater()
    registry = DeviceRegistry(
        heater=cast(HeaterBackend, heater),
        mock=True,
    )
    registry.poller = StatusPoller(registry, interval_s=0.01)
    app = create_app(registry, token=TOKEN)

    with TestClient(app) as client:
        failed = _wait_for_snapshot(
            registry.poller,
            lambda value: value["seq"] >= 2
            and "error" in value["devices"]["heater"],
        )
        response = client.get("/api/status", headers=AUTH_HEADERS)

        assert response.status_code == 200
        assert response.json() == failed
        assert failed["seq"] >= 2
        assert failed["ts"]
        assert "fake heater status failed" in cast(
            str, failed["devices"]["heater"]["error"]
        )

        heater.allow_success.set()
        recovered = _wait_for_snapshot(
            registry.poller,
            lambda value: value["seq"] > failed["seq"]
            and "error" not in value["devices"]["heater"],
        )

    assert recovered["seq"] > failed["seq"]
    assert recovered["devices"]["heater"]["connected"] is True
    assert heater.calls >= 3


def test_status_endpoint_does_not_wait_for_blocked_device() -> None:
    heater = _BlockingHeater()
    registry = DeviceRegistry(
        heater=cast(HeaterBackend, heater),
        mock=True,
    )
    registry.poller = StatusPoller(registry, interval_s=0.01)
    app = create_app(registry, token=TOKEN)

    with TestClient(app) as client:
        assert heater.entered.wait(timeout=1.0)
        started = time.monotonic()
        response = client.get("/api/status", headers=AUTH_HEADERS)
        elapsed_s = time.monotonic() - started
        heater.release.set()

    assert response.status_code == 200
    assert elapsed_s < 1.0
    assert response.json()["seq"] == 0


def test_sse_emits_increasing_seq_and_disconnects_without_thread_leak() -> None:
    before_threads = _web_poller_threads()
    heater = _CountingHeater()
    registry = DeviceRegistry(
        heater=cast(HeaterBackend, heater),
        mock=True,
    )
    poller = StatusPoller(registry, interval_s=0.01)
    request = _DisconnectableRequest()
    poller.start()

    async def consume_two_frames() -> tuple[dict[str, object], dict[str, object]]:
        stream = poller.stream(cast(Request, request))
        first_raw = await anext(stream)
        second_raw = await anext(stream)
        first = cast(
            dict[str, object],
            json.loads(first_raw.removeprefix("data: ").strip()),
        )
        second = cast(
            dict[str, object],
            json.loads(second_raw.removeprefix("data: ").strip()),
        )
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return first, second

    try:
        first, second = asyncio.run(consume_two_frames())
    finally:
        poller.stop()

    assert cast(int, second["seq"]) > cast(int, first["seq"])
    assert _web_poller_threads() == before_threads


def test_lifespan_starts_and_stops_poller() -> None:
    registry = DeviceRegistry.from_mocks()
    registry.poller = StatusPoller(registry, interval_s=0.01)
    app = create_app(registry, token=TOKEN)

    assert registry.poller.is_running() is False
    with TestClient(app):
        assert registry.poller.is_running() is True
    assert registry.poller.is_running() is False

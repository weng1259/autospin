"""W3.1 Web 急停与 shutdown 安全语义；全测试只使用假对象。"""
from __future__ import annotations

import threading
import time
from typing import cast

from fastapi.testclient import TestClient

from src.hardware.heater_backend import HeaterBackend, HeaterStatus
from src.system_estop import EstopReport, EstopStepReport, SystemEstop
from src.webapp import DeviceRegistry, create_app
from src.webapp.poller import StatusPoller


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class _FakeEstop:
    def __init__(self) -> None:
        self.calls = 0

    def halt_all(self) -> EstopReport:
        self.calls += 1
        return EstopReport(
            ok=True,
            steps=[
                EstopStepReport(
                    device="gantry",
                    action="abort_motion_immediate",
                    ok=True,
                )
            ],
            duration_ms=0.25,
        )


class _RaisingEstop:
    def __init__(self) -> None:
        self.calls = 0

    def halt_all(self) -> EstopReport:
        self.calls += 1
        raise RuntimeError("fake shutdown estop failure")


class _BlockingHeater:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self) -> HeaterStatus:
        self.entered.set()
        self.release.wait()
        return HeaterStatus(connected=True)


def test_estop_returns_immediately_while_poller_device_is_blocked() -> None:
    heater = _BlockingHeater()
    estop = _FakeEstop()
    registry = DeviceRegistry(
        heater=cast(HeaterBackend, heater),
        estop=cast(SystemEstop, estop),
        mock=True,
    )
    registry.poller = StatusPoller(registry, interval_s=0.01)
    app = create_app(registry, token=TOKEN)
    responses: list[object] = []
    errors: list[BaseException] = []

    with TestClient(app) as client:
        assert heater.entered.wait(timeout=1.0)

        def post_estop() -> None:
            try:
                responses.append(
                    client.post("/api/estop", headers=AUTH_HEADERS)
                )
            except BaseException as exc:
                errors.append(exc)

        request_thread = threading.Thread(target=post_estop)
        started = time.monotonic()
        request_thread.start()
        request_thread.join(timeout=0.9)
        elapsed_s = time.monotonic() - started
        finished_immediately = not request_thread.is_alive()
        heater.release.set()
        request_thread.join(timeout=1.0)

    assert finished_immediately is True
    assert elapsed_s < 1.0
    assert errors == []
    assert len(responses) == 1
    response = responses[0]
    assert hasattr(response, "status_code")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert estop.calls == 1


def test_estop_keeps_bearer_token_authentication() -> None:
    estop = _FakeEstop()
    registry = DeviceRegistry(
        estop=cast(SystemEstop, estop),
        mock=True,
    )
    app = create_app(registry, token=TOKEN)

    with TestClient(app) as client:
        response = client.post("/api/estop")

    assert response.status_code == 401
    assert estop.calls == 0


def test_estop_releases_running_operation_and_records_completion() -> None:
    estop = _FakeEstop()
    registry = DeviceRegistry(
        estop=cast(SystemEstop, estop),
        mock=True,
    )
    app = create_app(registry, token=TOKEN)
    release = threading.Event()

    with TestClient(app) as client:
        gate = app.state.operation_gate
        accepted = gate.submit(
            "spincoater",
            "start",
            lambda _: release.wait(timeout=1.0),
        )
        response = client.post("/api/estop", headers=AUTH_HEADERS)
        current = client.get("/api/operations/current", headers=AUTH_HEADERS)
        operation = client.get(
            f"/api/operations/{accepted.id}", headers=AUTH_HEADERS
        )
        status = client.get("/api/status", headers=AUTH_HEADERS)
        release.set()

    assert response.status_code == 200
    assert current.status_code == 200
    assert current.json() is None
    assert operation.json()["status"] == "failed"
    assert operation.json()["error"]["error_code"] == "L3.OPERATION_ABORTED_BY_ESTOP"
    assert status.json()["last_operation"]["id"] == accepted.id


def test_lifespan_shutdown_calls_halt_all_once_outside_mock_mode() -> None:
    estop = _FakeEstop()
    registry = DeviceRegistry(
        estop=cast(SystemEstop, estop),
        mock=False,
    )
    app = create_app(registry, token=TOKEN)

    with TestClient(app):
        assert estop.calls == 0

    assert estop.calls == 1


def test_lifespan_shutdown_skips_halt_all_in_mock_mode() -> None:
    estop = _FakeEstop()
    registry = DeviceRegistry(
        estop=cast(SystemEstop, estop),
        mock=True,
    )
    app = create_app(registry, token=TOKEN)

    with TestClient(app):
        pass

    assert estop.calls == 0


def test_lifespan_shutdown_estop_is_best_effort() -> None:
    estop = _RaisingEstop()
    registry = DeviceRegistry(
        estop=cast(SystemEstop, estop),
        mock=False,
    )
    app = create_app(registry, token=TOKEN)

    with TestClient(app):
        pass

    assert estop.calls == 1

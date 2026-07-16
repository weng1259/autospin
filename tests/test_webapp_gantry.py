"""W3.2 龙门 Web 端点；全测试只使用假 backend 与假急停。"""
from __future__ import annotations

import threading
import time
from typing import cast

from fastapi.testclient import TestClient
import pytest

from src.hardware.gantry_backend import GantryBackend
from src.hardware.types import MachineState, MachineStatus, Position
from src.system_estop import EstopReport, EstopStepReport, SystemEstop
from src.webapp import DeviceRegistry, create_app


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
MOVE_BODY = {"x": -10.0, "y": -20.0, "z": -5.0, "feed": 500.0}
JOG_BODY = {"axis": "X", "distance": 1.5, "feed": 100.0}


class _FakeGantry:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.block_move = False
        self.move_entered = threading.Event()
        self.release_move = threading.Event()

    def get_status(self) -> MachineStatus:
        return MachineStatus(
            state=MachineState.IDLE,
            position=Position(x_mm=0.0, y_mm=0.0, z_mm=0.0),
            is_homed=True,
        )

    def connect(self) -> None:
        self.calls.append(("connect",))

    def close(self) -> None:
        self.calls.append(("disconnect",))

    def home(self, *, idempotency_key: str) -> object:
        self.calls.append(("home", idempotency_key))
        return {"homed": True}

    def move_to(
        self,
        target: Position,
        *,
        feed_mm_min: float,
    ) -> object:
        self.calls.append(("move", target, feed_mm_min))
        self.move_entered.set()
        if self.block_move:
            self.release_move.wait(timeout=2.0)
        return target

    def jog(
        self,
        axis: str,
        distance_mm: float,
        feed_mm_min: float,
    ) -> object:
        self.calls.append(("jog", axis, distance_mm, feed_mm_min))
        return Position(x_mm=distance_mm, y_mm=0.0, z_mm=0.0)

    def recover_from_alarm(self, *, idempotency_key: str) -> object:
        self.calls.append(("recover", idempotency_key))
        return {"recovered": True}

    def validate_grbl_settings(self) -> object:
        self.calls.append(("grbl-settings",))
        return {"success": True}


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
            duration_ms=0.1,
        )


def _registry(
    gantry: _FakeGantry,
    *,
    estop: _FakeEstop | None = None,
) -> DeviceRegistry:
    return DeviceRegistry(
        gantry=cast(GantryBackend, gantry),
        estop=(
            SystemEstop() if estop is None else cast(SystemEstop, estop)
        ),
        mock=True,
    )


def _wait_for_operation(
    client: TestClient,
    operation_id: str,
    *,
    timeout_s: float = 1.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/operations/{operation_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        record = cast(dict[str, object], response.json())
        if record["status"] != "running":
            return record
        time.sleep(0.001)
    pytest.fail(f"operation {operation_id} did not complete")


def _assert_accepted(response: object) -> str:
    assert hasattr(response, "status_code")
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert isinstance(body["operation_id"], str)
    return cast(str, body["operation_id"])


def test_all_gantry_endpoints_return_202_and_forward_typed_arguments() -> None:
    gantry = _FakeGantry()
    app = create_app(_registry(gantry), token=TOKEN)

    with TestClient(app) as client:
        cases = [
            ("post", "/api/gantry/connect", None),
            ("post", "/api/gantry/disconnect", None),
            ("post", "/api/gantry/home", {"idempotency_key": "home-key"}),
            ("post", "/api/gantry/move", MOVE_BODY),
            ("post", "/api/gantry/jog", JOG_BODY),
            (
                "post",
                "/api/gantry/recover",
                {"idempotency_key": "recover-key"},
            ),
            ("get", "/api/gantry/grbl-settings", None),
        ]

        for method, path, body in cases:
            response = (
                client.get(path, headers=AUTH_HEADERS)
                if method == "get"
                else client.post(path, headers=AUTH_HEADERS, json=body)
            )
            operation_id = _assert_accepted(response)
            record = _wait_for_operation(client, operation_id)
            assert record["status"] == "succeeded"

    assert gantry.calls[0] == ("connect",)
    assert gantry.calls[1] == ("disconnect",)
    assert gantry.calls[2] == ("home", "home-key")
    assert gantry.calls[3][0] == "move"
    assert gantry.calls[3][1] == Position(
        x_mm=-10.0,
        y_mm=-20.0,
        z_mm=-5.0,
    )
    assert gantry.calls[3][2] == 500.0
    assert gantry.calls[4] == ("jog", "X", 1.5, 100.0)
    assert gantry.calls[5] == ("recover", "recover-key")
    assert gantry.calls[6] == ("grbl-settings",)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/gantry/connect", None),
        ("post", "/api/gantry/disconnect", None),
        ("post", "/api/gantry/home", None),
        ("post", "/api/gantry/move", MOVE_BODY),
        ("post", "/api/gantry/jog", JOG_BODY),
        ("post", "/api/gantry/recover", None),
        ("get", "/api/gantry/grbl-settings", None),
    ],
)
def test_every_hardware_touching_gantry_endpoint_rejects_when_gate_is_busy(
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    gantry = _FakeGantry()
    gantry.block_move = True
    app = create_app(_registry(gantry), token=TOKEN)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/gantry/move",
            headers=AUTH_HEADERS,
            json=MOVE_BODY,
        )
        blocker_id = _assert_accepted(accepted)
        assert gantry.move_entered.wait(timeout=1.0)

        if method == "get":
            response = client.get(path, headers=AUTH_HEADERS)
        else:
            response = client.post(path, headers=AUTH_HEADERS, json=body)

        assert response.status_code == 409
        payload = response.json()
        assert payload["error"]["error_code"] == "L3.OPERATION_CONFLICT"
        current = payload["current_operation"]
        assert current["id"] == blocker_id
        assert current["device"] == "gantry"
        assert current["action"] == "move"
        assert current["started_at"]
        assert current["elapsed"] >= 0.0

        current_response = client.get(
            "/api/operations/current",
            headers=AUTH_HEADERS,
        )
        assert current_response.status_code == 200
        assert current_response.json()["id"] == blocker_id

        gantry.release_move.set()
        _wait_for_operation(client, blocker_id)

    assert [call[0] for call in gantry.calls] == ["move"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/gantry/move", {"x": 0, "y": 0, "feed": 100}),
        ("/api/gantry/move", {**MOVE_BODY, "x": "not-a-number"}),
        ("/api/gantry/jog", {"distance": 1, "feed": 100}),
        ("/api/gantry/jog", {**JOG_BODY, "distance": [1]}),
    ],
)
def test_invalid_move_or_jog_body_returns_422_without_backend_call(
    path: str,
    body: dict[str, object],
) -> None:
    gantry = _FakeGantry()
    app = create_app(_registry(gantry), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(path, headers=AUTH_HEADERS, json=body)

    assert response.status_code == 422
    assert gantry.calls == []


def test_estop_bypasses_gate_while_move_thread_is_blocked() -> None:
    gantry = _FakeGantry()
    gantry.block_move = True
    estop = _FakeEstop()
    app = create_app(_registry(gantry, estop=estop), token=TOKEN)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/gantry/move",
            headers=AUTH_HEADERS,
            json=MOVE_BODY,
        )
        blocker_id = _assert_accepted(accepted)
        assert gantry.move_entered.wait(timeout=1.0)

        started = time.monotonic()
        response = client.post("/api/estop", headers=AUTH_HEADERS)
        elapsed_s = time.monotonic() - started

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert elapsed_s < 0.5
        assert estop.calls == 1

        gantry.release_move.set()
        _wait_for_operation(client, blocker_id)


def test_completed_operation_is_visible_in_status_snapshot() -> None:
    gantry = _FakeGantry()
    app = create_app(_registry(gantry), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/api/gantry/home",
            headers=AUTH_HEADERS,
        )
        operation_id = _assert_accepted(response)
        _wait_for_operation(client, operation_id)
        status = client.get("/api/status", headers=AUTH_HEADERS)

    assert status.status_code == 200
    last_operation = status.json()["last_operation"]
    assert last_operation["id"] == operation_id
    assert last_operation["status"] == "succeeded"


def test_unattached_gantry_returns_structured_503_without_taking_gate() -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post("/api/gantry/connect", headers=AUTH_HEADERS)
        current = client.get(
            "/api/operations/current",
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 503
    assert response.json()["error"]["error_code"] == "L3.DEVICE_NOT_ATTACHED"
    assert current.status_code == 200
    assert current.json() is None


def test_nan_and_infinity_are_rejected_at_web_boundary() -> None:
    """NaN/Infinity 必须在 Web 边界同步 422，不许拿到 202 再异步失败（审查补丁回归）。"""
    gantry = _FakeGantry()
    app = create_app(_registry(gantry), token=TOKEN)
    with TestClient(app) as client:
        for body in (
            '{"x": NaN, "y": 0, "z": 0, "feed": 100}',
            '{"x": 0, "y": Infinity, "z": 0, "feed": 100}',
            '{"x": 0, "y": 0, "z": 0, "feed": Infinity}',
        ):
            response = client.post(
                "/api/gantry/move",
                content=body,
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            )
            assert response.status_code == 422, body

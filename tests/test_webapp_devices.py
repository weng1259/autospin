"""W3.3 外设 Web 端点；全测试只使用假 backend。"""
from __future__ import annotations

import threading
import time
from typing import cast

from fastapi.testclient import TestClient
from pydantic import BaseModel
import pytest

from src.hardware.gripper_backend import GripperBackend
from src.hardware.heater_backend import HeaterBackend
from src.hardware.linearstage_backend import (
    LinearStageActionResult,
    LinearStageBackend,
)
from src.hardware.pipette_backend import PipetteBackend
from src.hardware.relay_backend import RelayBackend
from src.hardware.spincoater_backend import SpincoaterBackend
from src.system_estop import SystemEstop
from src.webapp import DeviceRegistry, create_app


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class _FakeStatus(BaseModel):
    ready: bool = True


class _FakeHeater:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.block_set_sv = False
        self.set_sv_entered = threading.Event()
        self.release_set_sv = threading.Event()

    def status(self) -> _FakeStatus:
        return _FakeStatus()

    def connect(self) -> None:
        self.calls.append(("connect",))

    def close(self) -> None:
        self.calls.append(("close",))

    def set_sv(
        self,
        sv_c: float,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("set_sv", sv_c, idempotency_key))
        self.set_sv_entered.set()
        if self.block_set_sv:
            self.release_set_sv.wait(timeout=2.0)
        return {"target_sv_c": sv_c}

    def read_pv(self) -> _FakeStatus:
        self.calls.append(("read_pv",))
        return _FakeStatus()


class _FakeSpincoater:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def status(self) -> _FakeStatus:
        return _FakeStatus()

    def connect(self) -> None:
        self.calls.append(("connect",))

    def close(self) -> None:
        self.calls.append(("close",))

    def start(
        self,
        rpm: float,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("start", rpm, idempotency_key))
        return {"target_rpm": rpm}

    def set_deceleration(self, rpm_per_s: float) -> object:
        self.calls.append(("set_deceleration", rpm_per_s))
        return {"deceleration_rpm_per_s": rpm_per_s}

    def stop(
        self,
        *,
        use_brake: bool = True,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("stop", use_brake, idempotency_key))
        return {"brake_engaged": use_brake}

    def read_fault(self) -> _FakeStatus:
        self.calls.append(("read_fault",))
        return _FakeStatus()


class _FakePipette:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def status(self) -> _FakeStatus:
        return _FakeStatus()

    def connect(self) -> None:
        self.calls.append(("connect",))

    def close(self) -> None:
        self.calls.append(("close",))

    def home(self, *, idempotency_key: str | None = None) -> object:
        self.calls.append(("home", idempotency_key))
        return {"homed": True}

    def aspirate(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("aspirate", volume_ul, idempotency_key))
        return {"volume_ul": volume_ul}

    def dispense(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("dispense", volume_ul, idempotency_key))
        return {"volume_ul": volume_ul}

    def eject_tip(
        self,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("eject_tip", idempotency_key))
        return {"ejected": True}


class _FakeLinearStage:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.block_move = False
        self.move_entered = threading.Event()
        self.release_move = threading.Event()

    def status(self) -> _FakeStatus:
        return _FakeStatus()

    def connect(self) -> None:
        self.calls.append(("connect",))

    def close(self) -> None:
        self.calls.append(("close",))

    def home(self, *, idempotency_key: str | None = None) -> object:
        self.calls.append(("home", idempotency_key))
        return {"homed": True}

    def move_to(
        self,
        position_mm: float,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(("move_to", position_mm, idempotency_key))
        self.move_entered.set()
        if self.block_move:
            self.release_move.wait(timeout=2.0)
        return {"position_mm": position_mm}

    def stop(self) -> LinearStageActionResult:
        self.calls.append(("stop",))
        return LinearStageActionResult(
            success=True,
            action="stop",
            target_position_mm=None,
            final_position_mm=12.5,
            dry_run=False,
            action_description="fake immediate stop",
            duration_ms=0.1,
            event_id="fake-stop",
        )


class _FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_state(self) -> _FakeStatus:
        return _FakeStatus()

    def connect(self) -> None:
        self.calls.append(("connect",))

    def close(self) -> None:
        self.calls.append(("close",))

    def ch_on(self, channel: int, *, idempotency_key: str) -> object:
        self.calls.append(("ch_on", channel, idempotency_key))
        return {"channel": channel, "on": True}

    def ch_off(self, channel: int, *, idempotency_key: str) -> object:
        self.calls.append(("ch_off", channel, idempotency_key))
        return {"channel": channel, "on": False}


class _FakeGripper:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_state(self) -> _FakeStatus:
        return _FakeStatus()

    def open(self, *, idempotency_key: str) -> object:
        self.calls.append(("open", idempotency_key))
        return {"open": True}

    def close(self, *, idempotency_key: str) -> object:
        self.calls.append(("close", idempotency_key))
        return {"open": False}


class _FakeDevices:
    def __init__(self) -> None:
        self.heater = _FakeHeater()
        self.spincoater = _FakeSpincoater()
        self.pipette = _FakePipette()
        self.linear_stage = _FakeLinearStage()
        self.relay = _FakeRelay()
        self.gripper = _FakeGripper()

    def registry(self) -> DeviceRegistry:
        return DeviceRegistry(
            heater=cast(HeaterBackend, self.heater),
            spincoater=cast(SpincoaterBackend, self.spincoater),
            pipette=cast(PipetteBackend, self.pipette),
            linear_stage=cast(LinearStageBackend, self.linear_stage),
            relay=cast(RelayBackend, self.relay),
            gripper=cast(GripperBackend, self.gripper),
            mock=True,
        )

    def call_count(self, device: str) -> int:
        fake = cast(object, getattr(self, device))
        return len(fake.calls)


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


def test_all_device_endpoints_forward_typed_arguments() -> None:
    devices = _FakeDevices()
    app = create_app(devices.registry(), token=TOKEN)

    cases = [
        ("post", "/api/heater/connect", None),
        ("post", "/api/heater/disconnect", None),
        (
            "post",
            "/api/heater/set-sv",
            {"sv_c": 82.5, "idempotency_key": "heater-key"},
        ),
        ("get", "/api/heater/pv", None),
        ("post", "/api/spincoater/connect", None),
        ("post", "/api/spincoater/disconnect", None),
        (
            "post",
            "/api/spincoater/start",
            {"rpm": 2500.0, "idempotency_key": "spin-start-key"},
        ),
        (
            "post",
            "/api/spincoater/stop",
            {"use_brake": False, "idempotency_key": "spin-stop-key"},
        ),
        ("get", "/api/spincoater/fault", None),
        ("post", "/api/pipette/connect", None),
        ("post", "/api/pipette/disconnect", None),
        (
            "post",
            "/api/pipette/home",
            {"idempotency_key": "pipette-home-key"},
        ),
        (
            "post",
            "/api/pipette/aspirate",
            {"volume_ul": 10.5, "idempotency_key": "aspirate-key"},
        ),
        (
            "post",
            "/api/pipette/dispense",
            {"volume_ul": 8.25, "idempotency_key": "dispense-key"},
        ),
        (
            "post",
            "/api/pipette/eject-tip",
            {"idempotency_key": "eject-key"},
        ),
        ("post", "/api/linearstage/connect", None),
        ("post", "/api/linearstage/disconnect", None),
        (
            "post",
            "/api/linearstage/home",
            {"idempotency_key": "stage-home-key"},
        ),
        (
            "post",
            "/api/linearstage/move",
            {"position_mm": 12.5, "idempotency_key": "stage-move-key"},
        ),
        ("post", "/api/relay/connect", None),
        ("post", "/api/relay/disconnect", None),
        (
            "post",
            "/api/relay/ch",
            {"channel": 3, "on": True, "idempotency_key": "relay-on-key"},
        ),
        (
            "post",
            "/api/relay/ch",
            {"channel": 8, "on": False, "idempotency_key": "relay-off-key"},
        ),
        (
            "post",
            "/api/gripper/open",
            {"idempotency_key": "gripper-open-key"},
        ),
        (
            "post",
            "/api/gripper/close",
            {"idempotency_key": "gripper-close-key"},
        ),
    ]

    with TestClient(app) as client:
        for method, path, body in cases:
            response = (
                client.get(path, headers=AUTH_HEADERS)
                if method == "get"
                else client.post(path, headers=AUTH_HEADERS, json=body)
            )
            operation_id = _assert_accepted(response)
            record = _wait_for_operation(client, operation_id)
            assert record["status"] == "succeeded"

        stop = client.post(
            "/api/linearstage/stop",
            headers=AUTH_HEADERS,
        )

    assert stop.status_code == 200
    assert stop.json()["action"] == "stop"
    assert devices.heater.calls == [
        ("connect",),
        ("close",),
        ("set_sv", 82.5, "heater-key"),
        ("read_pv",),
    ]
    assert devices.spincoater.calls == [
        ("connect",),
        ("close",),
        ("start", 2500.0, "spin-start-key"),
        ("stop", False, "spin-stop-key"),
        ("read_fault",),
    ]
    assert devices.pipette.calls == [
        ("connect",),
        ("close",),
        ("home", "pipette-home-key"),
        ("aspirate", 10.5, "aspirate-key"),
        ("dispense", 8.25, "dispense-key"),
        ("eject_tip", "eject-key"),
    ]
    assert devices.linear_stage.calls == [
        ("connect",),
        ("close",),
        ("home", "stage-home-key"),
        ("move_to", 12.5, "stage-move-key"),
        ("stop",),
    ]
    assert devices.relay.calls == [
        ("connect",),
        ("close",),
        ("ch_on", 3, "relay-on-key"),
        ("ch_off", 8, "relay-off-key"),
    ]
    assert devices.gripper.calls == [
        ("open", "gripper-open-key"),
        ("close", "gripper-close-key"),
    ]


@pytest.mark.parametrize(
    ("device", "method", "path", "body"),
    [
        ("heater", "post", "/api/heater/disconnect", None),
        (
            "spincoater",
            "post",
            "/api/spincoater/disconnect",
            None,
        ),
        ("pipette", "post", "/api/pipette/disconnect", None),
        (
            "linear_stage",
            "post",
            "/api/linearstage/disconnect",
            None,
        ),
        ("relay", "post", "/api/relay/connect", None),
        ("relay", "post", "/api/relay/disconnect", None),
        ("heater", "get", "/api/heater/pv", None),
        ("spincoater", "get", "/api/spincoater/fault", None),
        (
            "pipette",
            "post",
            "/api/pipette/home",
            {"idempotency_key": "busy-pipette"},
        ),
        (
            "linear_stage",
            "post",
            "/api/linearstage/move",
            {"position_mm": 5.0, "idempotency_key": "busy-stage"},
        ),
        (
            "relay",
            "post",
            "/api/relay/ch",
            {"channel": 3, "on": True, "idempotency_key": "busy-relay"},
        ),
        (
            "gripper",
            "post",
            "/api/gripper/close",
            {"idempotency_key": "busy-gripper"},
        ),
    ],
)
def test_each_device_rejects_gated_endpoint_while_operation_is_running(
    device: str,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    devices = _FakeDevices()
    devices.heater.block_set_sv = True
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/heater/set-sv",
            headers=AUTH_HEADERS,
            json={"sv_c": 60.0, "idempotency_key": "blocker"},
        )
        blocker_id = _assert_accepted(accepted)
        assert devices.heater.set_sv_entered.wait(timeout=1.0)
        calls_before = devices.call_count(device)

        try:
            response = (
                client.get(path, headers=AUTH_HEADERS)
                if method == "get"
                else client.post(path, headers=AUTH_HEADERS, json=body)
            )

            assert response.status_code == 409
            payload = response.json()
            assert payload["error"]["error_code"] == "L3.OPERATION_CONFLICT"
            assert payload["current_operation"]["id"] == blocker_id
            assert payload["current_operation"]["device"] == "heater"
            assert payload["current_operation"]["action"] == "set-sv"
            assert devices.call_count(device) == calls_before
        finally:
            devices.heater.release_set_sv.set()
            _wait_for_operation(client, blocker_id)


@pytest.mark.parametrize("channel", [1, 2])
def test_relay_reserved_channels_return_422_without_backend_call(
    channel: int,
) -> None:
    devices = _FakeDevices()
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/api/relay/ch",
            headers=AUTH_HEADERS,
            json={
                "channel": channel,
                "on": True,
                "idempotency_key": "must-not-run",
            },
        )

    assert response.status_code == 422
    assert devices.relay.calls == []


def test_linear_stage_stop_bypasses_gate_during_running_move() -> None:
    devices = _FakeDevices()
    devices.linear_stage.block_move = True
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/linearstage/move",
            headers=AUTH_HEADERS,
            json={"position_mm": 20.0, "idempotency_key": "blocking-move"},
        )
        blocker_id = _assert_accepted(accepted)
        assert devices.linear_stage.move_entered.wait(timeout=1.0)

        try:
            started = time.monotonic()
            response = client.post(
                "/api/linearstage/stop",
                headers=AUTH_HEADERS,
            )
            elapsed_s = time.monotonic() - started
            current = client.get(
                "/api/operations/current",
                headers=AUTH_HEADERS,
            )

            assert response.status_code == 200
            assert response.json()["action"] == "stop"
            assert elapsed_s < 0.5
            assert current.status_code == 200
            assert current.json()["id"] == blocker_id
            assert ("stop",) in devices.linear_stage.calls
        finally:
            devices.linear_stage.release_move.set()
            _wait_for_operation(client, blocker_id)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/heater/connect", None),
        ("post", "/api/heater/disconnect", None),
        ("get", "/api/heater/pv", None),
        ("post", "/api/spincoater/connect", None),
        ("post", "/api/spincoater/disconnect", None),
        ("post", "/api/pipette/connect", None),
        ("post", "/api/pipette/disconnect", None),
        ("post", "/api/linearstage/connect", None),
        ("post", "/api/linearstage/disconnect", None),
        ("post", "/api/linearstage/stop", None),
        ("post", "/api/relay/connect", None),
        ("post", "/api/relay/disconnect", None),
        (
            "post",
            "/api/relay/ch",
            {"channel": 3, "on": True, "idempotency_key": "no-relay"},
        ),
        ("post", "/api/gripper/open", None),
    ],
)
def test_unattached_device_returns_structured_503(
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    app = create_app(
        DeviceRegistry(estop=SystemEstop(), mock=True),
        token=TOKEN,
    )

    with TestClient(app) as client:
        response = (
            client.get(path, headers=AUTH_HEADERS)
            if method == "get"
            else client.post(path, headers=AUTH_HEADERS, json=body)
        )
        current = client.get(
            "/api/operations/current",
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 503
    assert response.json()["error"]["error_code"] == "L3.DEVICE_NOT_ATTACHED"
    assert current.status_code == 200
    assert current.json() is None


def test_missing_idempotency_key_defaults_to_operation_id() -> None:
    devices = _FakeDevices()
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/api/gripper/open",
            headers=AUTH_HEADERS,
        )
        operation_id = _assert_accepted(response)
        record = _wait_for_operation(client, operation_id)

    assert record["status"] == "succeeded"
    assert devices.gripper.calls == [("open", operation_id)]


def test_spincoater_stop_defaults_to_brake() -> None:
    devices = _FakeDevices()
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/api/spincoater/stop",
            headers=AUTH_HEADERS,
        )
        operation_id = _assert_accepted(response)
        record = _wait_for_operation(client, operation_id)

    assert record["status"] == "succeeded"
    assert devices.spincoater.calls == [("stop", True, operation_id)]


def test_spincoater_deceleration_route_updates_normal_stop_ramp() -> None:
    devices = _FakeDevices()
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/api/spincoater/deceleration",
            headers=AUTH_HEADERS,
            json={"rpm_per_s": 350.0},
        )
        operation_id = _assert_accepted(response)
        record = _wait_for_operation(client, operation_id)

    assert record["status"] == "succeeded"
    assert devices.spincoater.calls == [("set_deceleration", 350.0)]


@pytest.mark.parametrize(
    ("device", "path", "body_template"),
    [
        ("heater", "/api/heater/set-sv", '{"sv_c": %s}'),
        ("spincoater", "/api/spincoater/start", '{"rpm": %s}'),
        ("pipette", "/api/pipette/aspirate", '{"volume_ul": %s}'),
        ("pipette", "/api/pipette/dispense", '{"volume_ul": %s}'),
        ("linear_stage", "/api/linearstage/move", '{"position_mm": %s}'),
    ],
)
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_rejected_at_web_boundary(
    device: str,
    path: str,
    body_template: str,
    literal: str,
) -> None:
    """NaN/Infinity 必须在 Web 边界同步 422，不许拿到 202 再异步失败。

    变异证据（审查 P2）：删掉 float 字段的 allow_inf_nan=False 时全仓测试
    仍全绿——本测试就是该约定在外设端点的回归锚，与 test_webapp_gantry 对齐。
    """
    devices = _FakeDevices()
    app = create_app(devices.registry(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            path,
            content=body_template % literal,
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )

    assert response.status_code == 422, (path, literal)
    assert devices.call_count(device) == 0

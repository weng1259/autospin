"""W3.4b：七设备内存 mock 的注册、活体端点与状态合同。"""
from __future__ import annotations

import importlib
import sys
import time
from typing import cast

from fastapi.testclient import TestClient
import pytest

from src.hardware.heater_backend import HeaterStatus
from src.hardware.linearstage_backend import LinearStageStatus
from src.hardware.pipette_backend import PipetteStatus
from src.hardware.spincoater_backend import SpinStatus
from src.hardware.types import MachineStatus, RelayState, GripperState
from src.webapp import DeviceRegistry, create_app


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
DEVICE_NAMES = {
    "gantry",
    "relay",
    "gripper",
    "heater",
    "spincoater",
    "pipette",
    "linear_stage",
}


def _serial_modules() -> set[str]:
    return {
        name
        for name in sys.modules
        if name == "serial" or name.startswith("serial.")
    }


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
        operation = cast(dict[str, object], response.json())
        if operation["status"] != "running":
            return operation
        time.sleep(0.001)
    pytest.fail(f"operation {operation_id} did not complete")


def _wait_for_written_snapshot(
    client: TestClient,
    *,
    timeout_s: float = 2.0,
) -> dict[str, dict[str, object]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get("/api/status", headers=AUTH_HEADERS)
        assert response.status_code == 200
        devices = cast(
            dict[str, dict[str, object]],
            response.json()["devices"],
        )
        if (
            set(devices) == DEVICE_NAMES
            and devices["gantry"].get("is_homed") is True
            and devices["heater"].get("sv_c") == 75.0
            and devices["spincoater"].get("running") is True
            and devices["pipette"].get("homed") is True
            and devices["linear_stage"].get("homed") is True
            and cast(dict[str, bool], devices["relay"].get("channels", {})).get(
                "3"
            )
            is True
            and devices["gripper"].get("commanded_state") == "closed"
        ):
            return devices
        time.sleep(0.01)
    pytest.fail("mock status poller did not expose the written seven-device state")


def test_importing_mock_devices_adds_no_serial_modules() -> None:
    """src.webapp 已加载；mock 子模块自身不得再带入任何 serial 模块。"""
    sys.modules.pop("src.webapp.mock_devices", None)
    before = _serial_modules()

    importlib.import_module("src.webapp.mock_devices")

    assert _serial_modules() - before == set()


def test_from_mocks_attaches_seven_devices_with_real_status_models() -> None:
    registry = DeviceRegistry.from_mocks()

    devices = (
        registry.gantry,
        registry.relay,
        registry.gripper,
        registry.heater,
        registry.spincoater,
        registry.pipette,
        registry.linear_stage,
    )
    assert registry.mock is True
    assert all(device is not None for device in devices)
    assert len({id(device) for device in devices}) == 7

    assert registry.gantry is not None
    assert registry.relay is not None
    assert registry.gripper is not None
    assert registry.heater is not None
    assert registry.spincoater is not None
    assert registry.pipette is not None
    assert registry.linear_stage is not None
    assert isinstance(registry.gantry.get_status(), MachineStatus)
    assert isinstance(registry.relay.get_state(), RelayState)
    assert isinstance(registry.gripper.get_state(), GripperState)
    assert isinstance(registry.heater.status(), HeaterStatus)
    assert isinstance(registry.spincoater.status(), SpinStatus)
    assert isinstance(registry.pipette.status(), PipetteStatus)
    assert isinstance(registry.linear_stage.status(), LinearStageStatus)


def test_each_mock_device_write_is_accepted_and_succeeds_through_gate() -> None:
    registry = DeviceRegistry.from_mocks()
    app = create_app(registry, token=TOKEN)
    cases: list[tuple[str, dict[str, object]]] = [
        ("/api/gantry/home", {"idempotency_key": "mock-gantry"}),
        (
            "/api/relay/ch",
            {"channel": 3, "on": True, "idempotency_key": "mock-relay"},
        ),
        ("/api/gripper/close", {"idempotency_key": "mock-gripper"}),
        (
            "/api/heater/set-sv",
            {"sv_c": 75.0, "idempotency_key": "mock-heater"},
        ),
        (
            "/api/spincoater/start",
            {"rpm": 1800.0, "idempotency_key": "mock-spincoater"},
        ),
        ("/api/pipette/home", {"idempotency_key": "mock-pipette"}),
        (
            "/api/linearstage/home",
            {"idempotency_key": "mock-linear-stage"},
        ),
    ]

    with TestClient(app) as client:
        for path, body in cases:
            response = client.post(path, headers=AUTH_HEADERS, json=body)
            assert response.status_code == 202, path
            payload = response.json()
            assert payload["accepted"] is True
            operation = _wait_for_operation(client, payload["operation_id"])
            assert operation["status"] == "succeeded", path
        devices = _wait_for_written_snapshot(client)

    assert devices["gantry"]["state"] == "idle"
    assert devices["gantry"]["position"] == {
        "x_mm": 0.0,
        "y_mm": 0.0,
        "z_mm": 0.0,
    }
    assert devices["heater"]["pv_c"] == 75.0
    assert devices["heater"]["sv_c"] == 75.0
    assert devices["spincoater"]["target_rpm"] == 1800.0
    assert devices["spincoater"]["fault_bits"] == []
    assert devices["pipette"]["position_steps"] == 0
    assert devices["pipette"]["tip_present"] is True
    assert devices["linear_stage"]["position_mm"] == 0.0
    assert devices["linear_stage"]["flags_raw"] == 3
    relay_channels = cast(dict[str, bool], devices["relay"]["channels"])
    assert relay_channels["3"] is True
    assert relay_channels["8"] is False
    assert devices["gripper"]["commanded_state"] == "closed"
    assert devices["gripper"]["position_known"] is False


def test_mock_lifecycle_routes_flip_six_device_connection_states() -> None:
    registry = DeviceRegistry.from_mocks()
    assert registry.gantry is not None
    assert registry.relay is not None
    assert registry.heater is not None
    assert registry.spincoater is not None
    assert registry.pipette is not None
    assert registry.linear_stage is not None
    app = create_app(registry, token=TOKEN)
    connect_paths = [
        "/api/gantry/connect",
        "/api/heater/connect",
        "/api/spincoater/connect",
        "/api/pipette/connect",
        "/api/linearstage/connect",
        "/api/relay/connect",
    ]
    disconnect_paths = [
        "/api/gantry/disconnect",
        "/api/heater/disconnect",
        "/api/spincoater/disconnect",
        "/api/pipette/disconnect",
        "/api/linearstage/disconnect",
        "/api/relay/disconnect",
    ]

    with TestClient(app) as client:
        for path in connect_paths:
            response = client.post(path, headers=AUTH_HEADERS)
            assert response.status_code == 202, path
            operation = _wait_for_operation(
                client,
                response.json()["operation_id"],
            )
            assert operation["status"] == "succeeded", path

        assert registry.gantry.is_connected() is True
        assert registry.relay.is_connected() is True
        assert registry.heater.status().connected is True
        assert registry.spincoater.status().connected is True
        assert registry.pipette.status().connected is True
        assert registry.linear_stage.status().connected is True

        for path in disconnect_paths:
            response = client.post(path, headers=AUTH_HEADERS)
            assert response.status_code == 202, path
            operation = _wait_for_operation(
                client,
                response.json()["operation_id"],
            )
            assert operation["status"] == "succeeded", path

    assert registry.gantry.is_connected() is False
    assert registry.relay.is_connected() is False
    assert registry.heater.status().connected is False
    assert registry.spincoater.status().connected is False
    assert registry.pipette.status().connected is False
    assert registry.linear_stage.status().connected is False


def test_mock_system_estop_uses_five_motion_and_heat_devices() -> None:
    registry = DeviceRegistry.from_mocks()
    assert registry.gantry is not None
    assert registry.relay is not None
    assert registry.gripper is not None
    assert registry.heater is not None
    assert registry.spincoater is not None
    assert registry.pipette is not None
    assert registry.linear_stage is not None

    registry.gantry.home(idempotency_key="prime-gantry")
    registry.relay.ch_on(3, idempotency_key="prime-relay")
    registry.gripper.close(idempotency_key="prime-gripper")
    registry.heater.set_sv(80.0, idempotency_key="prime-heater")
    registry.spincoater.start(1200.0, idempotency_key="prime-spincoater")
    registry.pipette.home(idempotency_key="prime-pipette")
    registry.linear_stage.home(idempotency_key="prime-linear-stage")
    app = create_app(registry, token=TOKEN)

    with TestClient(app) as client:
        response = client.post("/api/estop", headers=AUTH_HEADERS)

    assert response.status_code == 200
    report = response.json()
    assert report["ok"] is True
    assert [step["device"] for step in report["steps"]] == [
        "gantry",
        "spincoater",
        "linear_stage",
        "pipette",
        "heater",
    ]
    assert all(
        step["ok"] is True and step["skipped"] is False
        for step in report["steps"]
    )
    assert registry.gantry.is_homed() is False
    assert registry.spincoater.status().running is False
    assert registry.spincoater.status().brake_engaged is True
    assert registry.linear_stage.status().moving is False
    assert registry.pipette.status().homed is False
    assert registry.heater.status().sv_c == 0.0
    assert registry.heater.status().pv_c == 0.0
    assert registry.relay.get_state().channels[3] is True
    assert registry.gripper.get_state().commanded_state == "closed"

"""W3.6 示教-重放 Web 端点与程序面板合同；全程只用内存 mock。"""
from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest

from src.hardware.types import Position
from src.routine import RecordingProxy, Routine, RoutineRecorder, RoutineStep
from src.webapp import DeviceRegistry, create_app


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "webapp" / "static"


def _wait_for_operation(
    client: TestClient,
    operation_id: str,
    *,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/operations/{operation_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        operation = cast(dict[str, Any], response.json())
        if operation["status"] != "running":
            return operation
        time.sleep(0.005)
    pytest.fail(f"operation {operation_id} did not complete")


def _wait_for_progress(
    client: TestClient,
    operation_id: str,
    *,
    minimum_completed: int,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(
            "/api/operations/current",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        operation = response.json()
        if (
            isinstance(operation, dict)
            and operation.get("id") == operation_id
            and isinstance(operation.get("result"), dict)
            and operation["result"].get("steps_completed", -1)
            >= minimum_completed
        ):
            return cast(dict[str, Any], operation)
        time.sleep(0.005)
    pytest.fail(
        f"operation {operation_id} did not reach step {minimum_completed}"
    )


def _write_routine(base_dir: Path, routine: Routine) -> Path:
    path = base_dir / f"{routine.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(routine.to_json(), encoding="utf-8")
    return path


def _three_step_gripper_routine(name: str = "三步夹爪") -> Routine:
    return Routine(
        name=name,
        steps=[
            RoutineStep(
                seq=0,
                device="gripper",
                action="open",
                label="夹爪张开一",
            ),
            RoutineStep(
                seq=1,
                device="gripper",
                action="close",
                label="夹爪夹紧二",
            ),
            RoutineStep(
                seq=2,
                device="gripper",
                action="open",
                label="夹爪张开三",
            ),
        ],
    )


def test_record_flow_keeps_two_successes_and_drops_failed_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert all(isinstance(device, RecordingProxy) for device in devices)
    app = create_app(registry, token=TOKEN, routines_path=tmp_path)

    assert registry.gripper is not None
    raw_gripper = cast(RecordingProxy, registry.gripper).unwrapped

    def fail_open(
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> None:
        del idempotency_key, dry_run
        raise RuntimeError("injected gripper failure")

    with TestClient(app) as client:
        armed = client.post(
            "/api/routines/record/arm",
            headers=AUTH_HEADERS,
            json={"name": "录制流程"},
        )
        assert armed.status_code == 200
        assert armed.json()["armed"] is True

        close_response = client.post(
            "/api/gripper/close",
            headers=AUTH_HEADERS,
            json={},
        )
        close_operation = _wait_for_operation(
            client,
            close_response.json()["operation_id"],
        )
        assert close_operation["status"] == "succeeded"

        relay_response = client.post(
            "/api/relay/ch",
            headers=AUTH_HEADERS,
            json={"channel": 3, "on": True},
        )
        relay_operation = _wait_for_operation(
            client,
            relay_response.json()["operation_id"],
        )
        assert relay_operation["status"] == "succeeded"

        monkeypatch.setattr(raw_gripper, "open", fail_open)
        failed_response = client.post(
            "/api/gripper/open",
            headers=AUTH_HEADERS,
            json={},
        )
        failed_operation = _wait_for_operation(
            client,
            failed_response.json()["operation_id"],
        )
        assert failed_operation["status"] == "failed"

        status = client.get(
            "/api/routines/record",
            headers=AUTH_HEADERS,
        )
        assert status.json()["step_count"] == 2
        assert len(status.json()["recent_steps"]) == 2

        disarmed = client.post(
            "/api/routines/record/disarm",
            headers=AUTH_HEADERS,
            json={},
        )
        assert disarmed.status_code == 200
        assert disarmed.json()["recording"]["armed"] is False
        assert disarmed.json()["saved"]["step_count"] == 2

    saved = Routine.load(tmp_path / "录制流程.json")
    assert [(step.device, step.action) for step in saved.steps] == [
        ("gripper", "close"),
        ("relay", "ch_on"),
    ]


def test_atomic_disarm_replace_failure_preserves_original_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Routine(name="原子程序", description="original")
    destination = _write_routine(tmp_path, original)
    original_text = destination.read_text(encoding="utf-8")
    registry = DeviceRegistry.from_mocks()
    app = create_app(registry, token=TOKEN, routines_path=tmp_path)
    replace_paths: list[tuple[Path, Path]] = []

    def fail_replace(source: str | Path, target: str | Path) -> None:
        replace_paths.append((Path(source), Path(target)))
        raise OSError("injected os.replace failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        arm = client.post(
            "/api/routines/record/arm",
            headers=AUTH_HEADERS,
            json={"name": "原子程序"},
        )
        assert arm.status_code == 200
        action = client.post(
            "/api/gripper/close",
            headers=AUTH_HEADERS,
            json={},
        )
        _wait_for_operation(client, action.json()["operation_id"])

        monkeypatch.setattr(
            "src.webapp.routes_routines.os.replace",
            fail_replace,
        )
        response = client.post(
            "/api/routines/record/disarm",
            headers=AUTH_HEADERS,
            json={},
        )

    assert response.status_code == 500
    assert destination.read_text(encoding="utf-8") == original_text
    assert len(replace_paths) == 1
    temporary, attempted_destination = replace_paths[0]
    assert temporary.parent == destination.parent
    assert attempted_destination == destination
    assert not temporary.exists()


def test_replay_three_steps_updates_operation_progress_to_three_of_three(
    tmp_path: Path,
) -> None:
    routine = _three_step_gripper_routine()
    _write_routine(tmp_path, routine)
    registry = DeviceRegistry.from_mocks()
    app = create_app(registry, token=TOKEN, routines_path=tmp_path)

    with TestClient(app) as client:
        response = client.post(
            f"/api/routines/{routine.name}/replay",
            headers=AUTH_HEADERS,
            json={},
        )
        assert response.status_code == 202
        operation_id = cast(str, response.json()["operation_id"])
        progress = _wait_for_progress(
            client,
            operation_id,
            minimum_completed=1,
        )
        assert progress["device"] == "routine"
        assert progress["action"] == "replay"
        assert progress["result"]["steps_total"] == 3
        completed = _wait_for_operation(client, operation_id)

    assert completed["status"] == "succeeded"
    assert completed["result"]["success"] is True
    assert completed["result"]["steps_completed"] == 3
    assert completed["result"]["steps_total"] == 3
    assert completed["result"]["step_label"] == "夹爪张开三"
    assert registry.gripper is not None
    assert registry.gripper.get_state().commanded_state == "open"


def test_replay_occupies_gate_and_abort_bypasses_it_then_releases(
    tmp_path: Path,
) -> None:
    routine = _three_step_gripper_routine("可中止程序")
    _write_routine(tmp_path, routine)
    registry = DeviceRegistry.from_mocks()
    app = create_app(registry, token=TOKEN, routines_path=tmp_path)

    with TestClient(app) as client:
        replay = client.post(
            f"/api/routines/{routine.name}/replay",
            headers=AUTH_HEADERS,
            json={},
        )
        operation_id = cast(str, replay.json()["operation_id"])
        _wait_for_progress(client, operation_id, minimum_completed=1)

        conflicting_move = client.post(
            "/api/gantry/move",
            headers=AUTH_HEADERS,
            json={"x": -10.0, "y": -10.0, "z": -10.0, "feed": 1000.0},
        )
        assert conflicting_move.status_code == 409
        assert conflicting_move.json()["current_operation"]["id"] == operation_id

        abort = client.post(
            "/api/routines/replay/abort",
            headers=AUTH_HEADERS,
            json={},
        )
        assert abort.status_code == 200
        assert abort.json() == {
            "abort_requested": True,
            "operation_id": operation_id,
        }

        completed = _wait_for_operation(client, operation_id)
        assert completed["status"] == "failed"
        assert completed["error"]["error_code"] == (
            "L3.ROUTINE_REPLAY_ABORTED"
        )

        next_action = client.post(
            "/api/gripper/close",
            headers=AUTH_HEADERS,
            json={},
        )
        assert next_action.status_code == 202
        next_completed = _wait_for_operation(
            client,
            next_action.json()["operation_id"],
        )
        assert next_completed["status"] == "succeeded"


def test_unhomed_motion_routine_becomes_structured_operation_error(
    tmp_path: Path,
) -> None:
    recorder = RoutineRecorder()
    recorder.arm("未归零运动")
    recorder.record(
        "gantry",
        "move_to",
        args=(Position(x_mm=-10.0, y_mm=-10.0, z_mm=-10.0),),
        kwargs={"feed_mm_min": 1000.0},
    )
    routine = recorder.to_routine()
    _write_routine(tmp_path, routine)
    registry = DeviceRegistry.from_mocks()
    assert registry.gantry is not None
    assert registry.gantry.is_homed() is False
    app = create_app(registry, token=TOKEN, routines_path=tmp_path)

    with TestClient(app) as client:
        replay = client.post(
            f"/api/routines/{routine.name}/replay",
            headers=AUTH_HEADERS,
            json={},
        )
        assert replay.status_code == 202
        completed = _wait_for_operation(
            client,
            replay.json()["operation_id"],
        )

    assert completed["status"] == "failed"
    assert completed["result"] is None
    error = completed["error"]
    assert error["error_code"] == "L3.ROUTINE_MACHINE_NOT_HOMED"
    assert "未归零" in error["human_message"]
    assert "先完成龙门归零" in error["suggested_action_zh"]


def test_list_detail_and_delete_routine_files(tmp_path: Path) -> None:
    first = _three_step_gripper_routine("程序甲")
    second = Routine(
        name="程序乙",
        steps=[
            RoutineStep(
                seq=0,
                device="control",
                action="wait",
                kwargs={"seconds": 1.0},
                t_offset_s=2.5,
                label="等待 1s",
            )
        ],
    )
    _write_routine(tmp_path, first)
    _write_routine(tmp_path, second)
    app = create_app(
        DeviceRegistry.from_mocks(),
        token=TOKEN,
        routines_path=tmp_path,
    )

    with TestClient(app) as client:
        listing = client.get("/api/routines", headers=AUTH_HEADERS)
        detail = client.get("/api/routines/程序乙", headers=AUTH_HEADERS)
        deleted = client.delete("/api/routines/程序甲", headers=AUTH_HEADERS)
        missing = client.get("/api/routines/程序甲", headers=AUTH_HEADERS)

    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()] == ["程序乙", "程序甲"]
    second_summary = next(
        item for item in listing.json() if item["name"] == "程序乙"
    )
    assert second_summary == {
        "name": "程序乙",
        "step_count": 1,
        "duration_s": 2.5,
        "has_motion": False,
    }
    assert detail.status_code == 200
    assert detail.json()["steps"][0]["label"] == "等待 1s"
    assert deleted.json() == {"deleted": True, "name": "程序甲"}
    assert missing.status_code == 404


def test_routine_panel_reuses_panel_state_machine_and_keeps_abort_enabled() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="routine-panel"' in index
    assert 'data-device="routine"' not in index
    assert 'id="routine-record-form"' in index
    assert 'id="routine-list-body"' in index
    assert 'id="routine-replay-progress"' in index
    assert 'id="routine-replay-label"' in index

    abort_tag = re.search(
        r'<button\b[^>]*id="routine-abort-button"[^>]*>',
        index,
    )
    assert abort_tag is not None
    assert 'data-always-enabled="true"' in abort_tag.group(0)
    assert "disabled" not in abort_tag.group(0)

    for shared_pattern in (
        "panelPending",
        "updatePanelControls",
        "runPanelOperation",
        "runImmediatePanelAction",
    ):
        assert shared_pattern in script
    for endpoint in (
        "/api/routines/record/arm",
        "/api/routines/record/disarm",
        "/api/routines/replay/abort",
    ):
        assert endpoint in script
    assert "确认删除？" in script
    assert "window.confirm" not in script
    assert "alert(" not in script


def test_corrupt_routine_file_does_not_brick_list_delete_or_single_read(
    tmp_path: Path,
) -> None:
    """坏 JSON 文件：列表照常、单读 422、无需 load 即可删（审查 P1 回归锚）。"""
    _write_routine(tmp_path, _three_step_gripper_routine("好程序"))
    (tmp_path / "坏.json").write_text("{ 这不是 json", encoding="utf-8")
    app = create_app(
        DeviceRegistry.from_mocks(),
        token=TOKEN,
        routines_path=tmp_path,
    )

    with TestClient(app) as client:
        listing = client.get("/api/routines", headers=AUTH_HEADERS)
        detail = client.get("/api/routines/坏", headers=AUTH_HEADERS)
        deleted = client.delete("/api/routines/坏", headers=AUTH_HEADERS)

    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()] == ["好程序"]
    assert detail.status_code == 422
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "name": "坏"}
    assert not (tmp_path / "坏.json").exists()
    assert (tmp_path / "好程序.json").exists()


def test_abort_interrupts_wait_step_inside_step_not_at_boundary(
    tmp_path: Path,
) -> None:
    """中止必须打断进行中的 wait 步：裸 sleep 会拖满整步（审查 P1 回归锚）。"""
    routine = Routine(
        name="长保温",
        steps=[
            RoutineStep(
                seq=0,
                device="control",
                action="wait",
                kwargs={"seconds": 30.0},
                label="保温 30s",
            ),
            # 第二步的存在让 wait 被打断后有"下一个步骤边界"可查 abort；
            # 单步程序中止后无边界可查，会以 succeeded 收尾（属 player 语义）。
            RoutineStep(
                seq=1,
                device="gripper",
                action="open",
                label="张开",
            ),
        ],
    )
    _write_routine(tmp_path, routine)
    app = create_app(
        DeviceRegistry.from_mocks(),
        token=TOKEN,
        routines_path=tmp_path,
    )

    with TestClient(app) as client:
        accepted = client.post(
            "/api/routines/长保温/replay",
            headers=AUTH_HEADERS,
            json={},
        )
        assert accepted.status_code == 202
        operation_id = cast(str, accepted.json()["operation_id"])

        started = time.monotonic()
        aborted = client.post(
            "/api/routines/replay/abort",
            headers=AUTH_HEADERS,
            json={},
        )
        assert aborted.status_code == 200
        assert aborted.json()["abort_requested"] is True

        operation = _wait_for_operation(client, operation_id, timeout_s=5.0)
        elapsed_s = time.monotonic() - started

    assert operation["status"] == "failed"
    assert elapsed_s < 5.0, "abort 未打断 wait 步，拖到了步骤边界"

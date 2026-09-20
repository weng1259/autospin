"""W3.2 全局 operation 门闸；全测试只使用内存假动作。"""
from __future__ import annotations

import threading
import time

import pytest

from src.hardware.errors import L3Error, OperationConflictError
from src.webapp import DeviceRegistry
from src.webapp.gate import (
    Operation,
    OperationGate,
    OperationStatusPoller,
    operation_from_conflict,
)


class _FakeActionError(L3Error):
    error_code = "L3.FAKE_ACTION"
    severity = "warning"
    recoverable = True
    suggested_action = "Retry the fake action."
    suggested_action_zh = "重试假动作。"


def _wait_for_completion(
    gate: OperationGate,
    operation_id: str,
    *,
    timeout_s: float = 1.0,
) -> Operation:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        operation = gate.get(operation_id)
        if operation is not None and operation.status != "running":
            return operation
        time.sleep(0.001)
    pytest.fail(f"operation {operation_id} did not complete")


def test_try_start_is_atomic_for_two_simultaneous_threads_50_rounds() -> None:
    for round_index in range(50):
        gate = OperationGate()
        barrier = threading.Barrier(3)
        successes: list[Operation] = []
        conflicts: list[OperationConflictError] = []
        unexpected: list[BaseException] = []

        def attempt(action: str) -> None:
            barrier.wait()
            try:
                successes.append(gate.try_start("gantry", action))
            except OperationConflictError as exc:
                conflicts.append(exc)
            except BaseException as exc:
                unexpected.append(exc)

        first = threading.Thread(target=attempt, args=(f"first-{round_index}",))
        second = threading.Thread(target=attempt, args=(f"second-{round_index}",))
        first.start()
        second.start()
        barrier.wait()
        first.join(timeout=1.0)
        second.join(timeout=1.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert unexpected == []
        assert len(successes) == 1
        assert len(conflicts) == 1
        conflict_operation = operation_from_conflict(conflicts[0])
        assert conflict_operation is not None
        assert conflict_operation.id == successes[0].id


def test_l3_error_is_structured_and_gate_releases_for_next_operation() -> None:
    gate = OperationGate()

    def fail(_: Operation) -> object:
        raise _FakeActionError("假动作失败", "fake action failed")

    failed = gate.submit("gantry", "home", fail)
    record = _wait_for_completion(gate, failed.id)

    assert record.status == "failed"
    assert record.result is None
    assert record.error is not None
    assert record.error.model_dump() == {
        "error_code": "L3.FAKE_ACTION",
        "human_message": "假动作失败",
        "agent_message": "fake action failed",
        "severity": "warning",
        "recoverable": True,
        "suggested_action": "Retry the fake action.",
        "suggested_action_zh": "重试假动作。",
    }
    assert gate.current() is None

    next_operation = gate.try_start("gantry", "move")
    assert next_operation.status == "running"


def test_success_result_and_elapsed_are_recorded() -> None:
    gate = OperationGate()
    release = threading.Event()

    def succeed(_: Operation) -> object:
        release.wait(timeout=1.0)
        return {"position": [1.0, 2.0, 3.0]}

    accepted = gate.submit("gantry", "move", succeed)
    time.sleep(0.002)
    current = gate.current()
    assert current is not None
    assert current.elapsed > 0.0

    release.set()
    record = _wait_for_completion(gate, accepted.id)
    assert record.status == "succeeded"
    assert record.result == {"position": [1.0, 2.0, 3.0]}
    assert record.error is None
    assert record.completed_at is not None
    assert record.elapsed > 0.0


def test_estop_aborts_running_operation_and_ignores_late_result() -> None:
    gate = OperationGate()
    release = threading.Event()

    accepted = gate.submit(
        "spincoater",
        "start",
        lambda _: release.wait(timeout=1.0) or {"started": True},
    )
    aborted = gate.abort_current_after_estop()

    assert aborted is not None
    assert aborted.id == accepted.id
    assert aborted.status == "failed"
    assert aborted.error is not None
    assert aborted.error.error_code == "L3.OPERATION_ABORTED_BY_ESTOP"
    assert gate.current() is None

    next_operation = gate.try_start("spincoater", "start")
    assert next_operation.status == "running"
    release.set()
    time.sleep(0.01)
    assert gate.current() is not None
    assert gate.current().id == next_operation.id


def test_history_is_a_50_entry_ring_buffer() -> None:
    gate = OperationGate()
    operation_ids: list[str] = []

    for index in range(51):
        operation = gate.submit(
            "gantry",
            f"action-{index}",
            lambda _: None,
        )
        operation_ids.append(operation.id)
        _wait_for_completion(gate, operation.id)

    assert gate.get(operation_ids[0]) is None
    assert gate.get(operation_ids[1]) is not None
    assert gate.get(operation_ids[-1]) is not None


def test_completion_advances_status_snapshot_with_last_operation() -> None:
    registry = DeviceRegistry.from_mocks()
    operation_poller = OperationStatusPoller(registry.poller)
    gate = OperationGate(on_completed=operation_poller.record_completed)
    before = operation_poller.snapshot()

    operation = gate.submit("gantry", "home", lambda _: {"homed": True})
    completed = _wait_for_completion(gate, operation.id)
    after = operation_poller.snapshot()

    assert after["seq"] > before["seq"]
    assert after["ts"] >= before["ts"]
    last_operation = after["last_operation"]  # type: ignore[typeddict-item]
    assert isinstance(last_operation, dict)
    assert last_operation["id"] == completed.id
    assert last_operation["status"] == "succeeded"

"""RunLog SQLite connection lifecycle regression tests."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.event_bus import Event
from src.runlog import RunLog


class _TrackedConnection(sqlite3.Connection):
    """sqlite connection that exposes whether ``close()`` was called."""

    was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


def _terminal_event() -> Event:
    return Event(
        event_id="runlog-lifecycle-test",
        method="TestBackend.operation",
        phase="completed",
        params={"value": 1},
        result={"ok": True},
        duration_ms=1.5,
    )


def test_runlog_closes_schema_write_and_query_connections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connections: list[_TrackedConnection] = []

    def tracked_connect(runlog: RunLog) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(runlog.db_path),
            check_same_thread=False,
            factory=_TrackedConnection,
        )
        assert isinstance(conn, _TrackedConnection)
        conn.row_factory = sqlite3.Row
        connections.append(conn)
        return conn

    monkeypatch.setattr(RunLog, "_connect", tracked_connect)

    runlog = RunLog(tmp_path / "runlog.db")
    runlog.record(_terminal_event())
    rows = runlog.query_recent()

    assert len(rows) == 1
    assert rows[0]["event_id"] == "runlog-lifecycle-test"
    assert len(connections) == 3  # schema init, record, query
    assert all(connection.was_closed for connection in connections)


def test_runlog_emits_no_resource_warning_in_fresh_process(tmp_path: Path) -> None:
    """Exercise the real sqlite finalizer under ``-W error::ResourceWarning``."""
    program = """
import gc
import sys
from pathlib import Path

from src.event_bus import Event
from src.runlog import RunLog

runlog = RunLog(Path(sys.argv[1]))
runlog.record(Event(
    event_id="subprocess-lifecycle-test",
    method="TestBackend.operation",
    phase="completed",
))
runlog.query_recent()
del runlog
gc.collect()
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            "-c",
            program,
            str(tmp_path / "subprocess-runlog.db"),
        ],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ResourceWarning" not in completed.stderr

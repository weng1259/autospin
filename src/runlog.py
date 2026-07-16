"""SQLite 事件持久化 —— 每次完成/失败的 API 调用一条结构化记录。

ADR-004 §原则 6：每次 API 调用必须可被复盘 / 训练 / 可视化。

默认 DB 路径：`<repo>/runtime/runlog.db`（gitignored）。
表 schema 简单：append-only events 表 + 时间索引。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .event_bus import Event

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = _REPO_ROOT / "runtime" / "runlog.db"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class RunLog:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Return one transaction-scoped connection and always close it.

        ``sqlite3.Connection``'s own context manager only commits or rolls back;
        it does not close the connection.  Keep that transaction behaviour while
        making the resource lifetime explicit so short-lived tests and processes
        do not depend on garbage collection to release SQLite handles.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connection() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id     TEXT NOT NULL,
                    timestamp    TEXT NOT NULL,
                    method       TEXT NOT NULL,
                    phase        TEXT NOT NULL,
                    params_json  TEXT NOT NULL,
                    result_json  TEXT,
                    error_code   TEXT,
                    error_message TEXT,
                    duration_ms  REAL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp DESC)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_eid ON events(event_id)"
            )

    def record(self, event: Event) -> None:
        """只记录 terminal 事件（completed / error）。started/progress 在
        event_bus 里飘走，不入库。"""
        if event.phase not in ("completed", "error"):
            return
        with self._lock, self._connection() as c:
            c.execute(
                """
                INSERT INTO events
                  (event_id, timestamp, method, phase, params_json,
                   result_json, error_code, error_message, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    _iso(event.timestamp),
                    event.method,
                    event.phase,
                    json.dumps(event.params, ensure_ascii=False, default=str),
                    json.dumps(event.result, ensure_ascii=False, default=str)
                    if event.result is not None else None,
                    event.error_code,
                    event.error_message,
                    event.duration_ms,
                ),
            )

    def query_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as c:
            rows = c.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "seq": r["seq"],
                "event_id": r["event_id"],
                "timestamp": r["timestamp"],
                "method": r["method"],
                "phase": r["phase"],
                "params": json.loads(r["params_json"]),
                "result": json.loads(r["result_json"]) if r["result_json"] else None,
                "error_code": r["error_code"],
                "error_message": r["error_message"],
                "duration_ms": r["duration_ms"],
            })
        return out


# 进程级单例
RUNLOG = RunLog()

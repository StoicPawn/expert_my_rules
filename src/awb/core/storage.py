from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Task, TaskStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    priority REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gates (
    gate_id TEXT PRIMARY KEY,
    passed INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_task(self, task: Task) -> None:
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO tasks(id,title,description,status,created_by,priority,metadata_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title, description=excluded.description, status=excluded.status,
              created_by=excluded.created_by, priority=excluded.priority,
              metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
            """,
            (task.id, task.title, task.description, task.status.value, task.created_by,
             task.priority, json.dumps(task.metadata), now, now),
        )
        self.conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, statuses: Iterable[TaskStatus] | None = None) -> list[Task]:
        if statuses:
            values = [s.value for s in statuses]
            marks = ",".join("?" for _ in values)
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY priority DESC, created_at ASC", values
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM tasks ORDER BY priority DESC, created_at ASC").fetchall()
        return [self._row_to_task(r) for r in rows]

    def event(self, kind: str, payload: dict, task_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(ts,kind,task_id,payload_json) VALUES(?,?,?,?)",
            (self._now(), kind, task_id, json.dumps(payload)),
        )
        self.conn.commit()

    def set_gate(self, gate_id: str, passed: bool, detail: str = "") -> None:
        self.conn.execute(
            """INSERT INTO gates(gate_id,passed,detail,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(gate_id) DO UPDATE SET passed=excluded.passed, detail=excluded.detail, updated_at=excluded.updated_at""",
            (gate_id, int(passed), detail, self._now()),
        )
        self.conn.commit()

    def gate_state(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM gates").fetchall()
        return {r["gate_id"]: {"passed": bool(r["passed"]), "detail": r["detail"]} for r in rows}

    def recent_events(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            out.append({"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "task_id": r["task_id"], "payload": json.loads(r["payload_json"])})
        return out

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], title=row["title"], description=row["description"],
            status=TaskStatus(row["status"]), created_by=row["created_by"],
            priority=row["priority"], metadata=json.loads(row["metadata_json"]),
        )

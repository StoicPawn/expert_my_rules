from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = '''
CREATE TABLE IF NOT EXISTS project_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_id TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_project_memory_task ON project_memory(task_id, id);
CREATE INDEX IF NOT EXISTS idx_project_memory_kind ON project_memory(kind, id);
'''


class ProjectMemory:
    """Small durable memory separate from the LLM context window.

    It stores compact accepted evidence, objections and failed strategies in the
    workspace ledger. Retrieval is intentionally deterministic and cheap: lexical
    overlap + recency. A future embedding index can replace ranking without changing
    the engine contract.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=30000')
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def remember(self, kind: str, summary: str, *, task_id: str | None = None, payload: dict[str, Any] | None = None) -> int:
        cur = self.conn.execute(
            'INSERT INTO project_memory(ts,kind,task_id,summary,payload_json) VALUES(?,?,?,?,?)',
            (self._now(), str(kind), task_id, str(summary)[:12000], json.dumps(payload or {}, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {t for t in re.findall(r'[a-zA-Z0-9_]{3,}', text.lower()) if len(t) >= 3}

    def relevant(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            'SELECT id,ts,kind,task_id,summary,payload_json FROM project_memory ORDER BY id DESC LIMIT 400'
        ).fetchall()
        terms = self._terms(query)
        scored: list[tuple[float, sqlite3.Row]] = []
        for rank, row in enumerate(rows):
            hay = self._terms(str(row['summary']))
            overlap = len(terms & hay)
            # Always retain some recent memory; reward lexical relevance strongly.
            score = overlap * 10.0 + max(0.0, 4.0 - rank / 100.0)
            if overlap or rank < max(8, limit):
                scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for _, row in scored[:max(1, int(limit))]:
            item = dict(row)
            try:
                item['payload'] = json.loads(item.pop('payload_json') or '{}')
            except Exception:
                item['payload'] = {}
                item.pop('payload_json', None)
            out.append(item)
        return out

    def latest(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            'SELECT id,ts,kind,task_id,summary,payload_json FROM project_memory ORDER BY id DESC LIMIT ?',
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item['payload'] = json.loads(item.pop('payload_json') or '{}')
            except Exception:
                item['payload'] = {}
                item.pop('payload_json', None)
            out.append(item)
        return out

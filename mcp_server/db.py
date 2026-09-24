"""
mcp_server.db
===============

Слой хранения на SQLite (сырой `sqlite3`, стиль `agents_core.db`):
`scheduled_tools` и `scheduled_tool_runs`. Явно перечисленные колонки, а не
"SELECT *" — та же причина, что и в AgentsCore: схема видна в одном месте,
числовые колонки сохраняют affinity.

`_ensure_column()` — та же простая защитная миграция, что и в
`agents_core.db`: недостающие колонки добавляются через
`ALTER TABLE ... ADD COLUMN` при старте, без потери данных.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from typing import Any, Dict, Iterator, List, Optional

from .models import ScheduledTool, ScheduledToolRun

_SCHEDULED_TOOL_COLUMNS = [
    ("name", "TEXT NOT NULL"),
    ("description", "TEXT NOT NULL DEFAULT ''"),
    ("action", "TEXT NOT NULL"),
    ("schedule", "TEXT NOT NULL"),
    ("params_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("input_schema_json", "TEXT"),
    ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("created_at", "INTEGER NOT NULL"),
    ("last_run_at", "INTEGER"),
    ("next_run_at", "INTEGER"),
]


class Database:
    def __init__(self, path: str):
        self.path = path
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, name: str, sql_type: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def _init_schema(self) -> None:
        cols_sql = ",\n".join(f"{name} {sql_type}" for name, sql_type in _SCHEDULED_TOOL_COLUMNS)
        with self._connect() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS scheduled_tools (
                    id TEXT PRIMARY KEY,
                    {cols_sql}
                );

                CREATE TABLE IF NOT EXISTS scheduled_tool_runs (
                    id TEXT PRIMARY KEY,
                    tool_id TEXT NOT NULL REFERENCES scheduled_tools(id) ON DELETE CASCADE,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'running',
                    result_json TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_runs_tool_id ON scheduled_tool_runs(tool_id);
                """
            )
            for name, sql_type in _SCHEDULED_TOOL_COLUMNS:
                self._ensure_column(conn, "scheduled_tools", name, sql_type)

    # ---- scheduled_tools ---------------------------------------------------

    @staticmethod
    def _row_to_tool(row: sqlite3.Row) -> ScheduledTool:
        return ScheduledTool(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            action=row["action"],
            schedule=row["schedule"],
            params=json.loads(row["params_json"]) if row["params_json"] else {},
            input_schema=json.loads(row["input_schema_json"]) if row["input_schema_json"] else None,
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
        )

    def create_scheduled_tool(self, tool: ScheduledTool) -> ScheduledTool:
        tool_id = tool.id or str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tools (
                    id, name, description, action, schedule, params_json,
                    input_schema_json, enabled, created_at, last_run_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_id, tool.name, tool.description, tool.action, tool.schedule,
                    json.dumps(tool.params, ensure_ascii=False),
                    json.dumps(tool.input_schema, ensure_ascii=False) if tool.input_schema is not None else None,
                    int(tool.enabled), tool.created_at, tool.last_run_at, tool.next_run_at,
                ),
            )
        return self.get_scheduled_tool(tool_id)  # type: ignore[return-value]

    def get_scheduled_tool(self, tool_id: str) -> Optional[ScheduledTool]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tools WHERE id = ?", (tool_id,)).fetchone()
        return self._row_to_tool(row) if row else None

    def list_scheduled_tools(self, enabled: Optional[bool] = None) -> List[ScheduledTool]:
        query = "SELECT * FROM scheduled_tools"
        params: tuple = ()
        if enabled is not None:
            query += " WHERE enabled = ?"
            params = (int(enabled),)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_tool(row) for row in rows]

    def update_scheduled_tool(self, tool_id: str, patch: Dict[str, Any]) -> Optional[ScheduledTool]:
        if not patch:
            return self.get_scheduled_tool(tool_id)
        column_map = {
            "name": "name", "description": "description", "action": "action",
            "schedule": "schedule", "enabled": "enabled", "last_run_at": "last_run_at",
            "next_run_at": "next_run_at",
        }
        set_clauses: List[str] = []
        values: List[Any] = []
        for key, value in patch.items():
            if key == "params":
                set_clauses.append("params_json = ?")
                values.append(json.dumps(value, ensure_ascii=False))
            elif key == "input_schema":
                set_clauses.append("input_schema_json = ?")
                values.append(json.dumps(value, ensure_ascii=False) if value is not None else None)
            elif key == "enabled":
                set_clauses.append("enabled = ?")
                values.append(int(value))
            elif key in column_map:
                set_clauses.append(f"{column_map[key]} = ?")
                values.append(value)
        if not set_clauses:
            return self.get_scheduled_tool(tool_id)
        values.append(tool_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE scheduled_tools SET {', '.join(set_clauses)} WHERE id = ?", values)
        return self.get_scheduled_tool(tool_id)

    def delete_scheduled_tool(self, tool_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM scheduled_tools WHERE id = ?", (tool_id,))

    # ---- scheduled_tool_runs -------------------------------------------------

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> ScheduledToolRun:
        return ScheduledToolRun(
            id=row["id"],
            tool_id=row["tool_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )

    def create_run(self, run: ScheduledToolRun) -> ScheduledToolRun:
        run_id = run.id or str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tool_runs (id, tool_id, started_at, finished_at, status, result_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, run.tool_id, run.started_at, run.finished_at, run.status,
                    json.dumps(run.result, ensure_ascii=False) if run.result is not None else None,
                    run.error,
                ),
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> Optional[ScheduledToolRun]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tool_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def finish_run(self, run_id: str, *, finished_at: int, status: str, result: Any = None, error: Optional[str] = None) -> Optional[ScheduledToolRun]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_tool_runs SET finished_at = ?, status = ?, result_json = ?, error = ? WHERE id = ?",
                (
                    finished_at, status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error, run_id,
                ),
            )
        return self.get_run(run_id)

    def list_runs(self, tool_id: str, limit: int = 50) -> List[ScheduledToolRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tool_runs WHERE tool_id = ? ORDER BY started_at DESC LIMIT ?",
                (tool_id, limit),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

"""
mcp_server.store
===================

`SchedulerStore` — бизнес-логика поверх `Database` (стиль
`agents_core.repository.Repository`, только сильно меньше): валидация
(action/schedule), пересчёт `next_run_at`, регистрация запусков. И
MCP-инструменты (`server.py`), и REST API для AgentsApp (`api.py`), и сам
планировщик (`scheduler.py`) обращаются ТОЛЬКО сюда, не в `db.py` напрямую —
единая точка, где нельзя случайно сохранить невалидную задачу.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .cron import ScheduleError, compute_next_run, validate_schedule
from .db import Database
from .models import ACTION_KINDS, ScheduledTool, ScheduledToolRun


class StoreError(Exception):
    """Базовый класс ошибок этого модуля."""


class NotFoundError(StoreError):
    pass


class ValidationError(StoreError):
    pass


def _now() -> int:
    return int(time.time())


class SchedulerStore:
    def __init__(self, db: Database):
        self._db = db

    # ---- scheduled_tools ---------------------------------------------------

    def _validate(self, name: str, action: str, schedule: str) -> None:
        if not name or not name.strip():
            raise ValidationError("Название задачи не может быть пустым")
        if action not in ACTION_KINDS:
            raise ValidationError(f"Неизвестное действие '{action}': допустимо {ACTION_KINDS}")
        try:
            validate_schedule(schedule)
        except ScheduleError as exc:
            raise ValidationError(str(exc)) from exc

    def register_tool(
        self, name: str, action: str, schedule: str, *,
        description: str = "", params: Optional[Dict[str, Any]] = None,
        input_schema: Optional[Dict[str, Any]] = None, enabled: bool = True,
    ) -> ScheduledTool:
        self._validate(name, action, schedule)
        now = _now()
        next_run_at = None
        if enabled:
            next_run_at = int(compute_next_run(schedule, datetime.fromtimestamp(now, tz=timezone.utc)).timestamp())
        tool = ScheduledTool(
            id=str(uuid.uuid4()), name=name.strip(), description=description or "",
            action=action, schedule=schedule, params=params or {}, input_schema=input_schema,
            enabled=enabled, created_at=now, last_run_at=None, next_run_at=next_run_at,
        )
        return self._db.create_scheduled_tool(tool)

    def get_tool(self, tool_id: str) -> ScheduledTool:
        tool = self._db.get_scheduled_tool(tool_id)
        if tool is None:
            raise NotFoundError(f"Периодическая задача не найдена: {tool_id}")
        return tool

    def list_tools(self, enabled: Optional[bool] = None) -> List[ScheduledTool]:
        return self._db.list_scheduled_tools(enabled=enabled)

    def update_tool(self, tool_id: str, patch: Dict[str, Any]) -> ScheduledTool:
        current = self.get_tool(tool_id)
        name = patch.get("name", current.name)
        action = patch.get("action", current.action)
        schedule = patch.get("schedule", current.schedule)
        enabled = patch.get("enabled", current.enabled)
        self._validate(name, action, schedule)

        effective_patch = dict(patch)
        # Расписание/включённость поменялись (или задача просто снова
        # enabled) -> next_run_at нужно пересчитать от текущего момента;
        # выключенной задаче next_run_at не нужен (не будет запущена).
        schedule_changed = "schedule" in patch and patch["schedule"] != current.schedule
        enabled_changed = "enabled" in patch and patch["enabled"] != current.enabled
        if not enabled:
            effective_patch["next_run_at"] = None
        elif schedule_changed or enabled_changed or current.next_run_at is None:
            effective_patch["next_run_at"] = int(
                compute_next_run(schedule, datetime.now(timezone.utc)).timestamp()
            )
        updated = self._db.update_scheduled_tool(tool_id, effective_patch)
        assert updated is not None
        return updated

    def delete_tool(self, tool_id: str) -> None:
        self.get_tool(tool_id)  # 404, если не существует
        self._db.delete_scheduled_tool(tool_id)

    def advance_next_run(self, tool_id: str, *, after: Optional[datetime] = None) -> ScheduledTool:
        """Вызывается планировщиком (`scheduler.py`) сразу после срабатывания
        задачи: обновляет `last_run_at`=сейчас и пересчитывает `next_run_at`
        от момента срабатывания."""
        tool = self.get_tool(tool_id)
        now = _now()
        base = after or datetime.now(timezone.utc)
        next_run_at = int(compute_next_run(tool.schedule, base).timestamp()) if tool.enabled else None
        updated = self._db.update_scheduled_tool(tool_id, {"last_run_at": now, "next_run_at": next_run_at})
        assert updated is not None
        return updated

    # ---- scheduled_tool_runs -------------------------------------------------

    def start_run(self, tool_id: str) -> ScheduledToolRun:
        self.get_tool(tool_id)  # 404, если не существует
        run = ScheduledToolRun(id=str(uuid.uuid4()), tool_id=tool_id, started_at=_now(), status="running")
        return self._db.create_run(run)

    def finish_run(self, run_id: str, *, ok: bool, result: Any = None, error: Optional[str] = None) -> ScheduledToolRun:
        finished = self._db.finish_run(
            run_id, finished_at=_now(), status="success" if ok else "error", result=result, error=error,
        )
        if finished is None:
            raise NotFoundError(f"Запуск не найден: {run_id}")
        return finished

    def list_runs(self, tool_id: str, limit: int = 50) -> List[ScheduledToolRun]:
        self.get_tool(tool_id)  # 404, если не существует
        return self._db.list_runs(tool_id, limit=limit)

    def run_result(self, tool_id: str, data: Any) -> Dict[str, Any]:
        """`save_result(task_id, data)` MCP-инструмент из ТЗ — сохраняет
        данные ПОСЛЕДНЕГО запуска этой задачи (последняя запись
        `scheduled_tool_runs`, ещё без `finished_at`, если она есть — иначе
        заводит отдельную завершённую запись, чтобы данные не потерялись,
        если инструмент вызван вне обычного цикла планировщика)."""
        self.get_tool(tool_id)
        runs = self._db.list_runs(tool_id, limit=1)
        if runs and runs[0].finished_at is None:
            finished = self.finish_run(runs[0].id, ok=True, result=data)
        else:
            run = self.start_run(tool_id)
            finished = self.finish_run(run.id, ok=True, result=data)
        return {"tool_id": tool_id, "run_id": finished.id, "saved": True}

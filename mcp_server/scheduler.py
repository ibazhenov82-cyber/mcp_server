"""
mcp_server.scheduler
=======================

`SchedulerService` — обёртка над `apscheduler.schedulers.asyncio.
AsyncIOScheduler` (ОДИН инстанс на процесс — см. README, "не запускать
несколько реплик этого сервиса", иначе одна и та же периодическая задача
выполнится N раз параллельно). При старте перечитывает `scheduled_tools` из
`SchedulerStore` и регистрирует задачи заново (идемпотентно — job id всегда
равен `ScheduledTool.id`, повторная регистрация просто заменяет).

`apscheduler` — импортируется ЛЕНИВО, только внутри методов, которые его
реально используют (`start`/`_trigger_for`) — модуль в целом (и, значит,
всё остальное: `store.py`/`git_hosts/*`/`actions.py`, тесты) не должен
падать при импорте в окружении, где `apscheduler` не установлен (см.
README, раздел "Ограничения окружения разработки"; в проде он в
requirements.txt и обязателен)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .actions import execute_action
from .cron import CronSchedule, DailySchedule, EverySchedule, parse_schedule
from .models import ScheduledTool
from .store import NotFoundError, SchedulerStore

_logger = logging.getLogger("mcp_server.scheduler")


class SchedulerNotAvailableError(RuntimeError):
    """`apscheduler` не установлен в текущем окружении."""


def _trigger_for(schedule: str) -> Any:
    """Строит `apscheduler`-триггер из той же строки расписания, что
    понимает `cron.py` (см. его докстринг про формат) — используется
    ТОЛЬКО здесь, для реальной регистрации задачи; `cron.compute_next_run`
    остаётся независимым источником `next_run_at` для REST/тестов."""
    try:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as exc:  # pragma: no cover - недоступно в песочнице разработки
        raise SchedulerNotAvailableError("Пакет 'apscheduler' не установлен") from exc

    parsed = parse_schedule(schedule)
    if isinstance(parsed, EverySchedule):
        return IntervalTrigger(seconds=parsed.seconds)
    if isinstance(parsed, DailySchedule):
        return CronTrigger(hour=parsed.hour, minute=parsed.minute)
    assert isinstance(parsed, CronSchedule)
    return CronTrigger.from_crontab(parsed.raw)


class SchedulerService:
    def __init__(self, store: SchedulerStore):
        self._store = store
        self._scheduler: Optional[Any] = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        """Создаёт `AsyncIOScheduler`, регистрирует все включённые задачи из
        БД и запускает его. Вызывается ОДИН раз при старте приложения (см.
        `app.py`, lifespan) — повторный вызов на уже запущенном планировщике
        является ошибкой программиста (roll your own idempotency guard,
        если вам это когда-нибудь понадобится)."""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError as exc:  # pragma: no cover - недоступно в песочнице разработки
            raise SchedulerNotAvailableError("Пакет 'apscheduler' не установлен") from exc
        self._scheduler = AsyncIOScheduler()
        for tool in self._store.list_tools(enabled=True):
            self._add_job(tool)
        self._scheduler.start()
        _logger.info("Планировщик запущен, задач зарегистрировано: %d", len(self._store.list_tools(enabled=True)))

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def _add_job(self, tool: ScheduledTool) -> None:
        assert self._scheduler is not None
        self._scheduler.add_job(
            self._run_tool, trigger=_trigger_for(tool.schedule), id=tool.id,
            replace_existing=True, kwargs={"tool_id": tool.id}, misfire_grace_time=60,
        )

    def sync_job(self, tool: ScheduledTool) -> None:
        """Вызывается после register/update через REST/MCP — держит набор
        job'ов APScheduler в согласии с БД без перезапуска процесса."""
        if self._scheduler is None:
            return
        if tool.enabled:
            self._add_job(tool)
        else:
            self.remove_job(tool.id)

    def remove_job(self, tool_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(tool_id)
        except Exception:  # apscheduler.jobstores.base.JobLookupError — задачи и не было
            pass

    async def _run_tool(self, tool_id: str) -> None:
        """Один цикл срабатывания: start_run -> execute_action -> finish_run
        -> advance_next_run. Сбой действия НЕ поднимается наружу (иначе
        APScheduler залогирует голый traceback и, в зависимости от
        misfire-политики, может остановить job) — записывается как
        `status="error"` в историю запусков, это и есть предназначенный
        канал для сбоев отдельных срабатываний."""
        try:
            tool = self._store.get_tool(tool_id)
        except NotFoundError:
            self.remove_job(tool_id)  # задачу удалили, а job ещё был запланирован
            return
        run = self._store.start_run(tool_id)
        try:
            if tool.action not in self._store.allowed_actions:
                # Задача заведена раньше, когда этот вид действия был
                # включён (например, git_pull при MCP_LOCAL_GIT_ENABLED=true),
                # а теперь выключен — не выполняем, а фиксируем причину в истории.
                raise RuntimeError(f"Действие '{tool.action}' выключено настройками MCP-сервера")
            result = await execute_action(tool.action, tool.params)
            self._store.finish_run(run.id, ok=True, result=result)
        except Exception as exc:
            _logger.warning("Периодическая задача %s (%s) завершилась с ошибкой: %s", tool.name, tool_id, exc)
            self._store.finish_run(run.id, ok=False, error=str(exc))
        self._store.advance_next_run(tool_id, after=datetime.now(timezone.utc))

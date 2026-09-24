"""Тесты `mcp_server.scheduler.SchedulerService`. Требуют пакет
`apscheduler` (см. requirements.txt) — недоступен в песочнице разработки
(см. README, "Ограничения окружения разработки"), поэтому весь класс
пропускается, а не падает, если его нет — тот же приём, что
`agents_core/tests/test_api.py` использует для `fastapi`."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    import apscheduler  # noqa: F401

    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

from mcp_server.db import Database
from mcp_server.store import SchedulerStore

if APSCHEDULER_AVAILABLE:
    from mcp_server.scheduler import SchedulerService


def make_store() -> SchedulerStore:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return SchedulerStore(Database(tmp.name))


@unittest.skipUnless(APSCHEDULER_AVAILABLE, "apscheduler не установлен в этом окружении")
class SchedulerServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = make_store()

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    async def test_start_registers_only_enabled_tools(self):
        enabled = self.store.register_tool("On", "http_fetch", "every:5m", params={"url": "https://a"})
        self.store.register_tool("Off", "http_fetch", "every:5m", enabled=False)
        service = SchedulerService(self.store)
        try:
            service.start()
            job_ids = {job.id for job in service._scheduler.get_jobs()}
            self.assertEqual(job_ids, {enabled.id})
        finally:
            service.stop()

    async def test_sync_job_adds_job_for_newly_enabled_tool(self):
        service = SchedulerService(self.store)
        service.start()
        try:
            tool = self.store.register_tool("New", "http_fetch", "every:5m", enabled=False)
            self.assertEqual(service._scheduler.get_jobs(), [])
            enabled_tool = self.store.update_tool(tool.id, {"enabled": True})
            service.sync_job(enabled_tool)
            self.assertEqual({j.id for j in service._scheduler.get_jobs()}, {tool.id})
        finally:
            service.stop()

    async def test_sync_job_removes_job_for_disabled_tool(self):
        tool = self.store.register_tool("T", "http_fetch", "every:5m")
        service = SchedulerService(self.store)
        service.start()
        try:
            disabled = self.store.update_tool(tool.id, {"enabled": False})
            service.sync_job(disabled)
            self.assertEqual(service._scheduler.get_jobs(), [])
        finally:
            service.stop()

    async def test_run_tool_records_success_and_advances_next_run(self):
        tool = self.store.register_tool("T", "http_fetch", "every:5m", params={"url": "https://a"})
        service = SchedulerService(self.store)
        with patch("mcp_server.scheduler.execute_action", AsyncMock(return_value={"ok": True})):
            await service._run_tool(tool.id)
        runs = self.store.list_runs(tool.id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "success")
        updated = self.store.get_tool(tool.id)
        self.assertIsNotNone(updated.last_run_at)
        self.assertGreater(updated.next_run_at, tool.next_run_at - 1)

    async def test_run_tool_records_failure_without_raising(self):
        tool = self.store.register_tool("T", "http_fetch", "every:5m", params={"url": "https://a"})
        service = SchedulerService(self.store)
        with patch("mcp_server.scheduler.execute_action", AsyncMock(side_effect=RuntimeError("boom"))):
            await service._run_tool(tool.id)  # не должно поднять исключение
        runs = self.store.list_runs(tool.id)
        self.assertEqual(runs[0].status, "error")
        self.assertIn("boom", runs[0].error)

    async def test_run_tool_for_deleted_tool_is_a_noop(self):
        service = SchedulerService(self.store)
        await service._run_tool("nonexistent-id")  # не должно поднять исключение


if __name__ == "__main__":
    unittest.main()

"""Тесты `mcp_server.db` — сырой слой SQLite, полностью офлайн."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp_server.db import Database
from mcp_server.models import ScheduledTool, ScheduledToolRun


def make_db() -> Database:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Database(tmp.name)


class ScheduledToolCrudTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = make_db()

    def tearDown(self) -> None:
        Path(self.db.path).unlink(missing_ok=True)

    def _tool(self, **overrides) -> ScheduledTool:
        base = dict(
            id="", name="Poll releases", description="", action="git_host_poll",
            schedule="every:5m", params={"host": "github"}, input_schema=None,
            enabled=True, created_at=1000, last_run_at=None, next_run_at=1300,
        )
        base.update(overrides)
        return ScheduledTool(**base)

    def test_create_and_get_roundtrip(self):
        created = self.db.create_scheduled_tool(self._tool())
        fetched = self.db.get_scheduled_tool(created.id)
        self.assertEqual(fetched.name, "Poll releases")
        self.assertEqual(fetched.params, {"host": "github"})
        self.assertTrue(fetched.enabled)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.db.get_scheduled_tool("nope"))

    def test_list_filters_by_enabled(self):
        self.db.create_scheduled_tool(self._tool(name="On", enabled=True))
        self.db.create_scheduled_tool(self._tool(name="Off", enabled=False))
        self.assertEqual({t.name for t in self.db.list_scheduled_tools(enabled=True)}, {"On"})
        self.assertEqual({t.name for t in self.db.list_scheduled_tools(enabled=False)}, {"Off"})
        self.assertEqual({t.name for t in self.db.list_scheduled_tools()}, {"On", "Off"})

    def test_list_ordered_by_created_at(self):
        self.db.create_scheduled_tool(self._tool(name="Second", created_at=2000))
        self.db.create_scheduled_tool(self._tool(name="First", created_at=1000))
        names = [t.name for t in self.db.list_scheduled_tools()]
        self.assertEqual(names, ["First", "Second"])

    def test_update_patch_partial_fields(self):
        created = self.db.create_scheduled_tool(self._tool())
        updated = self.db.update_scheduled_tool(created.id, {"enabled": False, "next_run_at": None})
        self.assertFalse(updated.enabled)
        self.assertIsNone(updated.next_run_at)
        self.assertEqual(updated.name, "Poll releases")  # не тронуто

    def test_update_params_json(self):
        created = self.db.create_scheduled_tool(self._tool())
        updated = self.db.update_scheduled_tool(created.id, {"params": {"owner": "acme"}})
        self.assertEqual(updated.params, {"owner": "acme"})

    def test_delete_removes_tool_and_cascades_runs(self):
        created = self.db.create_scheduled_tool(self._tool())
        run = self.db.create_run(ScheduledToolRun(id="", tool_id=created.id, started_at=1000))
        self.db.delete_scheduled_tool(created.id)
        self.assertIsNone(self.db.get_scheduled_tool(created.id))
        self.assertIsNone(self.db.get_run(run.id))  # ON DELETE CASCADE


class ScheduledToolRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = make_db()
        self.tool = self.db.create_scheduled_tool(ScheduledTool(
            id="", name="T", description="", action="http_fetch", schedule="every:1h",
            params={}, input_schema=None, enabled=True, created_at=1000,
        ))

    def tearDown(self) -> None:
        Path(self.db.path).unlink(missing_ok=True)

    def test_create_run_starts_as_running(self):
        run = self.db.create_run(ScheduledToolRun(id="", tool_id=self.tool.id, started_at=1000))
        self.assertEqual(run.status, "running")
        self.assertIsNone(run.finished_at)

    def test_finish_run_sets_status_and_result(self):
        run = self.db.create_run(ScheduledToolRun(id="", tool_id=self.tool.id, started_at=1000))
        finished = self.db.finish_run(run.id, finished_at=2000, status="success", result={"ok": True})
        self.assertEqual(finished.status, "success")
        self.assertEqual(finished.result, {"ok": True})
        self.assertEqual(finished.finished_at, 2000)

    def test_finish_run_with_error(self):
        run = self.db.create_run(ScheduledToolRun(id="", tool_id=self.tool.id, started_at=1000))
        finished = self.db.finish_run(run.id, finished_at=2000, status="error", error="boom")
        self.assertEqual(finished.status, "error")
        self.assertEqual(finished.error, "boom")

    def test_list_runs_ordered_newest_first_and_limited(self):
        for i in range(5):
            self.db.create_run(ScheduledToolRun(id="", tool_id=self.tool.id, started_at=1000 + i))
        runs = self.db.list_runs(self.tool.id, limit=3)
        self.assertEqual(len(runs), 3)
        self.assertEqual([r.started_at for r in runs], [1004, 1003, 1002])


if __name__ == "__main__":
    unittest.main()

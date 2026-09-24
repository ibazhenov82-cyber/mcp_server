"""Тесты `mcp_server.store.SchedulerStore` — валидация, пересчёт
`next_run_at`, регистрация запусков. Полностью офлайн (sqlite во временном
файле)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mcp_server.db import Database
from mcp_server.store import NotFoundError, SchedulerStore, ValidationError


def make_store() -> SchedulerStore:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return SchedulerStore(Database(tmp.name))


class RegisterToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    def test_register_computes_next_run_at(self):
        tool = self.store.register_tool("Poll", "git_host_poll", "every:5m", params={"host": "github"})
        self.assertIsNotNone(tool.next_run_at)
        self.assertGreater(tool.next_run_at, tool.created_at)

    def test_register_disabled_has_no_next_run_at(self):
        tool = self.store.register_tool("Poll", "git_host_poll", "every:5m", enabled=False)
        self.assertIsNone(tool.next_run_at)

    def test_register_rejects_empty_name(self):
        with self.assertRaises(ValidationError):
            self.store.register_tool("  ", "http_fetch", "every:5m")

    def test_register_rejects_unknown_action(self):
        with self.assertRaises(ValidationError):
            self.store.register_tool("T", "delete_everything", "every:5m")

    def test_register_rejects_invalid_schedule(self):
        with self.assertRaises(ValidationError):
            self.store.register_tool("T", "http_fetch", "not a schedule")

    def test_register_persists_params_and_input_schema(self):
        tool = self.store.register_tool(
            "T", "http_fetch", "every:1h",
            params={"url": "https://example.com"}, input_schema={"type": "object"},
        )
        reloaded = self.store.get_tool(tool.id)
        self.assertEqual(reloaded.params, {"url": "https://example.com"})
        self.assertEqual(reloaded.input_schema, {"type": "object"})


class AllowedActionsTests(unittest.TestCase):
    """Виды задач, выключенные настройками сервера (см. `features.py`), не
    заводятся: без локального Git нет git_pull, без планировщика — ничего."""

    def _store(self, allowed):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: os.unlink(tmp.name))
        return SchedulerStore(Database(tmp.name), allowed_actions=allowed)

    def test_git_pull_rejected_when_not_allowed(self):
        store = self._store(["http_fetch", "git_host_poll"])
        with self.assertRaises(ValidationError) as ctx:
            store.register_tool("Pull", "git_pull", "every:5m", params={"repo_path": "/tmp"})
        self.assertIn("выключено", str(ctx.exception))

    def test_allowed_action_still_registers(self):
        store = self._store(["http_fetch"])
        tool = store.register_tool("Fetch", "http_fetch", "every:5m", params={"url": "https://a"})
        self.assertEqual(tool.action, "http_fetch")

    def test_default_allows_all_action_kinds(self):
        store = self._store(None)
        store.register_tool("Pull", "git_pull", "every:5m", params={"repo_path": "/tmp"})


class GetListDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    def test_get_missing_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.get_tool("nope")

    def test_list_all(self):
        self.store.register_tool("A", "http_fetch", "every:5m")
        self.store.register_tool("B", "http_fetch", "every:5m")
        self.assertEqual({t.name for t in self.store.list_tools()}, {"A", "B"})

    def test_delete_missing_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.delete_tool("nope")

    def test_delete_removes_tool(self):
        tool = self.store.register_tool("A", "http_fetch", "every:5m")
        self.store.delete_tool(tool.id)
        with self.assertRaises(NotFoundError):
            self.store.get_tool(tool.id)


class UpdateToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.tool = self.store.register_tool("A", "http_fetch", "every:5m", params={"url": "https://a"})

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    def test_partial_rename_keeps_other_fields(self):
        updated = self.store.update_tool(self.tool.id, {"name": "Renamed"})
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.action, "http_fetch")

    def test_changing_schedule_recomputes_next_run_at(self):
        before = self.tool.next_run_at
        updated = self.store.update_tool(self.tool.id, {"schedule": "every:1h"})
        self.assertNotEqual(updated.next_run_at, before)

    def test_disabling_clears_next_run_at(self):
        updated = self.store.update_tool(self.tool.id, {"enabled": False})
        self.assertIsNone(updated.next_run_at)

    def test_reenabling_recomputes_next_run_at(self):
        self.store.update_tool(self.tool.id, {"enabled": False})
        updated = self.store.update_tool(self.tool.id, {"enabled": True})
        self.assertIsNotNone(updated.next_run_at)

    def test_update_rejects_invalid_schedule(self):
        with self.assertRaises(ValidationError):
            self.store.update_tool(self.tool.id, {"schedule": "garbage"})

    def test_update_rejects_unknown_action(self):
        with self.assertRaises(ValidationError):
            self.store.update_tool(self.tool.id, {"action": "delete_everything"})


class RunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.tool = self.store.register_tool("A", "http_fetch", "every:5m")

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    def test_start_then_finish_success(self):
        run = self.store.start_run(self.tool.id)
        self.assertEqual(run.status, "running")
        finished = self.store.finish_run(run.id, ok=True, result={"n": 3})
        self.assertEqual(finished.status, "success")
        self.assertEqual(finished.result, {"n": 3})

    def test_finish_error(self):
        run = self.store.start_run(self.tool.id)
        finished = self.store.finish_run(run.id, ok=False, error="timeout")
        self.assertEqual(finished.status, "error")
        self.assertEqual(finished.error, "timeout")

    def test_start_run_for_missing_tool_raises(self):
        with self.assertRaises(NotFoundError):
            self.store.start_run("nope")

    def test_list_runs_for_missing_tool_raises(self):
        with self.assertRaises(NotFoundError):
            self.store.list_runs("nope")

    def test_advance_next_run_updates_last_run_and_next_run(self):
        before_next = self.tool.next_run_at
        updated = self.store.advance_next_run(self.tool.id)
        self.assertIsNotNone(updated.last_run_at)
        self.assertIsNotNone(updated.next_run_at)
        self.assertGreaterEqual(updated.next_run_at, before_next)

    def test_advance_next_run_for_disabled_tool_leaves_next_run_none(self):
        self.store.update_tool(self.tool.id, {"enabled": False})
        updated = self.store.advance_next_run(self.tool.id)
        self.assertIsNone(updated.next_run_at)

    def test_run_result_saves_into_open_run(self):
        run = self.store.start_run(self.tool.id)
        result = self.store.run_result(self.tool.id, {"count": 7})
        self.assertEqual(result["run_id"], run.id)
        saved = self.store._db.get_run(run.id)
        self.assertEqual(saved.status, "success")
        self.assertEqual(saved.result, {"count": 7})

    def test_run_result_without_open_run_creates_one(self):
        result = self.store.run_result(self.tool.id, {"count": 1})
        runs = self.store.list_runs(self.tool.id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].id, result["run_id"])

    def test_run_result_for_missing_tool_raises(self):
        with self.assertRaises(NotFoundError):
            self.store.run_result("nope", {})


if __name__ == "__main__":
    unittest.main()

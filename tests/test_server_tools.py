"""Тесты `mcp_server.server.build_mcp_server` — вызывает MCP-инструменты
ТОЧНО так же, как это будет делать AgentsCore-клиент (`FastMCP.call_tool`),
без реального Streamable HTTP транспорта (`mcp` пакет для этого не нужен —
`FastMCP` работает в процессе). git_host_* тесты подменяют `get_provider`
фиктивным провайдером — сетевые вызовы уже покрыты `test_git_hosts.py`."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp_server.db import Database
from mcp_server.server import build_mcp_server
from mcp_server.store import SchedulerStore


def _tool_text(result) -> dict:
    """`FastMCP.call_tool()` возвращает `(content_blocks, structured)` —
    structured (второй элемент) уже разобранный dict/list Python-значений,
    им и пользуемся в тестах вместо парсинга text-контента."""
    _, structured = result
    return structured.get("result", structured)


def make_store() -> SchedulerStore:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return SchedulerStore(Database(tmp.name))


class RegisterListCancelToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.mcp = build_mcp_server(self.store)

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    async def test_register_then_list_then_cancel(self):
        registered = _tool_text(await self.mcp.call_tool("register_scheduled_tool", {
            "name": "Poll releases", "schedule": "every:1h", "action": "git_host_poll",
            "params": {"host": "github", "owner": "acme", "repo": "widgets", "resource": "releases"},
        }))
        self.assertEqual(registered["name"], "Poll releases")
        self.assertIsNotNone(registered["next_run_at"])

        listed = _tool_text(await self.mcp.call_tool("list_scheduled_tools", {}))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], registered["id"])

        cancelled = _tool_text(await self.mcp.call_tool("cancel_scheduled_tool", {"task_id": registered["id"]}))
        self.assertTrue(cancelled["cancelled"])

        listed_after = _tool_text(await self.mcp.call_tool("list_scheduled_tools", {}))
        self.assertEqual(listed_after, [])

    async def test_list_scheduled_tools_filters_by_status(self):
        await self.mcp.call_tool("register_scheduled_tool", {"name": "On", "schedule": "every:5m", "action": "http_fetch"})
        off = _tool_text(await self.mcp.call_tool("register_scheduled_tool", {"name": "Off", "schedule": "every:5m", "action": "http_fetch"}))
        self.store.update_tool(off["id"], {"enabled": False})

        enabled_only = _tool_text(await self.mcp.call_tool("list_scheduled_tools", {"status": "enabled"}))
        self.assertEqual([t["name"] for t in enabled_only], ["On"])

        disabled_only = _tool_text(await self.mcp.call_tool("list_scheduled_tools", {"status": "disabled"}))
        self.assertEqual([t["name"] for t in disabled_only], ["Off"])

    async def test_register_with_invalid_schedule_returns_error_dict_not_exception(self):
        result = _tool_text(await self.mcp.call_tool("register_scheduled_tool", {
            "name": "Bad", "schedule": "not a schedule", "action": "http_fetch",
        }))
        self.assertIn("error", result)

    async def test_cancel_unknown_task_returns_error_dict(self):
        result = _tool_text(await self.mcp.call_tool("cancel_scheduled_tool", {"task_id": "nope"}))
        self.assertIn("error", result)


class SaveResultToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.mcp = build_mcp_server(self.store)

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    async def test_save_result_persists_into_run_history(self):
        tool = self.store.register_tool("T", "http_fetch", "every:5m")
        saved = _tool_text(await self.mcp.call_tool("save_result", {"task_id": tool.id, "data": {"n": 42}}))
        self.assertTrue(saved["saved"])
        runs = self.store.list_runs(tool.id)
        self.assertEqual(runs[0].result, {"n": 42})

    async def test_save_result_for_unknown_task_returns_error(self):
        result = _tool_text(await self.mcp.call_tool("save_result", {"task_id": "nope", "data": {}}))
        self.assertIn("error", result)


class ExecuteGitCommandToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.mcp = build_mcp_server(self.store)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_path = self._tmpdir.name
        subprocess.run(["git", "init"], cwd=self.repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo_path, check=True, capture_output=True)

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)
        self._tmpdir.cleanup()

    async def test_status_on_clean_repo(self):
        result = _tool_text(await self.mcp.call_tool("execute_git_command", {
            "repo_path": self.repo_path, "command": "status", "args": ["--short"],
        }))
        self.assertEqual(result["returncode"], 0)

    async def test_unknown_subcommand_returns_error_dict(self):
        result = _tool_text(await self.mcp.call_tool("execute_git_command", {
            "repo_path": self.repo_path, "command": "not-a-real-git-command",
        }))
        self.assertIn("error", result)


class GitHostToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.mcp = build_mcp_server(self.store)

    def tearDown(self) -> None:
        Path(self.store._db.path).unlink(missing_ok=True)

    async def test_git_host_list_commits_dispatches_to_provider(self):
        fake_provider = AsyncMock()
        fake_provider.list_commits.return_value = [{"sha": "abc"}]
        with patch("mcp_server.server.get_provider", return_value=fake_provider):
            result = _tool_text(await self.mcp.call_tool("git_host_list_commits", {
                "host": "github", "owner": "acme", "repo": "widgets",
            }))
        self.assertEqual(result, [{"sha": "abc"}])

    async def test_unknown_host_returns_error_without_calling_provider(self):
        with patch("mcp_server.server.get_provider") as get_provider_mock:
            result = _tool_text(await self.mcp.call_tool("git_host_list_commits", {
                "host": "bitbucket", "owner": "acme", "repo": "widgets",
            }))
        self.assertIn("error", result)
        get_provider_mock.assert_not_called()

    async def test_git_host_get_file_dispatches_to_provider(self):
        fake_provider = AsyncMock()
        fake_provider.get_file.return_value = {"path": "README.md", "content": "hi"}
        with patch("mcp_server.server.get_provider", return_value=fake_provider):
            result = _tool_text(await self.mcp.call_tool("git_host_get_file", {
                "host": "gitlab", "owner": "acme", "repo": "widgets", "path": "README.md",
            }))
        self.assertEqual(result["content"], "hi")


if __name__ == "__main__":
    unittest.main()

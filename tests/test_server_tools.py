"""Тесты `mcp_server.server.build_mcp_server` — вызывает MCP-инструменты
напрямую через `FastMCP.call_tool()` (без HTTP)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp_server.features import Features
from mcp_server.server import build_mcp_server


def _tool_text(result) -> dict:
    """`FastMCP.call_tool()` возвращает `(content_blocks, structured)`."""
    _, structured = result
    return structured.get("result", structured)


#: Все включаемые группы — для тестов самих инструментов.
ALL = Features.all_enabled()


class ToolTitlesTests(unittest.IsolatedAsyncioTestCase):
    """У каждого инструмента — краткое русское описание (`title`)."""

    async def test_every_tool_has_russian_title(self):
        tools = await build_mcp_server(features=ALL).list_tools()
        self.assertTrue(tools)
        for tool in tools:
            self.assertTrue(tool.title, f"нет title у {tool.name}")
            self.assertRegex(tool.title, "[А-Яа-яЁё]", f"title у {tool.name} не на русском")

    async def test_every_tool_has_group_in_meta(self):
        groups = {t.name: (t.meta or {}).get("agentscore/group") for t in await build_mcp_server(features=ALL).list_tools()}
        self.assertEqual(groups["git_pull"], "Локальный GIT")
        self.assertEqual(groups["execute_git_command"], "Локальный GIT")
        self.assertEqual(groups["git_host_get_repo"], "GIT API")
        self.assertEqual(groups["http_fetch"], "HTTP-запросы")
        self.assertEqual(groups["duckduckgo_search"], "Интернет поиск")
        self.assertEqual(groups["read_web_page"], "Интернет поиск")
        self.assertEqual(groups["summarize"], "Обработка LLM")
        self.assertEqual(groups["save_to_text_file"], "Работа с файлами")
        self.assertEqual(groups["run_pipeline"], "Пайплайны")
        self.assertTrue(all(groups.values()), groups)


class FeatureGatingTests(unittest.IsolatedAsyncioTestCase):
    """Без соответствующей настройки инструменты группы не попадают в список."""

    LOCAL_GIT = {"execute_git_command", "git_pull"}
    HTTP = {"http_fetch"}
    SCHEDULING = {"register_scheduled_tool", "list_scheduled_tools", "cancel_scheduled_tool", "save_result"}

    async def _names(self, features: Features) -> set:
        return {t.name for t in await build_mcp_server(features=features).list_tools()}

    async def test_nothing_enabled_leaves_only_git_host_tools_and_pipeline(self):
        names = await self._names(Features())
        self.assertTrue(names)
        self.assertTrue(all(n.startswith("git_host_") or n == "run_pipeline" for n in names), names)

    async def test_pipeline_groups_gated(self):
        names = await self._names(Features(web_search=True))
        self.assertTrue({"duckduckgo_search", "read_web_page"} <= names)
        self.assertFalse(names & {"summarize", "save_to_text_file"})
        names = await self._names(Features(llm=True, files=True))
        self.assertTrue({"summarize", "save_to_text_file"} <= names)
        self.assertNotIn("duckduckgo_search", names)

    async def test_local_git_only(self):
        names = await self._names(Features(local_git=True))
        self.assertTrue(self.LOCAL_GIT <= names)
        self.assertFalse(names & self.HTTP)

    async def test_http_fetch_only(self):
        names = await self._names(Features(http_fetch=True))
        self.assertTrue(self.HTTP <= names)
        self.assertFalse(names & self.LOCAL_GIT)

    async def test_no_scheduling_tools_anymore(self):
        # Периодические задачи переехали в scheduler_service.
        self.assertFalse(await self._names(ALL) & self.SCHEDULING)


class GitPullAndHttpFetchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_pull_error_returned_as_dict(self):
        result = _tool_text(await build_mcp_server(features=ALL).call_tool("git_pull", {"repo_path": "/no/such/path"}))
        self.assertIn("error", result)

    async def test_http_fetch_dispatches(self):
        with patch("mcp_server.server.http_fetch_action", AsyncMock(return_value={"status_code": 200})) as fetch:
            result = _tool_text(await build_mcp_server(features=ALL).call_tool("http_fetch", {"url": "https://x.test"}))
        self.assertEqual(result, {"status_code": 200})
        fetch.assert_awaited_once()


class ExecuteGitCommandToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mcp = build_mcp_server(features=ALL)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_path = self._tmpdir.name
        subprocess.run(["git", "init"], cwd=self.repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo_path, check=True, capture_output=True)

    def tearDown(self) -> None:
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
        self.mcp = build_mcp_server(features=ALL)

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

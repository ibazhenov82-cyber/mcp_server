"""Тесты `mcp_server.actions`. `git_pull` — реальный `git` над временными
локальными репозиториями (без сети); `http_fetch` — `httpx.MockTransport`
через monkeypatch `httpx.AsyncClient`; `git_host_poll` — подмена
`get_provider` фиктивным async-провайдером (диспетчеризация по `resource`,
без реальных HTTP-запросов — это уже покрыто `test_git_hosts.py`)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from mcp_server.actions import ActionError, execute_action

_RealAsyncClient = httpx.AsyncClient


def _run_git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class GitPullActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.remote = root / "remote.git"
        self.clone = root / "clone"
        self.remote.mkdir()
        _run_git("init", "--bare", cwd=str(self.remote))

        seed = root / "seed"
        seed.mkdir()
        _run_git("init", cwd=str(seed))
        _run_git("config", "user.email", "test@example.com", cwd=str(seed))
        _run_git("config", "user.name", "Test", cwd=str(seed))
        (seed / "file.txt").write_text("v1")
        _run_git("add", "file.txt", cwd=str(seed))
        _run_git("commit", "-m", "initial", cwd=str(seed))
        _run_git("branch", "-M", "main", cwd=str(seed))
        _run_git("remote", "add", "origin", str(self.remote), cwd=str(seed))
        _run_git("push", "origin", "main", cwd=str(seed))

        _run_git("clone", str(self.remote), str(self.clone), cwd=str(root))
        _run_git("config", "user.email", "test@example.com", cwd=str(self.clone))
        _run_git("config", "user.name", "Test", cwd=str(self.clone))

        # Ещё один коммит в "seed", запушенный в remote — clone должен его
        # подтянуть через git_pull.
        (seed / "file.txt").write_text("v2")
        _run_git("commit", "-am", "second", cwd=str(seed))
        _run_git("push", "origin", "main", cwd=str(seed))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    async def test_pull_fetches_new_commit(self):
        result = await execute_action("git_pull", {"repo_path": str(self.clone), "branch": "main"})
        self.assertEqual(result["returncode"], 0)
        self.assertEqual((self.clone / "file.txt").read_text(), "v2")

    async def test_missing_repo_path_raises(self):
        with self.assertRaises(ActionError):
            await execute_action("git_pull", {})

    async def test_nonexistent_path_raises_action_error(self):
        with self.assertRaises(ActionError):
            await execute_action("git_pull", {"repo_path": "/no/such/path"})


class HttpFetchActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_returns_status_and_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        fake_client = lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
        with patch("mcp_server.actions.httpx.AsyncClient", fake_client):
            result = await execute_action("http_fetch", {"url": "https://example.test/api"})
        self.assertEqual(result["status_code"], 200)
        self.assertIn("ok", result["body"])

    async def test_missing_url_raises(self):
        with self.assertRaises(ActionError):
            await execute_action("http_fetch", {})

    async def test_long_body_is_truncated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="x" * 6000)

        fake_client = lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
        with patch("mcp_server.actions.httpx.AsyncClient", fake_client):
            result = await execute_action("http_fetch", {"url": "https://example.test/big"})
        self.assertIn("обрезано", result["body"])
        self.assertLess(len(result["body"]), 6000)


class GitHostPollActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_commits_to_provider(self):
        fake_provider = AsyncMock()
        fake_provider.list_commits.return_value = [{"sha": "abc"}]
        with patch("mcp_server.actions.get_provider", return_value=fake_provider):
            result = await execute_action("git_host_poll", {
                "host": "github", "owner": "acme", "repo": "widgets", "resource": "commits", "since": "-1d",
            })
        self.assertEqual(result, [{"sha": "abc"}])
        fake_provider.list_commits.assert_awaited_once()

    async def test_dispatches_releases(self):
        fake_provider = AsyncMock()
        fake_provider.list_releases.return_value = [{"tag": "v1"}]
        with patch("mcp_server.actions.get_provider", return_value=fake_provider):
            result = await execute_action("git_host_poll", {
                "host": "gitea", "owner": "acme", "repo": "widgets", "resource": "releases",
            })
        self.assertEqual(result, [{"tag": "v1"}])

    async def test_missing_required_params_raises(self):
        with self.assertRaises(ActionError):
            await execute_action("git_host_poll", {"host": "github"})

    async def test_unknown_resource_raises(self):
        fake_provider = AsyncMock()
        with patch("mcp_server.actions.get_provider", return_value=fake_provider):
            with self.assertRaises(ActionError):
                await execute_action("git_host_poll", {
                    "host": "github", "owner": "a", "repo": "b", "resource": "nonsense",
                })

    async def test_file_resource_requires_path(self):
        fake_provider = AsyncMock()
        with patch("mcp_server.actions.get_provider", return_value=fake_provider):
            with self.assertRaises(ActionError):
                await execute_action("git_host_poll", {
                    "host": "github", "owner": "a", "repo": "b", "resource": "file",
                })


class UnknownActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_action_raises(self):
        with self.assertRaises(ActionError):
            await execute_action("delete_everything", {})


if __name__ == "__main__":
    unittest.main()

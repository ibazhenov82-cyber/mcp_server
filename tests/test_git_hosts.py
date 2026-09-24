"""
Тесты `mcp_server.git_hosts`: нормализация ответов GitHub/GitLab/Gitea,
анонимный доступ vs с токеном, обработка 401/403/404/429 (+backoff),
фильтрация по `since`. `httpx.MockTransport` — без сети и без `respx`
(недоступен в песочнице разработки — см. README)."""

from __future__ import annotations

import unittest

import httpx

from mcp_server.git_hosts.base import GitHostError, parse_since
from mcp_server.git_hosts.gitea import GiteaProvider
from mcp_server.git_hosts.github import GitHubProvider
from mcp_server.git_hosts.gitlab import GitLabProvider


def _client(handler, token=None) -> httpx.AsyncClient:
    # Мимикрирует `git_hosts._client_for`: токен (если задан) кладётся в
    # заголовки самого клиента, а не передаётся отдельно в конструктор
    # провайдера — так же, как в реальном коде.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test", headers=headers)


class ParseSinceTests(unittest.TestCase):
    def test_relative_hours(self):
        from datetime import datetime, timezone
        dt = parse_since("-1h")
        self.assertLess((datetime.now(timezone.utc) - dt).total_seconds() - 3600, 5)

    def test_iso8601_with_z(self):
        dt = parse_since("2026-01-01T00:00:00Z")
        self.assertEqual(dt.year, 2026)
        self.assertIsNotNone(dt.tzinfo)

    def test_none_when_empty(self):
        self.assertIsNone(parse_since(None))
        self.assertIsNone(parse_since(""))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_since("not-a-date")


class GitHubProviderTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, handler, token=None) -> GitHubProvider:
        return GitHubProvider(_client(handler, token=token), "https://example.test", token=token, backoff_seconds=0.001)

    async def test_anonymous_request_has_no_authorization_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=[])

        provider = self._provider(handler, token=None)
        await provider.list_commits("acme", "widgets")
        self.assertIsNone(seen["auth"])

    async def test_token_request_sends_bearer_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=[])

        provider = self._provider(handler, token="ghp_secret")
        await provider.list_commits("acme", "widgets")
        self.assertEqual(seen["auth"], "Bearer ghp_secret")

    async def test_list_commits_normalizes_fields(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {
                    "sha": "abc123",
                    "html_url": "https://github.com/acme/widgets/commit/abc123",
                    "commit": {
                        "message": "Fix bug",
                        "author": {"name": "Alice", "email": "alice@example.com", "date": "2026-01-05T10:00:00Z"},
                    },
                    "author": {"login": "alice"},
                },
            ])

        provider = self._provider(handler)
        commits = await provider.list_commits("acme", "widgets")
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["sha"], "abc123")
        self.assertEqual(commits[0]["message"], "Fix bug")
        self.assertEqual(commits[0]["author"], "Alice")

    async def test_since_filters_out_older_items(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"sha": "new", "html_url": "u", "commit": {"message": "m", "author": {"name": "a", "date": "2026-02-01T00:00:00Z"}}},
                {"sha": "old", "html_url": "u", "commit": {"message": "m", "author": {"name": "a", "date": "2020-01-01T00:00:00Z"}}},
            ])

        provider = self._provider(handler)
        commits = await provider.list_commits("acme", "widgets", since="2025-01-01T00:00:00Z")
        self.assertEqual([c["sha"] for c in commits], ["new"])

    async def test_issues_excludes_pull_requests(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"number": 1, "title": "Real issue", "state": "open", "user": {"login": "bob"},
                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "html_url": "u"},
                {"number": 2, "title": "A PR", "state": "open", "user": {"login": "bob"}, "pull_request": {},
                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "html_url": "u"},
            ])

        provider = self._provider(handler)
        issues = await provider.list_issues("acme", "widgets")
        self.assertEqual([i["number"] for i in issues], [1])

    async def test_get_file_decodes_base64_content(self):
        import base64

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "path": "README.md", "sha": "deadbeef", "size": 11,
                "content": base64.b64encode(b"hello world").decode("ascii"),
            })

        provider = self._provider(handler)
        file = await provider.get_file("acme", "widgets", "README.md")
        self.assertEqual(file["content"], "hello world")

    async def test_404_raises_git_host_error_with_status_code(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        provider = self._provider(handler)
        with self.assertRaises(GitHostError) as ctx:
            await provider.get_repo("acme", "does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("Not Found", str(ctx.exception))

    async def test_401_raises_git_host_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        provider = self._provider(handler, token="bad-token")
        with self.assertRaises(GitHostError) as ctx:
            await provider.get_repo("acme", "widgets")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_403_raises_git_host_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Forbidden"})

        provider = self._provider(handler)
        with self.assertRaises(GitHostError) as ctx:
            await provider.get_repo("acme", "widgets")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_429_retries_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"full_name": "acme/widgets"})

        provider = self._provider(handler)
        repo = await provider.get_repo("acme", "widgets")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(repo["full_name"], "acme/widgets")

    async def test_429_exhausts_retries_and_raises(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"})

        provider = self._provider(handler)
        with self.assertRaises(GitHostError) as ctx:
            await provider.get_repo("acme", "widgets")
        self.assertEqual(calls["n"], provider.max_retries + 1)
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_get_status_reads_rate_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"resources": {"core": {"remaining": 42, "reset": 1234567890}}})

        provider = self._provider(handler, token="tok")
        status = await provider.get_status()
        self.assertEqual(status.rate_limit_remaining, 42)
        self.assertEqual(status.rate_limit_reset_at, 1234567890)
        self.assertTrue(status.configured)


class GitLabProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_pull_requests_maps_state_and_merged_flag(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("state"), "opened")
            return httpx.Response(200, json=[
                {"iid": 5, "title": "Add feature", "state": "opened", "author": {"username": "carol"},
                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z", "web_url": "u"},
            ])

        provider = GitLabProvider(_client(handler), "https://example.test", backoff_seconds=0.001)
        prs = await provider.list_pull_requests("acme", "widgets", state="open")
        self.assertEqual(prs[0]["number"], 5)
        self.assertFalse(prs[0]["merged"])

    async def test_project_id_is_url_encoded(self):
        seen_path = {}

        def handler(request: httpx.Request) -> httpx.Response:
            # httpx нормализует `.path` (decoded) — берём "сырой" путь с
            # диска запроса, чтобы увидеть реальное URL-кодирование "/".
            seen_path["raw"] = request.url.raw_path.decode("ascii")
            return httpx.Response(200, json={"path_with_namespace": "acme/widgets"})

        provider = GitLabProvider(_client(handler), "https://example.test", backoff_seconds=0.001)
        await provider.get_repo("acme", "widgets")
        self.assertIn("acme%2Fwidgets", seen_path["raw"])


class GiteaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_releases_normalizes_fields(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"tag_name": "v1.0", "name": "First release", "created_at": "2026-01-01T00:00:00Z",
                 "published_at": "2026-01-02T00:00:00Z", "draft": False, "prerelease": False, "html_url": "u"},
            ])

        provider = GiteaProvider(_client(handler), "https://example.test", backoff_seconds=0.001)
        releases = await provider.list_releases("acme", "widgets")
        self.assertEqual(releases[0]["tag"], "v1.0")

    async def test_get_status_without_rate_limit_headers_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"version": "1.22.0"})

        provider = GiteaProvider(_client(handler), "https://example.test", backoff_seconds=0.001)
        status = await provider.get_status()
        self.assertIsNone(status.rate_limit_remaining)
        self.assertFalse(status.configured)


if __name__ == "__main__":
    unittest.main()

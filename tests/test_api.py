"""Интеграционные тесты REST API и MCP-эндпоинта через FastAPI TestClient.
Требуют `fastapi` — в песочнице разработки недоступен, классы пропускаются."""

from __future__ import annotations

import unittest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from mcp_server.features import Features

if FASTAPI_AVAILABLE:
    from mcp_server.app import create_app

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi не установлен в этом окружении")
class ApiTests(unittest.TestCase):
    def client(self, features: Features):
        # Контекстный менеджер обязателен: только так запускается lifespan
        # (session_manager MCP-приложения, см. README «Устранение неполадок»).
        return TestClient(create_app(features), base_url="http://localhost:8001")

    def test_status_reports_features(self):
        with self.client(Features(local_git=True)) as client:
            body = client.get("/api/status").json()
        self.assertTrue(body["local_git_enabled"])
        self.assertFalse(body["http_fetch_enabled"])
        self.assertGreater(body["tool_count"], 0)

    def test_tools_list_respects_features(self):
        with self.client(Features()) as client:
            names = {t["name"] for t in client.get("/api/tools").json()}
        self.assertIn("git_host_get_repo", names)
        self.assertNotIn("execute_git_command", names)
        self.assertNotIn("http_fetch", names)

    def test_scheduled_tools_endpoints_removed(self):
        with self.client(Features.all_enabled()) as client:
            self.assertIn(client.get("/api/scheduled-tools").status_code, (404, 405))

    def test_git_hosts_lists_all_three(self):
        with self.client(Features()) as client:
            hosts = {h["host"] for h in client.get("/api/git-hosts").json()}
        self.assertEqual(hosts, {"github", "gitlab", "gitea"})

    def test_status_reports_pipeline_groups(self):
        with self.client(Features(web_search=True, files=True)) as client:
            body = client.get("/api/status").json()
        self.assertTrue(body["web_search_enabled"])
        self.assertTrue(body["files_enabled"])
        self.assertFalse(body["llm_enabled"])

    def test_files_list_and_content(self):
        import tempfile
        from unittest.mock import patch

        from mcp_server.config import MCPConfig

        with tempfile.TemporaryDirectory() as tmp, patch.object(MCPConfig, "FILES_DIR", tmp):
            with self.client(Features(files=True)) as client:
                self.assertEqual(client.get("/api/files").json(), [])
                resp = client.post("/mcp", headers=HEADERS, json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "save_to_text_file", "arguments": {"filename": "r/a.md", "content": "# Отчёт"}},
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                files = client.get("/api/files").json()
                self.assertEqual([f["path"] for f in files], ["r/a.md"])
                body = client.get("/api/files/content", params={"path": "r/a.md"}).json()
                self.assertEqual(body["content"], "# Отчёт")
                self.assertEqual(client.get("/api/files/content", params={"path": "../x.md"}).status_code, 400)
                missing = client.get("/api/files/content", params={"path": "nope.md"})
                self.assertEqual(missing.status_code, 404)
                self.assertIn("error", missing.json())

    def test_files_endpoint_disabled(self):
        with self.client(Features()) as client:
            self.assertEqual(client.get("/api/files").status_code, 404)

    def test_mcp_tools_list_via_real_endpoint(self):
        with self.client(Features(local_git=True)) as client:
            resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                               headers=HEADERS)
        self.assertEqual(resp.status_code, 200, resp.text)
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        self.assertIn("git_pull", names)


if __name__ == "__main__":
    unittest.main()

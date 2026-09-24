"""
Интеграционные тесты REST API (`mcp_server/api.py`) через FastAPI
TestClient. Требует `fastapi` — недоступен в песочнице разработки, весь
класс пропускается (`skipUnless`), как и `AgentsCore/tests/test_api.py`.

Планировщик здесь не запускается (`start_scheduler=False`) — сборка
`create_app_with_store` не требует `apscheduler`, а вся логика
регистрации/патча/удаления задач и так уже покрыта `test_store.py`
напрямую; здесь проверяется только HTTP-слой (коды ответов, форма JSON,
проброс ошибок в {"error": ...})."""

from __future__ import annotations

import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from mcp_server.db import Database
from mcp_server.store import SchedulerStore

if FASTAPI_AVAILABLE:
    from mcp_server.app import create_app_with_store


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi не установлен в этом окружении")
class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SchedulerStore(Database(self.db_path))
        app = create_app_with_store(self.store, start_scheduler=False)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_status_reports_zero_scheduled_initially(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["scheduler_running"])  # start_scheduler=False
        self.assertEqual(body["scheduled_count"], 0)
        self.assertGreater(body["tool_count"], 0)  # 11 MCP-инструментов сервера

    def test_tools_list_includes_action_kinds_as_schedulable(self):
        resp = self.client.get("/api/tools")
        self.assertEqual(resp.status_code, 200)
        names = {t["name"]: t for t in resp.json()}
        self.assertIn("git_pull", names)
        self.assertTrue(names["git_pull"]["schedulable"])
        self.assertIn("execute_git_command", names)
        self.assertFalse(names["execute_git_command"]["schedulable"])
        self.assertNotIn("register_scheduled_tool", names)

    def test_create_list_patch_delete_scheduled_tool(self):
        create_resp = self.client.post(
            "/api/scheduled-tools",
            json={"name": "poll-repo", "action": "git_host_poll", "schedule": "every:1h",
                  "params": {"host": "github", "owner": "acme", "repo": "widgets"}},
        )
        self.assertEqual(create_resp.status_code, 201)
        tool = create_resp.json()
        self.assertTrue(tool["enabled"])
        self.assertIsNotNone(tool["next_run_at"])

        list_resp = self.client.get("/api/scheduled-tools")
        self.assertEqual(len(list_resp.json()), 1)

        patch_resp = self.client.patch(f"/api/scheduled-tools/{tool['id']}", json={"enabled": False})
        self.assertEqual(patch_resp.status_code, 200)
        self.assertFalse(patch_resp.json()["enabled"])
        self.assertIsNone(patch_resp.json()["next_run_at"])

        runs_resp = self.client.get(f"/api/scheduled-tools/{tool['id']}/runs")
        self.assertEqual(runs_resp.status_code, 200)
        self.assertEqual(runs_resp.json(), [])

        delete_resp = self.client.delete(f"/api/scheduled-tools/{tool['id']}")
        self.assertEqual(delete_resp.status_code, 204)
        self.assertEqual(self.client.get("/api/scheduled-tools").json(), [])

    def test_create_scheduled_tool_invalid_schedule_returns_error_key(self):
        resp = self.client.post(
            "/api/scheduled-tools",
            json={"name": "bad", "action": "http_fetch", "schedule": "not-a-schedule",
                  "params": {"url": "https://example.com"}},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_create_scheduled_tool_unknown_action_returns_error_key(self):
        resp = self.client.post(
            "/api/scheduled-tools",
            json={"name": "bad", "action": "delete_everything", "schedule": "every:5m", "params": {}},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_patch_unknown_tool_returns_404(self):
        resp = self.client.patch("/api/scheduled-tools/does-not-exist", json={"enabled": False})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.json())

    def test_delete_unknown_tool_returns_404(self):
        resp = self.client.delete("/api/scheduled-tools/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_git_hosts_lists_all_three_as_unconfigured_by_default(self):
        resp = self.client.get("/api/git-hosts")
        self.assertEqual(resp.status_code, 200)
        hosts = {h["host"]: h for h in resp.json()}
        self.assertEqual(set(hosts), {"github", "gitlab", "gitea"})
        for entry in hosts.values():
            self.assertFalse(entry["configured"])


if __name__ == "__main__":
    unittest.main()

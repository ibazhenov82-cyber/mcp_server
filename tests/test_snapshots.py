"""Тесты «Сохранять ответ инструмента» (`save_tool_result`): первый вызов —
весь список, следующие — только новые и изменившиеся элементы, свои снимки у
каждого чата. Git-хостинг подменён, сеть не нужна."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from mcp_server.features import Features
from mcp_server.files import FileStore
from mcp_server.pipeline_tools import PipelineTools
from mcp_server.results import ResultStore
from mcp_server.server import build_mcp_server
from tests.test_pipeline import FakeLLM
from mcp_server.snapshots import (
    SnapshotError, SnapshotStore, extract_items, item_key, snapshot_filename, tracking_key,
)

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ARGS = {"host": "github", "owner": "octo", "repo": "hello"}


def commit(sha, message="msg"):
    return {"sha": sha, "message": message, "author": "Ann", "date": "2026-09-25T10:00:00Z", "url": f"https://g/{sha}"}


class FakeProvider:
    def __init__(self):
        self.commits = [commit("c3"), commit("c2"), commit("c1")]
        self.prs = [{"number": 7, "title": "PR", "state": "open", "url": "https://g/pr/7"}]
        self.calls = []

    async def list_commits(self, owner, repo, since=None, until=None, branch=None, limit=50):
        self.calls.append(("commits", owner, repo, branch, limit))
        return self.commits[:limit]

    async def list_pull_requests(self, owner, repo, state="all", since=None, limit=50):
        return self.prs[:limit]


class SnapshotUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_items(self):
        self.assertEqual(extract_items([{"a": 1}, "x"]), [{"a": 1}])
        self.assertEqual(extract_items({"results": [{"n": 1}]}), [{"n": 1}])
        with self.assertRaises(SnapshotError):
            extract_items({"error": "404"})
        with self.assertRaises(SnapshotError):
            extract_items({"sha": "x"})

    def test_item_key_and_tracking_key(self):
        self.assertEqual(item_key(commit("abc")), "sha:abc")
        self.assertEqual(item_key({"number": 5, "title": "t"}), "number:5")
        self.assertEqual(item_key({"n": 1, "url": "https://x"}), "url:https://x")
        # Окно (limit, since) не меняет того, что отслеживается.
        self.assertEqual(tracking_key("t", {**ARGS, "limit": 10}), tracking_key("t", {**ARGS, "limit": 100, "since": "-1d"}))
        self.assertNotEqual(tracking_key("t", ARGS), tracking_key("t", {**ARGS, "branch": "dev"}))
        name = snapshot_filename("git_host_list_commits", ARGS, tracking_key("git_host_list_commits", ARGS))
        self.assertTrue(name.startswith("git_host_list_commits_github_octo_hello_"), name)

    def test_first_run_then_delta(self):
        first = self.store.compare_and_save("chat-1", "git_host_list_commits", ARGS, [commit("c2"), commit("c1")])
        self.assertTrue(first["first_run"])
        self.assertEqual(first["new_count"], 2)

        same = self.store.compare_and_save("chat-1", "git_host_list_commits", ARGS, [commit("c2"), commit("c1")])
        self.assertFalse(same["first_run"])
        self.assertFalse(same["has_changes"])
        self.assertEqual(same["new_items"], [])
        self.assertEqual(same["previous_run_at"] is not None, True)

        # Новый коммит; c1 выпал из окна — это не удаление.
        delta = self.store.compare_and_save("chat-1", "git_host_list_commits", ARGS, [commit("c3"), commit("c2")])
        self.assertEqual([i["sha"] for i in delta["new_items"]], ["c3"])
        self.assertEqual(delta["known_items"], 3)
        self.assertEqual(delta["runs"], 3)

        saved = json.loads(Path(self.tmp.name, delta["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual([e["item"]["sha"] for e in saved["items"]], ["c3", "c2", "c1"])
        self.assertEqual(saved["args"], ARGS)

    def test_changed_items_reported(self):
        pr = {"number": 7, "title": "PR", "state": "open"}
        self.store.compare_and_save("c", "git_host_list_pull_requests", ARGS, [pr])
        result = self.store.compare_and_save("c", "git_host_list_pull_requests", ARGS, [{**pr, "state": "closed"}])
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["changed_items"][0]["changed_fields"], ["state"])
        self.assertTrue(result["has_changes"])

    def test_possibly_incomplete_and_reset(self):
        self.store.compare_and_save("c", "t", ARGS, [commit("a")], requested_limit=2)
        full_window = self.store.compare_and_save("c", "t", ARGS, [commit("x"), commit("y")], requested_limit=2)
        self.assertTrue(full_window["possibly_incomplete"])
        again = self.store.compare_and_save("c", "t", ARGS, [commit("x")], reset=True)
        self.assertTrue(again["first_run"])

    def test_chats_have_separate_snapshots(self):
        a = self.store.compare_and_save("chat-a", "t", ARGS, [commit("1")])
        b = self.store.compare_and_save("chat-b", "t", ARGS, [commit("1")])
        self.assertTrue(b["first_run"])
        self.assertNotEqual(a["snapshot"], b["snapshot"])
        self.assertTrue(a["snapshot"].startswith("snapshots/chat-a/"))


class SaveToolResultMcpTests(unittest.TestCase):
    """Через настоящий MCP-эндпоинт — чтобы проверить `_meta` с чатом."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = FakeProvider()
        self.patcher = patch("mcp_server.server.get_provider", lambda host: self.provider)
        self.patcher.start()
        self.llm = FakeLLM()
        tools = PipelineTools(
            ResultStore(os.path.join(self.tmp.name, "data")), files=FileStore(os.path.join(self.tmp.name, "files")),
            llm=self.llm,
        )
        mcp = build_mcp_server(Features(files=True, llm=True), pipeline_tools=tools)
        self.files = tools.files
        self.client = TestClient(mcp.streamable_http_app(), base_url="http://localhost:8001")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patcher.stop()
        self.tmp.cleanup()

    def call(self, arguments, chat_id=None, allowed=None, name="save_tool_result"):
        params = {"name": name, "arguments": arguments}
        if chat_id:
            params["_meta"] = {"agentscore/chat_id": chat_id, "agentscore/agent_id": "agent-1"}
            if allowed is not None:
                params["_meta"]["agentscore/allowed_tools"] = allowed
        resp = self.client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["result"]["structuredContent"]["result"]

    def test_commits_report_then_only_new(self):
        args = {"tool": "git_host_list_commits", "args": ARGS}
        first = self.call(args, chat_id="chat-1")
        self.assertTrue(first["first_run"])
        self.assertTrue(first["chat_scoped"])
        self.assertEqual([c["sha"] for c in first["new_items"]], ["c3", "c2", "c1"])
        self.assertTrue(first["snapshot"].startswith("snapshots/chat-1/"))
        # Снимок виден среди файлов (раздел «Файлы» в приложении).
        self.assertIn(first["snapshot"], [f["path"] for f in self.files.list()])

        nothing = self.call(args, chat_id="chat-1")
        self.assertFalse(nothing["has_changes"])
        self.assertIn("новых 0", nothing["summary"])

        self.provider.commits.insert(0, commit("c4", "Новая функция"))
        delta = self.call(args, chat_id="chat-1")
        self.assertEqual([c["sha"] for c in delta["new_items"]], ["c4"])
        self.assertEqual(delta["new_count"], 1)

        # В другом чате — свой снимок: там это первый вызов.
        other = self.call(args, chat_id="chat-2")
        self.assertTrue(other["first_run"])

    def test_source_tool_must_be_selected_in_chat(self):
        denied = self.call({"tool": "git_host_list_commits", "args": ARGS}, chat_id="c", allowed=["save_tool_result"])
        self.assertIn("не выбран", denied["error"])
        ok = self.call({"tool": "git_host_list_commits", "args": ARGS}, chat_id="c",
                       allowed=["save_tool_result", "git_host_list_commits"])
        self.assertTrue(ok["first_run"])

    def test_pipeline_limited_to_chat_tools(self):
        steps = [{"tool": "git_host_list_commits", "args": ARGS}]
        denied = self.call({"steps": steps}, chat_id="c", allowed=["run_pipeline"], name="run_pipeline")
        self.assertIn("не выбран", denied["error"])
        # Через save_tool_result в цепочке тоже нельзя обойти выбор инструментов.
        sneaky = self.call(
            {"steps": [{"tool": "save_tool_result", "args": {"tool": "git_host_list_commits", "args": ARGS}}]},
            chat_id="c", allowed=["run_pipeline", "save_tool_result"], name="run_pipeline",
        )
        self.assertFalse(sneaky["ok"])
        self.assertIn("не выбран", sneaky["steps"][0]["error"])
        # Без чата (планировщик) ограничений нет.
        free = self.call({"steps": steps}, name="run_pipeline")
        self.assertTrue(free["ok"])

    def test_commits_summary_file_pipeline(self):
        self.llm.answer = "## Краткий ответ\nТри коммита от Ann."
        allowed = ["run_pipeline", "git_host_list_commits", "summarize", "save_to_text_file"]
        result = self.call({"steps": [
            {"tool": "git_host_list_commits", "args": ARGS},
            {"tool": "summarize", "args": {"result_id": "$prev", "question": "Отчёт по коммитам"}},
            {"tool": "save_to_text_file", "args": {"result_id": "$prev", "filename": "reports/commits.md"}},
        ]}, chat_id="c", allowed=allowed, name="run_pipeline")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["steps"][0]["auto_stored"])
        self.assertEqual(result["steps"][1]["source_result_id"], result["steps"][0]["result_id"])
        self.assertTrue(all(c["ok"] for c in result["checks"]), result["checks"])
        # Модель получила сами коммиты.
        self.assertIn('"sha":"c3"', self.llm.prompts[0][1])
        content = self.files.read("reports/commits.md")["content"]
        self.assertEqual(content, "# Отчёт по коммитам\n\n## Краткий ответ\nТри коммита от Ann.\n")

    def test_tracking_in_pipeline_uses_chat_folder(self):
        result = self.call({"steps": [
            {"tool": "save_tool_result", "args": {"tool": "git_host_list_commits", "args": ARGS}},
        ]}, chat_id="chat-9", name="run_pipeline")
        self.assertTrue(result["steps"][0]["output"]["snapshot"].startswith("snapshots/chat-9/"))

    def test_without_chat_uses_shared(self):
        result = self.call({"tool": "git_host_list_commits", "args": ARGS})
        self.assertFalse(result["chat_scoped"])
        self.assertTrue(result["snapshot"].startswith("snapshots/shared/"))

    def test_rejects_non_trackable_and_bad_args(self):
        self.assertIn("нельзя отслеживать", self.call({"tool": "save_to_text_file", "args": {}})["error"])
        self.assertIn("нельзя отслеживать", self.call({"tool": "save_tool_result", "args": {}})["error"])
        self.assertIn("неверные аргументы", self.call({"tool": "git_host_list_commits", "args": {"host": "github"}})["error"])

    def test_source_error_passed_through(self):
        result = self.call({"tool": "git_host_list_commits", "args": {**ARGS, "host": "bitbucket"}})
        self.assertIn("Неизвестный host", result["error"])

    def test_max_items_limits_returned_list(self):
        result = self.call({"tool": "git_host_list_commits", "args": ARGS, "max_items": 2}, chat_id="c")
        self.assertEqual(len(result["new_items"]), 2)
        self.assertEqual(result["new_items_omitted"], 1)
        self.assertEqual(result["new_count"], 3)


if __name__ == "__main__":
    unittest.main()

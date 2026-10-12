"""Тесты инструментов-звеньев пайплайна (DuckDuckGo → Суммарный ответ →
Сохранить в текстовый файл) и `run_pipeline`. Без сети: поиск, модель и
загрузка страниц подменены."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from mcp_server import web_search
from mcp_server.features import Features
from mcp_server.files import FileStore, FileToolError, render_filename, safe_relative_path
from mcp_server.llm import LLMClient, LLMError, Source, check_citations, render_sources_markdown
from mcp_server.pipeline_tools import PipelineTools
from mcp_server.results import KIND_SEARCH_RESULTS, ResultNotFoundError, ResultStore
from mcp_server.server import build_mcp_server
from mcp_server.web_pages import PageFetchError, WebPage, _ensure_public, html_to_text, normalize_url, parse_html

RAW_RESULTS = [
    {"title": "Android 17: локальная сеть", "href": "https://example.com/a", "body": "Нужно разрешение ACCESS_LOCAL_NETWORK"},
    {"title": "Дубликат", "href": "https://example.com/a", "body": "тот же адрес"},
    {"title": "Изменения поведения", "href": "https://example.com/b", "body": "targetSdk 37"},
    {"title": "Без сниппета", "href": "https://example.com/c"},
]


class FakeSearch:
    def __init__(self, results=None, error=None):
        self.results = RAW_RESULTS if results is None else results
        self.error = error
        self.calls = []

    def __call__(self, query, region, timelimit, max_results):
        self.calls.append((query, region, timelimit, max_results))
        if self.error:
            raise self.error
        return list(self.results)


class FakeLLM:
    def __init__(self, answer="## Краткий ответ\nНужно разрешение [1], см. также [2, 9].\n\n## Ключевые тезисы\n- targetSdk 37 [2]",
                 error=None):
        self.answer = answer
        self.error = error
        self.prompts = []

    async def complete(self, system, user):
        self.prompts.append((system, user))
        if self.error:
            raise self.error
        return self.answer


def _structured(result) -> dict:
    _, structured = result
    return structured.get("result", structured)


class Env:
    """Временные каталоги и сервер с подменёнными зависимостями."""

    def __init__(self, search=None, llm=None, reader=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResultStore(os.path.join(self.tmp.name, "data"))
        self.files = FileStore(os.path.join(self.tmp.name, "files"))
        self.search = search or FakeSearch()
        self.llm = llm or FakeLLM()
        self.tools = PipelineTools(
            self.store, search_fn=self.search, llm=self.llm, files=self.files,
            page_reader=reader,
        )
        self.mcp = build_mcp_server(Features.all_enabled(), pipeline_tools=self.tools)

    async def call(self, name, args):
        return _structured(await self.mcp.call_tool(name, args))

    def close(self):
        self.tmp.cleanup()


class ResultStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResultStore(self.tmp.name, ttl_hours=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_and_text_sha(self):
        r = self.store.put("markdown", {"text": "Привет"}, source_result_id="r_000000000000")
        got = self.store.get(r.id)
        self.assertEqual(got.data, {"text": "Привет"})
        self.assertEqual(got.sha256, hashlib.sha256("Привет".encode()).hexdigest())
        self.assertEqual(got.source_result_id, "r_000000000000")

    def test_bad_or_missing_id(self):
        with self.assertRaises(ResultNotFoundError):
            self.store.get("../../etc/passwd")
        with self.assertRaises(ResultNotFoundError):
            self.store.get("r_0123456789ab")

    def test_expired_results_removed(self):
        old = self.store.put("text", {"text": "старое"})
        path = self.store.dir / f"{old.id}.json"
        past = time.time() - 7200
        os.utime(path, (past, past))
        self.store.put("text", {"text": "новое"})
        self.assertFalse(path.exists())


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_dedupes_and_numbers(self):
        items = await web_search.search(FakeSearch(), "android 17", "ru-ru", "w", 10)
        self.assertEqual([i.n for i in items], [1, 2, 3])
        self.assertEqual([i.url for i in items], ["https://example.com/a", "https://example.com/b", "https://example.com/c"])
        self.assertEqual(items[2].snippet, "")

    async def test_validation(self):
        with self.assertRaises(web_search.WebSearchError):
            await web_search.search(FakeSearch(), "  ", "ru-ru", None, 5)
        with self.assertRaises(web_search.WebSearchError):
            await web_search.search(FakeSearch(), "x", "ru-ru", "week", 5)

    async def test_max_results_clamped(self):
        fake = FakeSearch()
        await web_search.search(fake, "x", "ru-ru", None, 1000)
        self.assertEqual(fake.calls[0][3], web_search.MAX_RESULTS_LIMIT)


class LlmHelpersTests(unittest.IsolatedAsyncioTestCase):
    def test_check_citations_removes_unknown_numbers(self):
        checked = check_citations("А [1], Б [2, 9], В [7].", 3)
        self.assertEqual(checked["text"], "А [1], Б [2], В .")
        self.assertEqual(checked["used"], [1, 2])
        self.assertEqual(checked["invalid"], 2)

    def test_sources_rendered_by_code(self):
        md = render_sources_markdown("Тема", "Текст [1]", [Source(1, "Заголовок [x]", "https://e.com/a b", "")])
        self.assertIn("## Источники", md)
        self.assertIn("1. [Заголовок \\[x\\]](https://e.com/a%20b)", md)

    async def test_llm_client_openai_compatible(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": " Ответ "}}]})

        client = LLMClient("https://llm.test/v1", "key", "m1", transport=httpx.MockTransport(handler))
        self.assertEqual(await client.complete("sys", "user"), "Ответ")
        self.assertEqual(seen["url"], "https://llm.test/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer key")
        self.assertEqual(seen["body"]["model"], "m1")
        self.assertEqual(seen["body"]["messages"][0], {"role": "system", "content": "sys"})

    async def test_llm_client_http_error(self):
        client = LLMClient(
            "https://llm.test", "key", "m", transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")),
        )
        with self.assertRaises(LLMError):
            await client.complete("s", "u")


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = FileStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_unsafe_paths(self):
        for bad in ("/etc/passwd.md", "../x.md", "a/../../x.md", ".hidden.md", "a\\b.md", "x.exe", "C:/x.md"):
            with self.assertRaises(FileToolError, msg=bad):
                safe_relative_path(bad)

    def test_default_extension_and_template(self):
        self.assertEqual(safe_relative_path("reports/итоги").as_posix(), "reports/итоги.md")
        from datetime import datetime

        self.assertEqual(render_filename("r/{date}_{time}.md", datetime(2026, 9, 28, 9, 5)), "r/2026-09-28_09-05.md")

    def test_save_no_overwrite_by_default(self):
        saved = self.files.save("reports/a.md", "текст")
        self.assertEqual(saved.path, "reports/a.md")
        self.assertEqual(Path(self.tmp.name, "reports/a.md").read_text(encoding="utf-8"), "текст")
        with self.assertRaises(FileToolError):
            self.files.save("reports/a.md", "другой")
        again = self.files.save("reports/a.md", "другой", overwrite=True)
        self.assertTrue(again.overwritten)
        self.assertEqual(self.files.read("reports/a.md")["content"], "другой")
        self.assertEqual([f["path"] for f in self.files.list()], ["reports/a.md"])


class WebPagesTests(unittest.IsolatedAsyncioTestCase):
    def test_html_to_text_skips_scripts_and_menus(self):
        html = "<html><head><title>T</title><script>evil()</script></head><body><nav>Меню</nav>" \
               "<h1>Заголовок</h1><p>Первый&nbsp;абзац</p><div>Второй</div></body></html>"
        self.assertEqual(html_to_text(html), "Заголовок\nПервый абзац\nВторой")

    async def test_internal_addresses_blocked(self):
        for url in ("http://127.0.0.1/", "http://localhost:8001/api", "http://10.0.2.2:8000/", "file:///etc/passwd"):
            with self.assertRaises(PageFetchError, msg=url):
                await _ensure_public(httpx.URL(url))


class PipelineToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    async def test_three_tools_step_by_step(self):
        found = await self.env.call("duckduckgo_search", {"query": "Android 17 локальная сеть", "timelimit": "m"})
        self.assertEqual(found["count"], 3)
        self.assertEqual(self.env.search.calls[0][1:], ("ru-ru", "m", 10))

        summary = await self.env.call("summarize", {"result_id": found["result_id"]})
        self.assertEqual(summary["source_result_id"], found["result_id"])
        self.assertEqual(summary["sources_count"], 3)
        self.assertEqual(summary["citations_used"], [1, 2])
        self.assertEqual(summary["invalid_citations"], 1)  # [9] — выдуманный источник удалён
        self.assertNotIn("[2, 9]", summary["markdown"])
        # Все найденные адреса — в списке источников, в том же порядке.
        for n, url in enumerate(["https://example.com/a", "https://example.com/b", "https://example.com/c"], start=1):
            self.assertIn(f"{n}. [", summary["markdown"])
            self.assertIn(f"]({url})", summary["markdown"])
        # Модель получила материалы поиска, а не что-то другое.
        prompt = self.env.llm.prompts[0][1]
        self.assertIn("[1] Android 17: локальная сеть", prompt)
        self.assertIn("Нужно разрешение ACCESS_LOCAL_NETWORK", prompt)

        saved = await self.env.call("save_to_text_file", {"result_id": summary["result_id"], "filename": "reports/android17"})
        self.assertEqual(saved["path"], "reports/android17.md")
        self.assertTrue(saved["content_matches_source"])
        content = self.env.files.read("reports/android17.md")["content"]
        self.assertEqual(content, summary["markdown"])
        self.assertEqual(saved["sha256"], summary["sha256"])

    async def test_run_pipeline_end_to_end(self):
        result = await self.env.call("run_pipeline", {"steps": [
            {"tool": "duckduckgo_search", "args": {"query": "Android 17 локальная сеть"}},
            {"tool": "summarize", "args": {"result_id": "$prev", "length": "short"}},
            {"tool": "save_to_text_file", "args": {"result_id": "$prev", "filename": "reports/{date}-android17.md"}},
        ]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["completed_steps"], 3)
        steps = result["steps"]
        self.assertEqual(steps[1]["source_result_id"], steps[0]["result_id"])
        self.assertEqual(steps[2]["source_result_id"], steps[1]["result_id"])
        self.assertEqual(steps[2]["sha256"], steps[1]["sha256"])
        self.assertEqual(len(result["checks"]), 3)
        self.assertTrue(all(c["ok"] for c in result["checks"]), result["checks"])
        files = self.env.files.list()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["path"].endswith("-android17.md"))
        self.assertIn("## Источники", self.env.files.read(files[0]["path"])["content"])

    async def test_pipeline_stops_on_error_and_writes_nothing(self):
        self.env.llm.error = LLMError("модель недоступна")
        result = await self.env.call("run_pipeline", {"steps": [
            {"tool": "duckduckgo_search", "args": {"query": "x"}},
            {"tool": "summarize", "args": {"result_id": "$prev"}},
            {"tool": "save_to_text_file", "args": {"result_id": "$prev", "filename": "x.md"}},
        ]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["steps"][1]["error"], "модель недоступна")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(self.env.files.list(), [])

    async def test_pipeline_validation(self):
        bad = await self.env.call("run_pipeline", {"steps": [{"tool": "run_pipeline", "args": {}}]})
        self.assertIn("error", bad)
        ref = await self.env.call("run_pipeline", {"steps": [{"tool": "summarize", "args": {"result_id": "$prev"}}]})
        self.assertFalse(ref["ok"])
        self.assertIn("нет предыдущего", ref["steps"][0]["error"])

    async def test_nothing_found_is_error(self):
        self.env.search.results = []
        result = await self.env.call("duckduckgo_search", {"query": "абракадабра"})
        self.assertIn("ничего не найдено", result["error"])

    async def test_rate_limit_error_is_readable(self):
        self.env.search.error = web_search.WebSearchError("DuckDuckGo временно ограничил частоту запросов")
        result = await self.env.call("duckduckgo_search", {"query": "x"})
        self.assertIn("ограничил", result["error"])

    async def test_summarize_text_and_argument_rules(self):
        both = await self.env.call("summarize", {"text": "a", "result_id": "r_0123456789ab"})
        self.assertIn("error", both)
        missing = await self.env.call("summarize", {"result_id": "r_0123456789ab"})
        self.assertIn("не найден", missing["error"])
        self.env.llm.answer = "Главное: всё хорошо."
        summary = await self.env.call("summarize", {"text": "Длинный текст", "question": "Итог встречи"})
        self.assertEqual(summary["markdown"], "# Итог встречи\n\nГлавное: всё хорошо.\n")
        self.assertIsNone(summary["source_result_id"])

    async def test_save_plain_content(self):
        saved = await self.env.call("save_to_text_file", {"filename": "notes/a.txt", "content": "заметка"})
        self.assertEqual(saved["path"], "notes/a.txt")
        self.assertIsNone(saved["content_matches_source"])
        bad = await self.env.call("save_to_text_file", {"filename": "../a.md", "content": "x"})
        self.assertIn("error", bad)

    async def test_save_search_results_as_json(self):
        found = await self.env.call("duckduckgo_search", {"query": "x"})
        saved = await self.env.call("save_to_text_file", {"result_id": found["result_id"], "filename": "raw.json"})
        data = json.loads(self.env.files.read("raw.json")["content"])
        self.assertEqual(len(data["items"]), 3)
        self.assertIsNone(saved["content_matches_source"])


class PagesDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_are_fetched_and_passed_to_model(self):
        async def reader(url):
            if url.endswith("/b"):
                raise PageFetchError("HTTP 404")
            return WebPage(url=url, title="", text=f"Полный текст страницы {url}")

        env = Env(reader=reader)
        try:
            found = await env.call("duckduckgo_search", {"query": "x"})
            summary = await env.call("summarize", {"result_id": found["result_id"], "depth": "pages", "max_pages": 2})
            self.assertEqual(summary["pages_fetched"], 1)
            self.assertEqual(summary["pages_failed"][0]["n"], 2)
            self.assertIn("Полный текст страницы https://example.com/a", env.llm.prompts[0][1])
        finally:
            env.close()

    async def test_read_web_page_returns_text_links_and_chains(self):
        opened = []

        async def reader(url):
            opened.append(url)
            html = ("<title>РБК</title><h1>Главное</h1><ul>"
                    "<li><a href='/economics/1'>Центробанк сохранил ключевую ставку на прежнем уровне</a></li>"
                    "<li><a href='/politics/2'>Правительство обсудило бюджет на следующий год</a></li>"
                    "<li><a href='/x'>Ещё</a></li></ul>")
            return parse_html(html, "https://www.rbc.ru/")

        env = Env(reader=reader)
        try:
            page = await env.call("read_web_page", {"url": "rbc.ru"})
            self.assertEqual(page["url"], "https://www.rbc.ru/")
            self.assertEqual(page["title"], "РБК")
            self.assertFalse(page["truncated"])
            self.assertIn("Центробанк сохранил ключевую ставку", page["text"])
            self.assertEqual(
                [link["url"] for link in page["links"]],
                ["https://www.rbc.ru/economics/1", "https://www.rbc.ru/politics/2"],
            )
            # Полный текст сохранён — по нему можно сделать суммарный ответ.
            env.llm.answer = "Главное за день."
            summary = await env.call("summarize", {"result_id": page["result_id"], "question": "Новости РБК"})
            self.assertEqual(summary["source_result_id"], page["result_id"])
            self.assertIn("Правительство обсудило бюджет", env.llm.prompts[0][1])
        finally:
            env.close()

    async def test_read_web_page_errors(self):
        async def reader(url):
            if "empty" in url:
                return WebPage(url=url, title="", text="  ")
            raise PageFetchError("адрес ведёт во внутреннюю сеть")

        env = Env(reader=reader)
        try:
            self.assertIn("JavaScript", (await env.call("read_web_page", {"url": "https://empty.test"}))["error"])
            self.assertIn("внутреннюю сеть", (await env.call("read_web_page", {"url": "http://10.0.0.1"}))["error"])
        finally:
            env.close()

    def test_normalize_url(self):
        self.assertEqual(normalize_url("rbc.ru"), "https://rbc.ru")
        self.assertEqual(normalize_url("http://rbc.ru/news"), "http://rbc.ru/news")
        with self.assertRaises(PageFetchError):
            normalize_url("ftp://x.test/file")
        with self.assertRaises(PageFetchError):
            normalize_url("  ")

    def test_links_extracted_absolute_deduplicated(self):
        page = parse_html(
            "<nav><a href='/menu'>Длинный пункт меню навигации сайта</a></nav>"
            "<a href='/n/1#comments'>Первая длинная новость про экономику</a>"
            "<a href='https://www.rbc.ru/n/1'>Первая длинная новость про экономику</a>"
            "<a href='javascript:void(0)'>Нажмите сюда, чтобы открыть окно</a>",
            "https://www.rbc.ru/",
        )
        self.assertEqual(page.links, [{"text": "Первая длинная новость про экономику", "url": "https://www.rbc.ru/n/1"}])

    def test_search_results_kind(self):
        self.assertEqual(KIND_SEARCH_RESULTS, "search_results")


if __name__ == "__main__":
    unittest.main()

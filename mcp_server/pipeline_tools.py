"""
mcp_server.pipeline_tools
===========================

Общие инструменты-звенья пайплайнов. Каждый можно вызывать отдельно (из
чата, по расписанию) или цепочкой — передавая `result_id` предыдущего шага:

- `duckduckgo_search` («Интернет поиск») — ищет и сохраняет выдачу;
- `read_web_page` («Интернет поиск») — открывает страницу: текст и ссылки;
- `summarize` («Обработка LLM») — суммарный ответ по результату или тексту;
- `save_to_text_file` («Работа с файлами») — сохраняет результат или текст в файл.

Зависимости (поиск, модель, загрузка страниц, хранилища) передаются в
конструктор — так тесты работают без сети.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import web_search
from .files import FileStore
from .llm import (
    LLMClient, SYSTEM_PROMPT, Source, build_sources_prompt, build_text_prompt, check_citations,
    render_sources_markdown, render_text_markdown,
)
from .results import KIND_MARKDOWN, KIND_SEARCH_RESULTS, KIND_WEB_PAGE, ResultStore, StoredResult
from .web_pages import MAX_PAGE_CHARS, PageFetchError, WebPage

MAX_SUMMARY_INPUT_CHARS = 30_000
MAX_PAGES = 5
DEPTHS = ("snippets", "pages")

#: url -> страница (поднимает исключение, если не удалось)
PageReader = Callable[[str], Awaitable[WebPage]]

#: Сколько текста страницы хранить (result_id) и сколько максимум отдавать модели.
MAX_STORED_PAGE_CHARS = 50_000
MAX_RETURNED_PAGE_CHARS = 20_000
MAX_RETURNED_LINKS = 30


class ToolError(Exception):
    """Ожидаемая ошибка инструмента — вернётся как `{"error": ...}`."""


class PipelineTools:
    def __init__(
        self, store: ResultStore, *, search_fn: Optional[web_search.SearchFn] = None,
        region: str = "ru-ru", llm: Optional[LLMClient] = None, page_reader: Optional[PageReader] = None,
        files: Optional[FileStore] = None,
    ):
        self.store = store
        self.search_fn = search_fn
        self.region = region
        self.llm = llm
        self.page_reader = page_reader
        self.files = files

    # -- DuckDuckGo ------------------------------------------------------------

    async def duckduckgo_search(
        self, query: str, max_results: int = 10, timelimit: Optional[str] = None, region: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.search_fn is None:
            raise ToolError("поиск в DuckDuckGo не настроен")
        region = region or self.region
        try:
            items = await web_search.search(self.search_fn, query, region, timelimit, max_results)
        except web_search.WebSearchError as exc:
            raise ToolError(str(exc)) from exc
        if not items:
            raise ToolError(f"по запросу «{query}» ничего не найдено")
        stored = self.store.put(KIND_SEARCH_RESULTS, {
            "query": query.strip(), "provider": "duckduckgo", "region": region, "timelimit": timelimit,
            "items": [i.as_dict() for i in items],
        })
        return {
            "result_id": stored.id, "kind": stored.kind, "query": query.strip(), "count": len(items),
            "sha256": stored.sha256,
            "results": [
                {**i.as_dict(), "snippet": i.snippet[:300]} for i in items
            ],
        }

    async def read_web_page(self, url: str, max_chars: int = 8000, include_links: bool = True) -> Dict[str, Any]:
        if self.page_reader is None:
            raise ToolError("чтение страниц не настроено")
        try:
            page = await self.page_reader(url)
        except PageFetchError as exc:
            raise ToolError(f"не удалось открыть {url}: {exc}") from exc
        if not page.text.strip():
            raise ToolError(
                f"на странице {page.url} нет текста — возможно, она показывает содержимое только через JavaScript"
            )
        text = page.text[:MAX_STORED_PAGE_CHARS]
        stored = self.store.put(KIND_WEB_PAGE, {
            "url": page.url, "title": page.title, "text": text, "links": page.links,
        })
        limit = max(500, min(int(max_chars or 8000), MAX_RETURNED_PAGE_CHARS))
        result: Dict[str, Any] = {
            "result_id": stored.id, "kind": stored.kind, "url": page.url, "title": page.title,
            "chars": len(text), "truncated": len(text) > limit, "sha256": stored.sha256,
            "text": text[:limit],
        }
        if include_links:
            result["links"] = page.links[:MAX_RETURNED_LINKS]
        return result

    # -- Обработка LLM ------------------------------------------------------------

    async def summarize(
        self, result_id: Optional[str] = None, text: Optional[str] = None, question: Optional[str] = None,
        depth: str = "snippets", max_pages: int = 3, length: str = "medium",
    ) -> Dict[str, Any]:
        if self.llm is None:
            raise ToolError("модель для суммарного ответа не настроена (MCP_LLM_API_KEY)")
        if bool(result_id) == bool(text):
            raise ToolError("укажите ровно одно из: result_id (результат предыдущего шага) или text")
        if depth not in DEPTHS:
            raise ToolError(f"depth должен быть одним из {list(DEPTHS)}")

        source: Optional[StoredResult] = self.store.get(result_id) if result_id else None
        info: Dict[str, Any] = {"sources_count": 0, "citations_used": [], "invalid_citations": 0}
        if source is not None and source.kind == KIND_SEARCH_RESULTS:
            title = (question or source.data.get("query") or "Суммарный ответ").strip()
            sources = [
                Source(n=i["n"], title=i["title"], url=i["url"], snippet=i.get("snippet", ""))
                for i in source.data.get("items", [])
            ]
            if not sources:
                raise ToolError(f"в результате {result_id} нет найденных страниц")
            if depth == "pages":
                info.update(await self._attach_pages(sources, max_pages))
            body = await self._complete(build_sources_prompt(title, sources, length))
            checked = check_citations(body, len(sources))
            markdown = render_sources_markdown(title, checked["text"], sources)
            info.update(
                sources_count=len(sources), citations_used=checked["used"], invalid_citations=checked["invalid"],
            )
        else:
            material = source.text() if source is not None else text or ""
            if len(material) > MAX_SUMMARY_INPUT_CHARS:
                material = material[:MAX_SUMMARY_INPUT_CHARS]
                info["input_truncated"] = True
            title = (question or "Суммарный ответ").strip()
            body = await self._complete(build_text_prompt(question or "Кратко изложи главное", material, length))
            markdown = render_text_markdown(title, body)

        stored = self.store.put(KIND_MARKDOWN, {"title": title, "text": markdown}, source_result_id=result_id)
        return {
            "result_id": stored.id, "kind": stored.kind, "source_result_id": result_id, "sha256": stored.sha256,
            "chars": len(markdown), **info, "markdown": markdown,
        }

    async def _complete(self, prompt: str) -> str:
        from .llm import LLMError

        try:
            return await self.llm.complete(SYSTEM_PROMPT, prompt)
        except LLMError as exc:
            raise ToolError(str(exc)) from exc

    async def _attach_pages(self, sources: List[Source], max_pages: int) -> Dict[str, Any]:
        if self.page_reader is None:
            raise ToolError("загрузка страниц не настроена")
        top = sources[: max(1, min(int(max_pages or 3), MAX_PAGES))]
        results = await asyncio.gather(*(self.page_reader(s.url) for s in top), return_exceptions=True)
        fetched, failed = 0, []
        for s, res in zip(top, results):
            if isinstance(res, Exception):
                failed.append({"n": s.n, "url": s.url, "error": str(res)[:200]})
            elif res.text:
                s.page_text = res.text[:MAX_PAGE_CHARS]
                fetched += 1
        return {"pages_fetched": fetched, "pages_failed": failed}

    # -- Работа с файлами ---------------------------------------------------------

    async def save_to_text_file(
        self, filename: str, result_id: Optional[str] = None, content: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        if self.files is None:
            raise ToolError("работа с файлами не настроена")
        if bool(result_id) == bool(content):
            raise ToolError("укажите ровно одно из: result_id (результат предыдущего шага) или content")
        source: Optional[StoredResult] = self.store.get(result_id) if result_id else None
        if source is not None:
            is_text = isinstance(source.data.get("text"), str)
            text = source.data["text"] if is_text else json.dumps(source.data, ensure_ascii=False, indent=2)
        else:
            is_text, text = True, content or ""
        from .files import FileToolError

        try:
            saved = await asyncio.to_thread(self.files.save, filename, text, overwrite)
        except FileToolError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "path": saved.path, "bytes": saved.bytes, "sha256": saved.sha256, "overwritten": saved.overwritten,
            "source_result_id": result_id,
            # Содержимое файла совпадает с сохраняемым результатом байт в байт.
            "content_matches_source": (saved.sha256 == source.sha256) if source is not None and is_text else None,
        }

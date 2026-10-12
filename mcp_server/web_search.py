"""
mcp_server.web_search
=======================

Поиск в DuckDuckGo через библиотеку `ddgs` (бывшая `duckduckgo_search`).

Официального API веб-поиска у DuckDuckGo нет: `ddgs` разбирает страницы
html/lite.duckduckgo.com. Библиотека неофициальная и при частых запросах
упирается в ограничение по частоте — тогда инструмент возвращает понятную
ошибку (планировщик в таком случае повторит попытку). Для учебного проекта
этого достаточно; для эксплуатации нужен официальный провайдер.

`ddgs` синхронная — вызывается в отдельном потоке. Функцию поиска можно
подменить (`search_fn`) — так тесты работают без сети.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

#: Ограничение по времени у ddgs: d — день, w — неделя, m — месяц, y — год.
TIMELIMITS = {"d", "w", "m", "y"}

MAX_RESULTS_LIMIT = 30

#: (query, region, timelimit, max_results) -> [{"title", "href", "body"}, ...]
SearchFn = Callable[[str, str, Optional[str], int], List[Dict[str, Any]]]


class WebSearchError(Exception):
    """Поиск не удался — инструмент вернёт `{"error": ...}`."""


@dataclass
class SearchItem:
    n: int
    title: str
    url: str
    snippet: str

    def as_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "title": self.title, "url": self.url, "snippet": self.snippet}


def make_ddgs_search(backend: str, timeout: float) -> SearchFn:
    """Функция поиска поверх `ddgs`; импорт ленивый, чтобы сервер
    запускался и без библиотеки (группа тогда просто не включается)."""

    def search(query: str, region: str, timelimit: Optional[str], max_results: int) -> List[Dict[str, Any]]:
        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise WebSearchError("библиотека ddgs не установлена: pip install ddgs") from exc
        try:
            return DDGS(timeout=int(timeout)).text(
                query, region=region, safesearch="moderate", timelimit=timelimit,
                max_results=max_results, backend=backend,
            ) or []
        except RatelimitException as exc:
            raise WebSearchError(
                "DuckDuckGo временно ограничил частоту запросов — повторите через минуту"
            ) from exc
        except TimeoutException as exc:
            raise WebSearchError(f"DuckDuckGo не ответил за {timeout:g} с") from exc
        except DDGSException as exc:
            if "no results" in str(exc).lower():
                return []
            raise WebSearchError(f"ошибка поиска DuckDuckGo: {exc}") from exc

    return search


async def search(
    search_fn: SearchFn, query: str, region: str, timelimit: Optional[str], max_results: int,
) -> List[SearchItem]:
    query = (query or "").strip()
    if not query:
        raise WebSearchError("пустой поисковый запрос")
    if timelimit is not None and timelimit not in TIMELIMITS:
        raise WebSearchError(f"timelimit должен быть одним из {sorted(TIMELIMITS)} или не задан")
    max_results = max(1, min(int(max_results or 10), MAX_RESULTS_LIMIT))
    raw = await asyncio.to_thread(search_fn, query, region, timelimit, max_results)
    items: List[SearchItem] = []
    seen = set()
    for entry in raw:
        url = str(entry.get("href") or entry.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(SearchItem(
            n=len(items) + 1,
            title=str(entry.get("title") or url).strip(),
            url=url,
            snippet=str(entry.get("body") or entry.get("snippet") or "").strip(),
        ))
        if len(items) >= max_results:
            break
    return items

"""
mcp_server.git_hosts.base
============================

Общий интерфейс `GitHostProvider` + вспомогательные функции: HTTP-запрос с
ретраями/backoff (стиль `agents_core.providers.deepseek_client.
DeepSeekClient._request`, только асинхронный, на `httpx`), разбор `since`
(ISO8601 или относительное "-1h"/"-1d"), нормализация rate-limit заголовков.

Токены НИКОГДА не попадают в исключения/логи — только в заголовок
Authorization самого запроса.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from ..models import GitHostStatus

_RELATIVE_SINCE_RE = re.compile(r"^-(\d+)(s|m|h|d)$")


class GitHostError(Exception):
    """Ошибка запроса к Git-хостингу (сетевая либо HTTP-статус не 2xx)."""

    def __init__(self, status_code: Optional[int], message: str, host: str):
        super().__init__(f"[{host}] {message}" + (f" (HTTP {status_code})" if status_code else ""))
        self.status_code = status_code
        self.message = message
        self.host = host


def parse_since(value: Optional[str]) -> Optional[datetime]:
    """`since` из ТЗ поддерживает ISO8601 ("2026-01-01T00:00:00Z") и
    относительные значения ("-1h", "-1d", "-30m", "-45s") — относительно
    текущего момента (UTC). Возвращает `None`, если `value` пусто."""
    if not value:
        return None
    m = _RELATIVE_SINCE_RE.match(value.strip())
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]
        return datetime.now(timezone.utc) - timedelta(**{delta: amount})
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать since={value!r}: ожидается ISO8601 либо '-<N>s|m|h|d'") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def request_with_retry(
    client: httpx.AsyncClient, method: str, url: str, *, host: str,
    params: Optional[Dict[str, Any]] = None, max_retries: int = 3, backoff_seconds: float = 1.0,
) -> httpx.Response:
    """Запрос с ретраями на сетевых сбоях и 429/5xx (экспоненциальный
    backoff, уважает `Retry-After`), поднимает `GitHostError` на итоговой
    неудаче — без утечки токена (он только в заголовках клиента, сюда не
    попадает)."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, params=params)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < max_retries:
                await asyncio.sleep(backoff_seconds * (2 ** attempt))
                continue
            raise GitHostError(None, f"сетевая ошибка: {exc}", host) from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else backoff_seconds * (2 ** attempt)
                await asyncio.sleep(delay)
                continue

        if resp.status_code >= 400:
            message = _extract_error_message(resp)
            raise GitHostError(resp.status_code, message, host)

        return resp

    raise GitHostError(None, f"не удалось выполнить запрос за {max_retries + 1} попыток(и): {last_exc}", host)


def _extract_error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("error") or body)
        return str(body)
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}"


def rate_limit_from_headers(headers: httpx.Headers) -> Dict[str, Optional[int]]:
    """Разные хостинги называют заголовки чуть по-разному — берём первое, что
    находится; None, если хостинг вообще не прислал лимиты (например,
    Gitea старых версий)."""
    remaining = headers.get("X-RateLimit-Remaining") or headers.get("RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset") or headers.get("RateLimit-Reset")
    return {
        "remaining": int(remaining) if remaining is not None and remaining.isdigit() else None,
        "reset_at": int(reset) if reset is not None and reset.isdigit() else None,
    }


@dataclass
class NormalizedItem:
    """Общая форма одного элемента (коммит/PR/issue/релиз) ПОСЛЕ
    нормализации конкретным провайдером — используется только для
    клиентской фильтрации по `since` (см. `GitHostProvider._filter_since`),
    финальный ответ инструмента — исходный нормализованный dict."""

    timestamp: Optional[datetime]
    data: Dict[str, Any]


class GitHostProvider:
    """Базовый класс — конкретные хостинги переопределяют методы ниже.
    `client` — уже настроенный `httpx.AsyncClient` (base_url + заголовок
    авторизации, если токен задан) — общий для всех вызовов одного
    провайдера, чтобы переиспользовать соединение."""

    host: str = "base"

    def __init__(
        self, client: httpx.AsyncClient, api_url: str, token: Optional[str] = None, *,
        max_retries: int = 3, backoff_seconds: float = 1.0,
    ):
        self.client = client
        self.api_url = api_url
        self.token = token
        # Настраиваемо ТОЛЬКО ради тестов (тесты 429/5xx-ретраев не должны
        # реально спать секундами) — в проде всегда дефолт.
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        return await request_with_retry(
            self.client, "GET", path, host=self.host, params=params,
            max_retries=self.max_retries, backoff_seconds=self.backoff_seconds,
        )

    @staticmethod
    def _apply_since_and_limit(items: List[NormalizedItem], since: Optional[datetime], limit: int) -> List[Dict[str, Any]]:
        if since is not None:
            items = [i for i in items if i.timestamp is None or i.timestamp >= since]
        return [i.data for i in items[:limit]]

    async def get_status(self) -> GitHostStatus:
        raise NotImplementedError

    async def list_commits(
        self, owner: str, repo: str, *, since: Optional[str] = None, until: Optional[str] = None,
        branch: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def list_pull_requests(
        self, owner: str, repo: str, *, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def list_issues(
        self, owner: str, repo: str, *, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def list_releases(self, owner: str, repo: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_file(self, owner: str, repo: str, path: str, *, ref: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

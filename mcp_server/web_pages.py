"""
mcp_server.web_pages
======================

Загрузка веб-страниц и извлечение из HTML читаемого текста и ссылок —
для инструмента «Чтение страницы» (`read_web_page`) и «Суммарного ответа»
в режиме `depth="pages"`.

Защита: запрашиваются только http/https-адреса, которые разрешаются в
публичные IP (не localhost, не частные сети) — в том числе после каждого
редиректа. Иначе ссылка из выдачи могла бы заставить сервер обратиться к
внутренним сервисам.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

#: Сколько текста страницы передавать модели.
MAX_PAGE_CHARS = 6000
#: Сколько байт страницы вообще читать.
MAX_DOWNLOAD_BYTES = 2_000_000
#: Ссылки: не больше стольких, и только с текстом не короче (отсекает меню).
MAX_LINKS = 40
MIN_LINK_TEXT = 20

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer", "form", "button", "select"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "header",
}


class PageFetchError(Exception):
    pass


@dataclass
class WebPage:
    url: str
    title: str
    text: str
    #: Ссылки со страницы: [{"text", "url"}] — например, на статьи с главной.
    links: List[Dict[str, str]] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: List[str] = []
        self.title = ""
        self._in_title = False
        #: [(href, текст ссылки)]
        self.links: List[List[str]] = []
        self._link: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and not self._skip_depth:
            href = dict(attrs).get("href")
            if href:
                self._link = [href, ""]
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._link is not None:
            self.links.append(self._link)
            # Заголовки-ссылки (списки новостей) — каждая с новой строки.
            self._parts.append("\n" if len(self._link[1].strip()) >= MIN_LINK_TEXT else " ")
            self._link = None
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._parts.append(data)
            if self._link is not None:
                self._link[1] += data

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    return parse_html(html, "").text


def parse_html(html: str, base_url: str) -> WebPage:
    """Заголовок, читаемый текст и содержательные ссылки страницы."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    links, seen = [], {urldefrag(base_url)[0]}
    for href, text in parser.links:
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < MIN_LINK_TEXT:
            continue
        absolute = urldefrag(urljoin(base_url, href.strip()))[0]
        if urlsplit(absolute).scheme not in ("http", "https") or absolute in seen:
            continue
        seen.add(absolute)
        links.append({"text": text[:200], "url": absolute})
        if len(links) >= MAX_LINKS:
            break
    return WebPage(url=base_url, title=re.sub(r"\s+", " ", parser.title).strip(), text=parser.text(), links=links)


def normalize_url(url: str) -> str:
    """«rbc.ru» -> «https://rbc.ru»; схема допускается только http/https."""
    url = (url or "").strip()
    if not url:
        raise PageFetchError("не указан адрес страницы")
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    if urlsplit(url).scheme not in ("http", "https"):
        raise PageFetchError(f"недопустимый адрес {url!r}: поддерживаются только http и https")
    return url


async def _ensure_public(url: httpx.URL) -> None:
    if url.scheme not in ("http", "https"):
        raise PageFetchError(f"недопустимая схема адреса: {url.scheme}")
    host = url.host
    if not host:
        raise PageFetchError("в адресе нет хоста")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PageFetchError(f"не удалось разрешить {host}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PageFetchError(f"адрес {host} ведёт во внутреннюю сеть ({ip}) — загрузка запрещена")


async def fetch_page(url: str, timeout: float = 15.0, client: Optional[httpx.AsyncClient] = None) -> WebPage:
    """Страница целиком: итоговый адрес (после редиректов), заголовок, текст,
    ссылки. Поднимает `PageFetchError`."""
    url = normalize_url(url)

    async def guard(request: httpx.Request) -> None:
        await _ensure_public(request.url)

    own = client is None
    client = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, max_redirects=5,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AgentsMCP/1.0)"},
        event_hooks={"request": [guard]},
    )
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise PageFetchError(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "")
            if ctype and "html" not in ctype and "text" not in ctype:
                raise PageFetchError(f"не текстовая страница ({ctype})")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_DOWNLOAD_BYTES:
                    break
            encoding = resp.encoding or "utf-8"
            final_url = str(resp.url)
    except httpx.HTTPError as exc:
        raise PageFetchError(f"сетевая ошибка: {exc}") from exc
    finally:
        if own:
            await client.aclose()
    html = b"".join(chunks).decode(encoding, errors="replace")
    if "<" not in html:
        return WebPage(url=final_url, title="", text=html.strip())
    return parse_html(html, final_url)

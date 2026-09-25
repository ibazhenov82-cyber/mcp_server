"""
mcp_server.features
=====================

Включаемые настройкой группы инструментов: пока настройка не задана,
инструменты группы не попадают в список доступных — ни агенту (MCP
`tools/list`), ни приложению (`GET /api/tools`).

- `local_git` (`MCP_LOCAL_GIT_ENABLED`) — «Локальный GIT»: `execute_git_command`, `git_pull`.
- `http_fetch` (`MCP_HTTP_FETCH_ENABLED`) — «HTTP-запросы»: `http_fetch`.
- `web_search` (`MCP_WEB_SEARCH_ENABLED`) — «Интернет поиск»: `duckduckgo_search`, `read_web_page`.
- `llm` (`MCP_LLM_ENABLED` и `MCP_LLM_API_KEY`) — «Обработка LLM»: `summarize`.
- `files` (`MCP_FILES_ENABLED`) — «Работа с файлами»: `save_to_text_file`.

Всегда доступны: Git-хостинги (`git_host_*`) и «Пайплайны» (`run_pipeline`).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import MCPConfig


@dataclass(frozen=True)
class Features:
    local_git: bool = False
    http_fetch: bool = False
    web_search: bool = False
    llm: bool = False
    files: bool = False

    @classmethod
    def from_config(cls) -> "Features":
        return cls(
            local_git=MCPConfig.LOCAL_GIT_ENABLED,
            http_fetch=MCPConfig.HTTP_FETCH_ENABLED,
            web_search=MCPConfig.WEB_SEARCH_ENABLED,
            llm=MCPConfig.LLM_ENABLED and bool(MCPConfig.LLM_API_KEY),
            files=MCPConfig.FILES_ENABLED,
        )

    @classmethod
    def all_enabled(cls) -> "Features":
        return cls(local_git=True, http_fetch=True, web_search=True, llm=True, files=True)

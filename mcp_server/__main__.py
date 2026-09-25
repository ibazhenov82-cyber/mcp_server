"""
Точка входа: `python -m mcp_server`.

Запускает процесс, обслуживающий MCP-протокол (Streamable HTTP, `/mcp` —
для AgentsCore и планировщика) и REST API (`/api` — для AgentsApp). Своего
состояния у сервера нет, его можно запускать в нескольких экземплярах.

Требует `fastapi`+`uvicorn` (см. requirements.txt) — недоступны в
песочнице разработки, поэтому этот модуль не покрыт тестами напрямую;
вся логика, которую он запускает, протестирована в `tests/`.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .app import create_app
from .config import DOTENV_PATH, MCPConfig


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, MCPConfig.LOG_LEVEL.upper(), logging.INFO),
        filename=MCPConfig.LOG_FILE or None,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import uvicorn

    app = create_app()

    def _onoff(flag: bool) -> str:
        return "включено" if flag else "выключено"

    features = app.state.features
    print(f"[mcp_server] Настройки: {DOTENV_PATH or 'файл .env не найден в ' + os.getcwd()}")
    print(f"[mcp_server] Локальный GIT (MCP_LOCAL_GIT_ENABLED): {_onoff(features.local_git)}")
    print(f"[mcp_server] HTTP-запросы (MCP_HTTP_FETCH_ENABLED): {_onoff(features.http_fetch)}")
    print(f"[mcp_server] Интернет поиск (MCP_WEB_SEARCH_ENABLED): {_onoff(features.web_search)}")
    llm_note = ""
    if MCPConfig.LLM_ENABLED and not MCPConfig.LLM_API_KEY:
        llm_note = " — не задан MCP_LLM_API_KEY"
    elif features.llm:
        llm_note = f" ({MCPConfig.LLM_MODEL})"
    print(f"[mcp_server] Обработка LLM (MCP_LLM_ENABLED): {_onoff(features.llm)}{llm_note}")
    files_note = f" (каталог {os.path.abspath(MCPConfig.FILES_DIR)})" if features.files else ""
    print(f"[mcp_server] Работа с файлами (MCP_FILES_ENABLED): {_onoff(features.files)}{files_note}")
    tools = asyncio.run(app.state.mcp.list_tools())
    print(f"[mcp_server] Инструменты ({len(tools)}): {', '.join(t.name for t in tools)}")
    uvicorn.run(app, host=MCPConfig.HOST, port=MCPConfig.PORT)


if __name__ == "__main__":
    main()

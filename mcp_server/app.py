"""
mcp_server.app
=================

Сборка FastAPI-приложения: REST API (`/api`) для AgentsApp плюс
MCP-протокол (Streamable HTTP, `/mcp`) для AgentsCore и планировщика.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .api import router as api_router
from .features import Features
from .git_hosts import close_all_clients
from .server import build_mcp_server, build_pipeline_tools

DESCRIPTION = """
MCP-сервер инструментов: Git-хостинги (GitHub/GitLab/Gitea), цепочки
инструментов (`run_pipeline`), а также — если включены настройками —
локальный Git, HTTP-запросы, поиск в DuckDuckGo, суммарный ответ LLM и
сохранение в текстовые файлы.

MCP-протокол (Streamable HTTP) — `/mcp`: им пользуются AgentsCore и
планировщик. REST API — `/api`: им пользуется AgentsApp (список
инструментов, статус Git-хостингов).
"""


def create_app(features: Optional[Features] = None) -> FastAPI:
    """Сборка приложения — то, что запускает `mcp_server.__main__`, и тесты
    (с явным `features`). Своей базы у сервера нет."""
    features = features if features is not None else Features.from_config()
    pipeline_tools = build_pipeline_tools(features)
    mcp = build_mcp_server(features, pipeline_tools=pipeline_tools)
    # `streamable_http_app()` лениво создаёт `mcp.session_manager` — вызываем
    # один раз: смонтируем результат и возьмём тот же `session_manager` для lifespan.
    mcp_asgi_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            # Вложенному (смонтированному) Starlette-приложению lifespan не
            # передаётся — `session_manager.run()` запускаем явно, иначе
            # любой запрос к /mcp падает с «Task group is not initialized»
            # (см. README, «Устранение неполадок»).
            await stack.enter_async_context(mcp.session_manager.run())
            try:
                yield
            finally:
                await close_all_clients()

    app = FastAPI(title="MCP Server", description=DESCRIPTION, lifespan=lifespan)
    app.state.mcp = mcp
    app.state.features = features
    # Каталог файлов «Работы с файлами» — для просмотра в приложении (/api/files).
    app.state.files = pipeline_tools.files

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        # Приложение показывает ошибки в формате {"error": "..."}.
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})

    app.include_router(api_router)
    # Монтируется под КОРНЕМ ("/"), а не под "/mcp": сам `mcp_asgi_app`
    # (Starlette-приложение из `streamable_http_app()`) уже регистрирует
    # СВОЙ единственный маршрут под `/mcp` (умолчание `streamable_http_path`
    # в FastMCP) — если смонтировать его ещё и под внешним "/mcp", реально
    # отвечающий путь задваивается в "/mcp/mcp", а документированный адрес
    # "/mcp" отвечает 404 (баг, найденный на практике). `/api/*`-маршруты
    # выше зарегистрированы ДО этого
    # монтирования и потому проверяются раньше — с корневым mount не
    # конфликтуют.
    app.mount("/", mcp_asgi_app)

    return app

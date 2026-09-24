"""
mcp_server.app
=================

Сборка FastAPI-приложения: REST API для AgentsApp (Часть 2 ТЗ, `api.py`)
плюс MCP-сервер (Streamable HTTP, `mcp.server.fastmcp.FastMCP.
streamable_http_app()`) для AgentsCore, смонтированный под `/mcp` — стиль
сборки как в `agents_core.api.app.create_app`, только меньше: один роутер,
один под-app.

Требует `fastapi` — недоступен в песочнице разработки (см. README).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router as api_router
from .config import MCPConfig
from .db import Database
from .git_hosts import close_all_clients
from .scheduler import SchedulerNotAvailableError, SchedulerService
from .server import build_mcp_server
from .store import NotFoundError, SchedulerStore, ValidationError

DESCRIPTION = """
MCP-сервер — отдельный сервис для регистрации периодических инструментов
(git_pull/http_fetch/git_host_poll), их расписания и истории запусков, а
также опроса публичных Git-хостингов (GitHub/GitLab/Gitea).

MCP-протокол (Streamable HTTP) смонтирован под `/mcp` — им пользуется
AgentsCore как MCP-клиент. REST API под `/api` — им пользуется AgentsApp
напрямую (список инструментов, расписание, история запусков, статус
Git-хостингов), в обход AgentsCore.
"""


def create_app(db_path: str = None) -> FastAPI:
    """Полная сборка с настоящей БД (согласно `MCPConfig`, если `db_path`
    не передан явно) — то, что запускает `mcp_server.__main__`."""
    db = Database(db_path or MCPConfig.DB_PATH)
    store = SchedulerStore(db)
    return create_app_with_store(store)


def create_app_with_store(store: SchedulerStore, start_scheduler: bool = True) -> FastAPI:
    """Сборка вокруг уже готового `SchedulerStore` — используется
    `create_app()`, а также тестами (с `start_scheduler=False`, если
    `apscheduler` недоступен в окружении)."""
    scheduler = SchedulerService(store) if start_scheduler else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scheduler is not None:
            try:
                scheduler.start()
            except SchedulerNotAvailableError:
                # Тот же принцип терпимости к окружению, что и в
                # `agents_core`: сервис не должен падать целиком только
                # из-за отсутствия одной необязательной для REST-слоя
                # библиотеки — просто периодические задачи не будут
                # реально срабатывать, пока apscheduler не установлен.
                app.state.scheduler = None
        yield
        if scheduler is not None:
            scheduler.stop()
        await close_all_clients()

    app = FastAPI(title="MCP Server", description=DESCRIPTION, version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.mcp = build_mcp_server(store, scheduler)

    app.include_router(api_router)
    app.mount("/mcp", app.state.mcp.streamable_http_app())

    @app.exception_handler(NotFoundError)
    def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(ValidationError)
    def _validation(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return app

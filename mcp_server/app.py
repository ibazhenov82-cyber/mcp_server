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

import contextlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router as api_router
from .config import MCPConfig
from .db import Database
from .features import FeatureDisabledError, Features
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
    features = Features.from_config()
    db = Database(db_path or MCPConfig.DB_PATH)
    store = SchedulerStore(db, allowed_actions=features.action_kinds)
    return create_app_with_store(store, features=features)


def create_app_with_store(
    store: SchedulerStore, start_scheduler: bool = True, features: Optional[Features] = None,
) -> FastAPI:
    """Сборка вокруг уже готового `SchedulerStore` — используется
    `create_app()`, а также тестами (с `start_scheduler=False`, если
    `apscheduler` недоступен в окружении). `features` — включаемые группы
    инструментов (см. `features.py`), по умолчанию — из `MCPConfig`; без
    `scheduling` планировщик не запускается вовсе."""
    features = features if features is not None else Features.from_config()
    scheduler = SchedulerService(store) if start_scheduler and features.scheduling else None
    mcp = build_mcp_server(store, scheduler, features)
    # `streamable_http_app()` ЛЕНИВО создаёт `mcp.session_manager` при первом
    # вызове (см. исходники `FastMCP`) — вызываем один раз здесь: смонтируем
    # результат ниже и заберём тот же самый `session_manager` для lifespan.
    mcp_asgi_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            # КРИТИЧНО и НЕ очевидно: у Starlette-приложения, которое
            # монтируется через `app.mount(...)`, собственный lifespan НЕ
            # запускается автоматически — ASGI lifespan-события доставляются
            # только приложению верхнего уровня, вложенные `Mount(...)` их не
            # получают (см. `starlette.routing.Router.lifespan` — она входит
            # только в lifespan САМОГО приложения, а не его смонтированных
            # под-приложений). А `streamable_http_app()` требует, чтобы был
            # запущен именно ЕЁ lifespan (`session_manager.run()` — заводит
            # task group для обработки запросов): без этого шага абсолютно
            # любой запрос к `/mcp` падает с
            # `RuntimeError: Task group is not initialized. Make sure to use run()`.
            # Это реальный баг, найденный на практике при первом живом
            # запуске (см. README, "Устранение неполадок") — тесты его не
            # ловили, потому что `fastapi`/полный HTTP-стек недоступны в
            # песочнице, где писался этот код (см. ниже, "Ограничения
            # окружения разработки"), и `/mcp` ни разу не был реально
            # опрошен по-настоящему. Приём — из докстринга самого
            # `FastMCP.session_manager`: "exposed to enable... mounting...
            # in a single FastAPI application".
            await stack.enter_async_context(mcp.session_manager.run())
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
            try:
                yield
            finally:
                if scheduler is not None:
                    scheduler.stop()
                await close_all_clients()

    app = FastAPI(title="MCP Server", description=DESCRIPTION, version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.mcp = mcp
    app.state.features = features

    app.include_router(api_router)
    # Монтируется под КОРНЕМ ("/"), а не под "/mcp": сам `mcp_asgi_app`
    # (Starlette-приложение из `streamable_http_app()`) уже регистрирует
    # СВОЙ единственный маршрут под `/mcp` (умолчание `streamable_http_path`
    # в FastMCP) — если смонтировать его ещё и под внешним "/mcp", реально
    # отвечающий путь задваивается в "/mcp/mcp", а документированный адрес
    # "/mcp" отвечает 404 (второй баг, найденный на практике вместе с
    # lifespan-багом выше). `/api/*`-маршруты выше зарегистрированы ДО этого
    # монтирования и потому проверяются раньше — с корневым mount не
    # конфликтуют.
    app.mount("/", mcp_asgi_app)

    @app.exception_handler(NotFoundError)
    def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(FeatureDisabledError)
    def _feature_disabled(request: Request, exc: FeatureDisabledError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(ValidationError)
    def _validation(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return app

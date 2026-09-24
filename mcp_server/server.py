"""
mcp_server.server
====================

`build_mcp_server()` — собирает `FastMCP` с инструментами из ТЗ. Вся
бизнес-логика — в `store.py`/`actions.py`/`git_hosts/*`; функции здесь —
тонкие обёртки: разбирают аргументы, вызывают нижний слой, ловят ожидаемые
исключения и возвращают структурированный `{"error": ...}` вместо того,
чтобы поднимать их наружу (FastMCP оборачивает необработанное исключение в
`ToolError` с потерей структуры — модели гораздо полезнее плоский dict,
который она может прочитать и решить, что делать дальше; см. аналогичное
решение в `agents_core.repository.Repository._execute_tool_call`).

Транспорт — ТОЛЬКО Streamable HTTP (см. `app.py`: `mcp.streamable_http_app()`
монтируется в FastAPI-приложение); stdio не реализуем (ТЗ)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP

from .actions import ActionError, execute_action, run_git_command
from .git_hosts import GitHostError, SUPPORTED_HOSTS, get_provider
from .models import ScheduledTool
from .scheduler import SchedulerService
from .store import NotFoundError, SchedulerStore, ValidationError

_EXPECTED_ERRORS = (NotFoundError, ValidationError, ActionError, GitHostError, ValueError)

#: git_host_* инструменты возвращают либо список нормализованных элементов
#: (коммиты/PR/issues/релизы), либо один dict (repo/file), либо
#: {"error": ...} — конкретный (не голый `Any`) return-аннотация нужна,
#: чтобы FastMCP построил structured output (`Any`/голый `list` без
#: параметра — не строит, см. тесты `test_server_tools.py`).
GitHostResult = Union[List[Dict[str, Any]], Dict[str, Any]]


def _tool_to_dict(tool: ScheduledTool) -> Dict[str, Any]:
    return {
        "id": tool.id, "name": tool.name, "description": tool.description,
        "action": tool.action, "schedule": tool.schedule, "params": tool.params,
        "input_schema": tool.input_schema, "enabled": tool.enabled,
        "created_at": tool.created_at, "last_run_at": tool.last_run_at, "next_run_at": tool.next_run_at,
    }


def build_mcp_server(store: SchedulerStore, scheduler: Optional[SchedulerService] = None) -> FastMCP:
    """`scheduler` необязателен (например, в тестах, где реальный
    `AsyncIOScheduler` недоступен, см. `tests/test_server_tools.py`) — без
    него зарегистрированная задача попадёт в БД и будет подхвачена при
    следующем запуске процесса с планировщиком, просто не встанет "на лету"
    в уже работающий job store текущего процесса."""
    # `stateless_http=True` — спецификация MCP 2026-07-28, требование ТЗ:
    # никакого хендшейка `initialize`/сессии между вызовами, каждый HTTP-запрос
    # самодостаточен (упрощает и AgentsCore как клиента — см.
    # `agents_core.mcp_client.MCPClient`: обычный синхронный POST на запрос,
    # без долгоживущего соединения). `json_response=True` — сервер отвечает
    # обычным JSON, а не SSE-потоком.
    mcp = FastMCP("mcp-server", stateless_http=True, json_response=True)

    @mcp.tool()
    async def register_scheduled_tool(
        name: str, schedule: str, action: str, params: Optional[Dict[str, Any]] = None,
        description: str = "", input_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Регистрирует новую периодическую задачу. `schedule` —
        'every:<N><s|m|h>' | 'daily:HH:MM' | 5-полевой cron. `action` —
        'git_pull' | 'http_fetch' | 'git_host_poll'."""
        try:
            tool = store.register_tool(
                name, action, schedule, description=description, params=params or {}, input_schema=input_schema,
            )
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}
        if scheduler is not None:
            scheduler.sync_job(tool)
        return _tool_to_dict(tool)

    @mcp.tool()
    async def list_scheduled_tools(status: Optional[str] = None) -> List[Dict[str, Any]]:
        """`status` — 'enabled' | 'disabled' | не задан (все)."""
        enabled = {"enabled": True, "disabled": False}.get(status) if status else None
        return [_tool_to_dict(t) for t in store.list_tools(enabled=enabled)]

    @mcp.tool()
    async def cancel_scheduled_tool(task_id: str) -> Dict[str, Any]:
        try:
            store.delete_tool(task_id)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}
        if scheduler is not None:
            scheduler.remove_job(task_id)
        return {"task_id": task_id, "cancelled": True}

    @mcp.tool()
    async def execute_git_command(repo_path: str, command: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        """Выполняет произвольную git-команду над локальной рабочей копией
        (`git -C repo_path <command> <args...>`) — НЕ через shell, но сама
        команда всё равно мощная (см. предупреждение о безопасности в
        README)."""
        try:
            return await run_git_command(repo_path, command, args)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def save_result(task_id: str, data: Any) -> Dict[str, Any]:
        """Сохраняет `data` как результат ПОСЛЕДНЕГО запуска периодической
        задачи `task_id` (см. `SchedulerStore.run_result`) — используется,
        когда данные для задачи посчитаны отдельно от обычного цикла
        планировщика."""
        try:
            return store.run_result(task_id, data)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    async def _git_host_call(host: str, coro_factory) -> GitHostResult:
        if host not in SUPPORTED_HOSTS:
            return {"error": f"Неизвестный host={host!r}: допустимо {SUPPORTED_HOSTS}"}
        try:
            provider = get_provider(host)
            return await coro_factory(provider)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def git_host_list_commits(
        host: str, owner: str, repo: str, since: Optional[str] = None, until: Optional[str] = None,
        branch: Optional[str] = None, limit: int = 50,
    ) -> GitHostResult:
        """`host` — 'github' | 'gitlab' | 'gitea'. `since`/`until` — ISO8601
        либо относительное значение вида '-1h'/'-1d'."""
        return await _git_host_call(host, lambda p: p.list_commits(owner, repo, since=since, until=until, branch=branch, limit=limit))

    @mcp.tool()
    async def git_host_list_pull_requests(
        host: str, owner: str, repo: str, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> GitHostResult:
        """`state` — 'open' | 'closed' | 'all'."""
        return await _git_host_call(host, lambda p: p.list_pull_requests(owner, repo, state=state, since=since, limit=limit))

    @mcp.tool()
    async def git_host_list_issues(
        host: str, owner: str, repo: str, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> GitHostResult:
        """`state` — 'open' | 'closed' | 'all'."""
        return await _git_host_call(host, lambda p: p.list_issues(owner, repo, state=state, since=since, limit=limit))

    @mcp.tool()
    async def git_host_list_releases(host: str, owner: str, repo: str, limit: int = 20) -> GitHostResult:
        return await _git_host_call(host, lambda p: p.list_releases(owner, repo, limit=limit))

    @mcp.tool()
    async def git_host_get_repo(host: str, owner: str, repo: str) -> GitHostResult:
        return await _git_host_call(host, lambda p: p.get_repo(owner, repo))

    @mcp.tool()
    async def git_host_get_file(host: str, owner: str, repo: str, path: str, ref: Optional[str] = None) -> GitHostResult:
        return await _git_host_call(host, lambda p: p.get_file(owner, repo, path, ref=ref))

    return mcp

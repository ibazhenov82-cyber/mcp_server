"""
mcp_server.api
=================

REST API для AgentsApp (Часть 2 ТЗ) — НЕ MCP-протокол, обычные
JSON-эндпоинты. Вся бизнес-логика — в `store.py`/`git_hosts/*`, здесь
только разбор запроса/сборка ответа (стиль `agents_core.api.*`). Требует
`fastapi` — недоступен в песочнице разработки, поэтому маршруты не
покрыты тестами напрямую здесь (см. README, "Ограничения окружения
разработки"); `store.py`/`git_hosts/*`, которые эти маршруты вызывают,
протестированы отдельно и полно."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from mcp.server.fastmcp import FastMCP

from .deps import get_features, get_mcp, get_scheduler, get_store, require_scheduling
from .features import Features
from .git_hosts import SUPPORTED_HOSTS, get_provider
from .models import ACTION_KINDS
from .scheduler import SchedulerService
from .schemas import (
    GitHostStatusOut,
    ScheduledToolCreate,
    ScheduledToolOut,
    ScheduledToolPatch,
    ScheduledToolRunOut,
    StatusOut,
    ToolDescriptionOut,
)
from .store import SchedulerStore

router = APIRouter(prefix="/api", tags=["mcp"])

#: Каждый `action` из `models.ACTION_KINDS` — это единственный вид
#: "инструмента", который реально можно поставить на периодическое
#: расписание через `register_scheduled_tool`/`POST /api/scheduled-tools`
#: (остальные MCP-инструменты сервера — одноразовые вызовы, не для
#: расписания, см. докстринг `list_available_tools` ниже).
_ACTION_TITLES = {
    "git_pull": "Обновление локального репозитория",
    "http_fetch": "HTTP-запрос",
    "git_host_poll": "Опрос Git-хостинга",
}
_ACTION_DESCRIPTIONS = {
    "git_pull": "Обновить локальную рабочую копию git-репозитория (git pull).",
    "http_fetch": "Выполнить произвольный HTTP-запрос по расписанию.",
    "git_host_poll": "Опросить Git-хостинг (коммиты/PR/issues/релизы/файл) и сохранить результат.",
}
_ACTION_PARAMETER_SCHEMAS = {
    "git_pull": {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Путь к локальной рабочей копии"},
            "remote": {"type": "string", "default": "origin"},
            "branch": {"type": "string"},
        },
        "required": ["repo_path"],
    },
    "http_fetch": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "default": "GET"},
            "headers": {"type": "object"},
            "body": {"type": "object"},
            "timeout": {"type": "number", "default": 30},
        },
        "required": ["url"],
    },
    "git_host_poll": {
        "type": "object",
        "properties": {
            "host": {"type": "string", "enum": SUPPORTED_HOSTS},
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "resource": {
                "type": "string",
                "enum": ["commits", "pull_requests", "issues", "releases", "repo", "file"],
                "default": "commits",
            },
            "since": {"type": "string", "description": "ISO8601 либо '-1h'/'-1d'"},
            "until": {"type": "string"},
            "branch": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "all"},
            "limit": {"type": "integer", "default": 50},
            "path": {"type": "string", "description": "Только для resource='file'"},
            "ref": {"type": "string", "description": "Только для resource='file'"},
        },
        "required": ["host", "owner", "repo"],
    },
}


async def list_available_tools(mcp: FastMCP, features: Optional[Features] = None) -> List[ToolDescriptionOut]:
    """`schedulable=True` — только сами `action` (см. `_ACTION_*` выше);
    остальные MCP-инструменты сервера (`execute_git_command`, `save_result`,
    `git_host_get_repo`/`get_file`, `list_scheduled_tools`,
    `cancel_scheduled_tool`, `register_scheduled_tool` — введён отдельным
    REST-эндпоинтом, а не через список инструментов) — одноразовые вызовы
    для модели через AgentsCore, не заводятся на расписание."""
    # Виды задач — только включённые настройками (см. `features.py`): без
    # MCP_SCHEDULER_ENABLED их нет вовсе, без MCP_LOCAL_GIT_ENABLED нет
    # git_pull. MCP-инструменты ниже уже отфильтрованы при регистрации
    # (`build_mcp_server`).
    features = features if features is not None else Features.from_config()
    items = [
        ToolDescriptionOut(
            name=action, title=_ACTION_TITLES[action], description=_ACTION_DESCRIPTIONS[action],
            parameters=_ACTION_PARAMETER_SCHEMAS[action], schedulable=True,
        )
        for action in features.action_kinds
    ]
    mcp_tools = await mcp.list_tools()
    for tool in mcp_tools:
        if tool.name == "register_scheduled_tool":
            continue  # заведение задачи — отдельный REST-эндпоинт (POST /api/scheduled-tools)
        items.append(ToolDescriptionOut(
            name=tool.name, title=tool.title or "", description=tool.description or "",
            parameters=tool.inputSchema or {}, schedulable=False,
        ))
    return items


def _tool_out(tool) -> ScheduledToolOut:
    return ScheduledToolOut(
        id=tool.id, name=tool.name, description=tool.description, action=tool.action,
        schedule=tool.schedule, params=tool.params, input_schema=tool.input_schema,
        enabled=tool.enabled, created_at=tool.created_at, last_run_at=tool.last_run_at,
        next_run_at=tool.next_run_at,
    )


def _run_out(run) -> ScheduledToolRunOut:
    return ScheduledToolRunOut(
        id=run.id, tool_id=run.tool_id, started_at=run.started_at, finished_at=run.finished_at,
        status=run.status, result=run.result, error=run.error,
    )


@router.get("/status", response_model=StatusOut)
async def get_status(
    store: SchedulerStore = Depends(get_store), scheduler: Optional[SchedulerService] = Depends(get_scheduler),
    mcp: FastMCP = Depends(get_mcp), features: Features = Depends(get_features),
) -> StatusOut:
    tools = await mcp.list_tools()
    return StatusOut(
        scheduler_running=bool(scheduler and scheduler.running),
        tool_count=len(tools),
        scheduled_count=len(store.list_tools(enabled=True)) if features.scheduling else 0,
        scheduling_enabled=features.scheduling,
        local_git_enabled=features.local_git,
    )


@router.get("/tools", response_model=List[ToolDescriptionOut])
async def get_tools(
    mcp: FastMCP = Depends(get_mcp), features: Features = Depends(get_features),
) -> List[ToolDescriptionOut]:
    return await list_available_tools(mcp, features)


@router.get("/scheduled-tools", response_model=List[ScheduledToolOut], dependencies=[Depends(require_scheduling)])
def list_scheduled_tools_route(store: SchedulerStore = Depends(get_store)) -> List[ScheduledToolOut]:
    return [_tool_out(t) for t in store.list_tools()]


@router.post("/scheduled-tools", response_model=ScheduledToolOut, status_code=201, dependencies=[Depends(require_scheduling)])
def create_scheduled_tool_route(
    payload: ScheduledToolCreate, store: SchedulerStore = Depends(get_store),
    scheduler: Optional[SchedulerService] = Depends(get_scheduler),
) -> ScheduledToolOut:
    tool = store.register_tool(
        payload.name, payload.action, payload.schedule, description=payload.description,
        params=payload.params, input_schema=payload.input_schema, enabled=payload.enabled,
    )
    if scheduler is not None:
        scheduler.sync_job(tool)
    return _tool_out(tool)


@router.patch("/scheduled-tools/{tool_id}", response_model=ScheduledToolOut, dependencies=[Depends(require_scheduling)])
def update_scheduled_tool_route(
    tool_id: str, payload: ScheduledToolPatch, store: SchedulerStore = Depends(get_store),
    scheduler: Optional[SchedulerService] = Depends(get_scheduler),
) -> ScheduledToolOut:
    patch = payload.model_dump(exclude_unset=True)
    tool = store.update_tool(tool_id, patch)
    if scheduler is not None:
        scheduler.sync_job(tool)
    return _tool_out(tool)


@router.delete("/scheduled-tools/{tool_id}", status_code=204, dependencies=[Depends(require_scheduling)])
def delete_scheduled_tool_route(
    tool_id: str, store: SchedulerStore = Depends(get_store),
    scheduler: Optional[SchedulerService] = Depends(get_scheduler),
) -> None:
    store.delete_tool(tool_id)
    if scheduler is not None:
        scheduler.remove_job(tool_id)


@router.get("/scheduled-tools/{tool_id}/runs", response_model=List[ScheduledToolRunOut], dependencies=[Depends(require_scheduling)])
def list_runs_route(
    tool_id: str, limit: int = Query(50, ge=1, le=500), store: SchedulerStore = Depends(get_store),
) -> List[ScheduledToolRunOut]:
    return [_run_out(r) for r in store.list_runs(tool_id, limit=limit)]


@router.get("/git-hosts", response_model=List[GitHostStatusOut])
async def list_git_hosts() -> List[GitHostStatusOut]:
    statuses = []
    for host in SUPPORTED_HOSTS:
        provider = get_provider(host)
        try:
            status = await provider.get_status()
            statuses.append(GitHostStatusOut(
                host=status.host, configured=status.configured, api_url=status.api_url,
                rate_limit_remaining=status.rate_limit_remaining, rate_limit_reset_at=status.rate_limit_reset_at,
            ))
        except Exception:
            # Хостинг недоступен/сеть упала — статус всё равно должен
            # вернуться (просто без данных о лимите), а не завалить весь
            # список остальных хостингов.
            statuses.append(GitHostStatusOut(
                host=host, configured=provider.configured, api_url=provider.api_url,
            ))
    return statuses

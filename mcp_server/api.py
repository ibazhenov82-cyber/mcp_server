"""
mcp_server.api
=================

REST API для AgentsApp (`/api`) — обычные JSON-эндпоинты, не MCP:
состояние сервера, список инструментов, статус Git-хостингов.
Периодические задачи — в сервисе планировщика (scheduler_service)."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mcp.server.fastmcp import FastMCP

from .deps import get_features, get_mcp
from .features import Features
from .git_hosts import SUPPORTED_HOSTS, get_provider
from .config import MCPConfig
from .files import FileStore, FileToolError
from .schemas import FileContentOut, FileInfoOut, GitHostStatusOut, StatusOut, ToolDescriptionOut
from .server import GROUP_META_KEY

router = APIRouter(prefix="/api", tags=["mcp"])


async def list_available_tools(mcp: FastMCP) -> List[ToolDescriptionOut]:
    """Инструменты, зарегистрированные на сервере (выключенные настройками
    группы сюда не попадают — они не регистрируются вовсе, см. `server.py`)."""
    return [
        ToolDescriptionOut(
            name=tool.name, title=tool.title or "", description=tool.description or "",
            parameters=tool.inputSchema or {},
            group=(tool.meta or {}).get(GROUP_META_KEY, ""),
        )
        for tool in await mcp.list_tools()
    ]


@router.get("/status", response_model=StatusOut)
async def get_status(mcp: FastMCP = Depends(get_mcp), features: Features = Depends(get_features)) -> StatusOut:
    return StatusOut(
        tool_count=len(await mcp.list_tools()),
        local_git_enabled=features.local_git,
        http_fetch_enabled=features.http_fetch,
        web_search_enabled=features.web_search,
        llm_enabled=features.llm,
        llm_model=MCPConfig.LLM_MODEL if features.llm else "",
        files_enabled=features.files,
    )


def _files(request: Request) -> FileStore:
    files: Optional[FileStore] = getattr(request.app.state, "files", None)
    if files is None:
        raise HTTPException(status_code=404, detail="работа с файлами выключена (MCP_FILES_ENABLED)")
    return files


@router.get("/files", response_model=List[FileInfoOut])
def list_files(request: Request) -> List[FileInfoOut]:
    """Файлы, сохранённые инструментом «Сохранить в текстовый файл», новые сверху."""
    return [FileInfoOut(**f) for f in _files(request).list()]


@router.get("/files/content", response_model=FileContentOut)
def read_file(request: Request, path: str = Query(..., description="Путь из GET /api/files")) -> FileContentOut:
    try:
        return FileContentOut(**_files(request).read(path))
    except FileToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"файл {path} не найден") from exc


@router.get("/tools", response_model=List[ToolDescriptionOut])
async def get_tools(mcp: FastMCP = Depends(get_mcp)) -> List[ToolDescriptionOut]:
    return await list_available_tools(mcp)


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

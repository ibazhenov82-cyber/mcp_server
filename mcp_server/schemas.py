"""
mcp_server.schemas
=====================

Pydantic-схемы REST API для AgentsApp (стиль `agents_core.schemas`) — Часть
2 ТЗ. Отдельно от MCP-инструментов (`server.py`), у которых свой формат
(структурированный dict, а не response_model FastAPI)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .models import ACTION_KINDS


class StatusOut(BaseModel):
    scheduler_running: bool
    tool_count: int
    scheduled_count: int = Field(..., description="Число ВКЛЮЧЁННЫХ периодических задач")


class ToolDescriptionOut(BaseModel):
    """Один доступный MCP-инструмент — для экрана «MCP» (список
    инструментов) и для формы «Добавить задачу» (какие из них
    `schedulable`)."""

    name: str
    description: str
    parameters: Dict[str, Any] = Field(default_factory=dict, description="JSON Schema параметров")
    schedulable: bool = Field(..., description="Можно ли завести на него периодическую задачу (action)")


class ScheduledToolOut(BaseModel):
    id: str
    name: str
    description: str
    action: str = Field(..., description=f"Одна из: {ACTION_KINDS}")
    schedule: str = Field(..., description="'every:<N><s|m|h>' | 'daily:HH:MM' | 5-полевой cron")
    params: Dict[str, Any] = Field(default_factory=dict)
    input_schema: Optional[Dict[str, Any]] = None
    enabled: bool
    created_at: int
    last_run_at: Optional[int] = None
    next_run_at: Optional[int] = None


class ScheduledToolCreate(BaseModel):
    name: str = Field(..., min_length=1)
    action: str = Field(..., description=f"Одна из: {ACTION_KINDS}")
    schedule: str
    description: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)
    input_schema: Optional[Dict[str, Any]] = None
    enabled: bool = True


class ScheduledToolPatch(BaseModel):
    name: Optional[str] = None
    action: Optional[str] = None
    schedule: Optional[str] = None
    description: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    input_schema: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class ScheduledToolRunOut(BaseModel):
    id: str
    tool_id: str
    started_at: int
    finished_at: Optional[int] = None
    status: str = Field(..., description="'running' | 'success' | 'error'")
    result: Optional[Any] = None
    error: Optional[str] = None


class GitHostStatusOut(BaseModel):
    host: str
    configured: bool
    api_url: str
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset_at: Optional[int] = None


class ErrorOut(BaseModel):
    error: str

"""Зависимости FastAPI, общие для всех роутеров (стиль `agents_core.api.deps`)."""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from mcp.server.fastmcp import FastMCP

from .features import Features
from .scheduler import SchedulerService
from .store import SchedulerStore


def get_store(request: Request) -> SchedulerStore:
    return request.app.state.store


def get_scheduler(request: Request) -> Optional[SchedulerService]:
    return getattr(request.app.state, "scheduler", None)


def get_mcp(request: Request) -> FastMCP:
    return request.app.state.mcp


def get_features(request: Request) -> Features:
    return getattr(request.app.state, "features", None) or Features.from_config()


def require_scheduling(request: Request) -> None:
    """Зависимость для `/api/scheduled-tools*`: без MCP_SCHEDULER_ENABLED
    отвечают 409 `{"error": ...}` (см. обработчик в `app.py`)."""
    get_features(request).require_scheduling()

"""Зависимости FastAPI, общие для всех роутеров (стиль `agents_core.api.deps`)."""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from mcp.server.fastmcp import FastMCP

from .scheduler import SchedulerService
from .store import SchedulerStore


def get_store(request: Request) -> SchedulerStore:
    return request.app.state.store


def get_scheduler(request: Request) -> Optional[SchedulerService]:
    return getattr(request.app.state, "scheduler", None)


def get_mcp(request: Request) -> FastMCP:
    return request.app.state.mcp

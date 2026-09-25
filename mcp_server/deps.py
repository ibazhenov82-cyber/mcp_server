"""Зависимости FastAPI, общие для роутеров."""

from __future__ import annotations

from fastapi import Request
from mcp.server.fastmcp import FastMCP

from .features import Features


def get_mcp(request: Request) -> FastMCP:
    return request.app.state.mcp


def get_features(request: Request) -> Features:
    return getattr(request.app.state, "features", None) or Features.from_config()

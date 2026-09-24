"""
mcp_server.models
====================

Дата-классы хранимых сущностей (стиль `agents_core.models`) — только
данные, вся логика в `store.py`/`scheduler.py`/`git_hosts/*`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: `action` планируемого инструмента — см. `mcp_server.actions`.
ACTION_KINDS = ["git_pull", "http_fetch", "git_host_poll"]

#: Возможные статусы одного запуска (`scheduled_tool_runs.status`).
RUN_STATUSES = ["running", "success", "error"]


@dataclass
class ScheduledTool:
    id: str
    name: str
    description: str
    action: str  # один из ACTION_KINDS
    schedule: str  # "every:<N><s|m|h>" | "daily:HH:MM" | сырой cron "m h dom mon dow"
    params: Dict[str, Any] = field(default_factory=dict)
    input_schema: Optional[Dict[str, Any]] = None
    enabled: bool = True
    created_at: int = 0
    last_run_at: Optional[int] = None
    next_run_at: Optional[int] = None


@dataclass
class ScheduledToolRun:
    id: str
    tool_id: str
    started_at: int
    finished_at: Optional[int] = None
    status: str = "running"  # один из RUN_STATUSES
    result: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class GitHostStatus:
    host: str
    configured: bool
    api_url: str
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset_at: Optional[int] = None

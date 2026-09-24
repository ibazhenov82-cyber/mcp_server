"""
mcp_server.features
=====================

Включаемые настройкой группы возможностей сервера (по замечанию
пользователя): пока соответствующая настройка не задана, инструменты
группы НЕ попадают в список доступных — ни в MCP-протокол (`tools/list`,
им пользуется AgentsCore), ни в REST `GET /api/tools` (AgentsApp).

- `local_git` (`MCP_LOCAL_GIT_ENABLED`) — работа с ЛОКАЛЬНЫМИ рабочими
  копиями git на машине MCP-сервера: инструмент `execute_git_command` и
  вид периодической задачи `git_pull`.
- `scheduling` (`MCP_SCHEDULER_ENABLED`) — периодические задачи:
  инструменты `register_scheduled_tool`/`list_scheduled_tools`/
  `cancel_scheduled_tool`/`save_result`, REST `/api/scheduled-tools*`,
  виды задач (`action`) в `/api/tools` и сам планировщик (без этой
  настройки он не запускается — уже заведённые задачи хранятся, но не
  срабатывают).

Инструменты Git-хостингов (`git_host_*`) — публичные HTTP API
GitHub/GitLab/Gitea — доступны всегда.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .config import MCPConfig
from .models import ACTION_KINDS

#: Виды периодических задач, которым нужен локальный Git.
LOCAL_GIT_ACTIONS = frozenset({"git_pull"})


class FeatureDisabledError(RuntimeError):
    """Обращение к выключенной настройкой возможности (REST отвечает 409)."""


@dataclass(frozen=True)
class Features:
    local_git: bool = False
    scheduling: bool = False

    @classmethod
    def from_config(cls) -> "Features":
        return cls(local_git=MCPConfig.LOCAL_GIT_ENABLED, scheduling=MCPConfig.SCHEDULER_ENABLED)

    @classmethod
    def all_enabled(cls) -> "Features":
        return cls(local_git=True, scheduling=True)

    @property
    def action_kinds(self) -> List[str]:
        """Виды периодических задач, доступные на этом сервере: пусто без
        `scheduling`; `git_pull` — только вместе с `local_git`."""
        if not self.scheduling:
            return []
        return [a for a in ACTION_KINDS if self.local_git or a not in LOCAL_GIT_ACTIONS]

    def require_scheduling(self) -> None:
        if not self.scheduling:
            raise FeatureDisabledError(
                "Периодические задачи выключены на MCP-сервере (настройка MCP_SCHEDULER_ENABLED)"
            )

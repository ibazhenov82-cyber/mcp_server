"""
mcp_server.schemas
=====================

Pydantic-схемы REST API для AgentsApp (`/api`). У MCP-инструментов
(`server.py`) свой формат — структурированный dict.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class StatusOut(BaseModel):
    tool_count: int
    local_git_enabled: bool = Field(..., description="Включена ли работа с локальным Git (MCP_LOCAL_GIT_ENABLED)")
    http_fetch_enabled: bool = Field(..., description="Включён ли инструмент http_fetch (MCP_HTTP_FETCH_ENABLED)")
    web_search_enabled: bool = Field(False, description="Интернет поиск: DuckDuckGo и чтение страниц (MCP_WEB_SEARCH_ENABLED)")
    llm_enabled: bool = Field(False, description="Суммарный ответ LLM (MCP_LLM_ENABLED и MCP_LLM_API_KEY)")
    llm_model: str = Field("", description="Модель суммарного ответа")
    files_enabled: bool = Field(False, description="Сохранение в текстовые файлы (MCP_FILES_ENABLED)")


class FileInfoOut(BaseModel):
    path: str = Field(..., description="Путь относительно каталога файлов")
    bytes: int
    modified_at: int = Field(..., description="Время изменения, Unix-секунды")


class FileContentOut(FileInfoOut):
    sha256: str
    content: str


class ToolDescriptionOut(BaseModel):
    """Один доступный MCP-инструмент — для экрана «MCP-сервер»."""

    name: str
    title: str = Field("", description="Краткое русское описание (например, «Получение информации о репозитории»)")
    description: str
    parameters: Dict[str, Any] = Field(default_factory=dict, description="JSON Schema параметров")
    group: str = Field("", description="Группа инструмента: «GIT API», «Локальный GIT», «HTTP-запросы»")


class GitHostStatusOut(BaseModel):
    host: str
    configured: bool
    api_url: str
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset_at: Optional[int] = None


class ErrorOut(BaseModel):
    error: str

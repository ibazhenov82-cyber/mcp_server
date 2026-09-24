"""
mcp_server.config
===================

Настройки уровня сервиса (аналог `agents_core.config.AgentConfig`, тот же
стиль): адрес/порт HTTP-сервера, путь к собственной SQLite, токены
Git-хостингов. Всё считывается один раз из переменных окружения либо файла
`.env` рядом с местом запуска процесса.

MCP-сервер — САМОДОСТАТОЧНЫЙ отдельный сервис: не импортирует ничего из
`agents_core`, разворачивается отдельным процессом со своей БД.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _load_dotenv(path: str = ".env") -> None:
    """Минимальный загрузчик `.env` — тот же формат, что и в
    `agents_core.config._load_dotenv`: строки вида KEY=VALUE, комментарии
    через '#', уже существующие переменные окружения имеют приоритет."""
    file = Path(path)
    if not file.exists():
        return
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


class MCPConfig:
    """Читается один раз при импорте модуля."""

    HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("MCP_PORT", "8001"))
    DB_PATH: str = os.environ.get("AGENT_MCP_DB_PATH", "mcp_server.db")

    # Заготовка под будущую аутентификацию между сервисами (AgentsCore/
    # AgentsApp -> этот сервис) — пока не используется НИГДЕ в коде (см. ТЗ:
    # "аутентификация пока не вводится, но в конфиге закладывается пустое
    # поле"), чтобы включить её позже без переделки конфигурации на обеих
    # сторонах.
    API_KEY: str = os.environ.get("MCP_API_KEY", "").strip()

    # --- Git-хостинги -----------------------------------------------------
    # Токен не задан -> провайдер работает в анонимном режиме (публичные
    # репозитории, куда более жёсткие лимиты запросов) — это штатный режим,
    # не ошибка конфигурации.
    GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "").strip()
    GITHUB_API_URL: str = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    GITLAB_TOKEN: str = os.environ.get("GITLAB_TOKEN", "").strip()
    GITLAB_API_URL: str = os.environ.get("GITLAB_API_URL", "https://gitlab.com/api/v4").rstrip("/")

    GITEA_TOKEN: str = os.environ.get("GITEA_TOKEN", "").strip()
    GITEA_API_URL: str = os.environ.get("GITEA_API_URL", "https://gitea.com/api/v1").rstrip("/")

    # Таймаут HTTP-запросов к внешним Git-хостингам и внутри action
    # http_fetch (секунды).
    HTTP_TIMEOUT: float = float(os.environ.get("MCP_HTTP_TIMEOUT", "30") or "30")

    # Логирование — тот же формат уровня/файла, что и в AgentsCore
    # (agents_core.logging_setup), но настраивается независимо (отдельный
    # процесс, отдельная переменная окружения).
    LOG_LEVEL: str = os.environ.get("MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    LOG_FILE: str = os.environ.get("MCP_LOG_FILE", "").strip()

    @classmethod
    def is_git_host_configured(cls, host: str) -> bool:
        return bool(getattr(cls, f"{host.upper()}_TOKEN", ""))

    @classmethod
    def token_for(cls, host: str) -> Optional[str]:
        token = getattr(cls, f"{host.upper()}_TOKEN", "")
        return token or None

    @classmethod
    def api_url_for(cls, host: str) -> str:
        return getattr(cls, f"{host.upper()}_API_URL")

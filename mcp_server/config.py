"""
mcp_server.config
===================

Настройки уровня сервиса (аналог `agents_core.config.AgentConfig`, тот же
стиль): адрес/порт HTTP-сервера, включаемые группы инструментов, токены
Git-хостингов. Всё считывается один раз из переменных окружения либо файла
`.env` рядом с местом запуска процесса.

MCP-сервер — САМОДОСТАТОЧНЫЙ отдельный сервис: не импортирует ничего из
`agents_core`, разворачивается отдельным процессом. Своей базы нет: периодические задачи
живут в отдельном сервисе планировщика (scheduler_service).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


#: Откуда загружен `.env` (None — файл не найден); печатается при старте.
DOTENV_PATH: Optional[Path] = None


def _load_dotenv(path: str = ".env") -> None:
    """Минимальный загрузчик `.env` — тот же формат, что и в
    `agents_core.config._load_dotenv`: строки вида KEY=VALUE, комментарии
    через '#', уже существующие переменные окружения имеют приоритет."""
    global DOTENV_PATH
    file = Path(path)
    if not file.exists():
        return
    DOTENV_PATH = file.resolve()
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


class MCPConfig:
    """Читается один раз при импорте модуля."""

    # --- Включаемые возможности (см. `features.py`) -------------------------
    # Не заданы -> выключены: инструменты группы не попадают в список
    # доступных ни AgentsCore (MCP tools/list), ни AgentsApp (/api/tools).
    LOCAL_GIT_ENABLED: bool = _bool_env("MCP_LOCAL_GIT_ENABLED")
    HTTP_FETCH_ENABLED: bool = _bool_env("MCP_HTTP_FETCH_ENABLED")
    # Интернет поиск (DuckDuckGo через ddgs + чтение страниц), суммарный
    # ответ LLM, сохранение в текстовые файлы — общие инструменты пайплайнов
    # (см. features.py).
    WEB_SEARCH_ENABLED: bool = _bool_env("MCP_WEB_SEARCH_ENABLED")
    LLM_ENABLED: bool = _bool_env("MCP_LLM_ENABLED")
    FILES_ENABLED: bool = _bool_env("MCP_FILES_ENABLED")

    # --- Интернет поиск: DuckDuckGo -----------------------------------------
    # Поисковик(и) внутри ddgs: "duckduckgo" — только DuckDuckGo; можно
    # перечислить через запятую ("duckduckgo,brave") или "auto".
    DDGS_BACKEND: str = os.environ.get("MCP_DDGS_BACKEND", "duckduckgo").strip() or "duckduckgo"
    DDGS_REGION: str = os.environ.get("MCP_DDGS_REGION", "ru-ru").strip() or "ru-ru"
    DDGS_TIMEOUT: float = float(os.environ.get("MCP_DDGS_TIMEOUT", "15") or "15")

    # --- LLM для «Суммарного ответа» (OpenAI-совместимый API) ---------------
    LLM_BASE_URL: str = os.environ.get("MCP_LLM_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    LLM_API_KEY: str = os.environ.get("MCP_LLM_API_KEY", "").strip()
    LLM_MODEL: str = os.environ.get("MCP_LLM_MODEL", "deepseek-v4-flash").strip()
    LLM_TIMEOUT: float = float(os.environ.get("MCP_LLM_TIMEOUT", "120") or "120")
    LLM_MAX_TOKENS: int = int(os.environ.get("MCP_LLM_MAX_TOKENS", "2000") or "2000")

    # --- Файлы и промежуточные результаты -----------------------------------
    # Каталог, куда пишет save_to_text_file (за его пределы запись невозможна).
    FILES_DIR: str = os.environ.get("MCP_FILES_DIR", "mcp_files").strip() or "mcp_files"
    # Промежуточные результаты шагов (result_id) и сколько их хранить.
    DATA_DIR: str = os.environ.get("MCP_DATA_DIR", "mcp_data").strip() or "mcp_data"
    RESULT_TTL_HOURS: float = float(os.environ.get("MCP_RESULT_TTL_HOURS", "24") or "24")

    HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("MCP_PORT", "8001"))

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

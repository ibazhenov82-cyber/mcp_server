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
from typing import List, Optional, Tuple
from urllib.parse import urlparse


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


def _list_env(name: str) -> List[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",") if p.strip()]


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

    # --- Репозиторий (ассистент разработчика) --------------------------------
    # Группа «Репозиторий»: repo_open (клон по URL или локальная копия) и
    # чтение/коммит рабочих копий. Клоны по URL — в MCP_REPOS_DIR (по
    # умолчанию MCP_DATA_DIR/repos), локальные проекты — только внутри
    # MCP_PROJECT_ROOTS (через запятую или «;»).
    REPOS_ENABLED: bool = _bool_env("MCP_REPOS_ENABLED")
    REPOS_DIR: str = os.environ.get("MCP_REPOS_DIR", "").strip()
    PROJECT_ROOTS: List[str] = _list_env("MCP_PROJECT_ROOTS")
    # Глубина клона (коммитов истории); 0 — полная история.
    REPO_CLONE_DEPTH: int = int(os.environ.get("MCP_REPO_CLONE_DEPTH", "200") or "200")
    # Не чаще, чем раз в столько секунд, клон обновляется (git fetch) при repo_open.
    REPO_FETCH_INTERVAL: float = float(os.environ.get("MCP_REPO_FETCH_INTERVAL", "60") or "60")
    REPO_CLONE_TIMEOUT: float = float(os.environ.get("MCP_REPO_CLONE_TIMEOUT", "300") or "300")
    # С каких хостов можно клонировать: пусто — github.com, gitlab.com,
    # gitea.com и хосты из *_API_URL; «*» — любые.
    REPO_ALLOWED_HOSTS: List[str] = _list_env("MCP_REPO_ALLOWED_HOSTS")

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

    @classmethod
    def repos_dir(cls) -> str:
        return cls.REPOS_DIR or os.path.join(cls.DATA_DIR, "repos")

    @classmethod
    def _web_host(cls, host: str) -> str:
        api = (urlparse(getattr(cls, f"{host.upper()}_API_URL")).hostname or "").lower()
        return "github.com" if api == "api.github.com" else api

    @classmethod
    def repo_allowed_hosts(cls) -> List[str]:
        if cls.REPO_ALLOWED_HOSTS:
            return [h.lower() for h in cls.REPO_ALLOWED_HOSTS]
        hosts = {"github.com", "gitlab.com", "gitea.com"}
        hosts.update(h for h in (cls._web_host(x) for x in ("github", "gitlab", "gitea")) if h)
        return sorted(hosts)

    @classmethod
    def git_credentials(cls, host: str) -> Optional[Tuple[str, str]]:
        """Логин и токен для git по HTTPS: токены хостингов из этого же
        конфига (GITHUB_TOKEN, GITLAB_TOKEN, GITEA_TOKEN) — только для их хоста."""
        host = (host or "").lower()
        if cls.GITHUB_TOKEN and host == cls._web_host("github"):
            return "x-access-token", cls.GITHUB_TOKEN
        if cls.GITLAB_TOKEN and host == cls._web_host("gitlab"):
            return "oauth2", cls.GITLAB_TOKEN
        if cls.GITEA_TOKEN and host == cls._web_host("gitea"):
            # Gitea: токен в имени пользователя, пароль x-oauth-basic.
            return cls.GITEA_TOKEN, "x-oauth-basic"
        return None

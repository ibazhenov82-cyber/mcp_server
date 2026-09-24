"""
mcp_server.git_hosts
=======================

`get_provider(host)` — единая точка получения клиента конкретного
Git-хостинга, настроенного по `MCPConfig` (токен/URL). `host` ∈
{"github", "gitlab", "gitea"}.
"""

from __future__ import annotations

from typing import Dict, Optional

import httpx

from ..config import MCPConfig
from .base import GitHostError, GitHostProvider
from .gitea import GiteaProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

__all__ = ["GitHostError", "GitHostProvider", "get_provider", "SUPPORTED_HOSTS", "close_all_clients"]

SUPPORTED_HOSTS = ["github", "gitlab", "gitea"]

_PROVIDER_CLASSES = {
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
    "gitea": GiteaProvider,
}

# Один httpx.AsyncClient на хостинг, переиспользуется между вызовами
# (пул соединений) — создаётся лениво при первом обращении.
_clients: Dict[str, httpx.AsyncClient] = {}


def _client_for(host: str, api_url: str, token: Optional[str]) -> httpx.AsyncClient:
    if host not in _clients:
        headers = {"Accept": "application/json"}
        if token:
            # У всех трёх хостингов Bearer-токен принимается в
            # Authorization (у GitLab также работает заголовок
            # PRIVATE-TOKEN, но Bearer поддерживается GitLab.com и
            # современными self-hosted версиями — единообразия ради здесь
            # везде Bearer).
            headers["Authorization"] = f"Bearer {token}"
        _clients[host] = httpx.AsyncClient(base_url=api_url, headers=headers, timeout=MCPConfig.HTTP_TIMEOUT)
    return _clients[host]


def get_provider(host: str) -> GitHostProvider:
    if host not in _PROVIDER_CLASSES:
        raise ValueError(f"Неизвестный Git-хостинг '{host}': допустимо {SUPPORTED_HOSTS}")
    api_url = MCPConfig.api_url_for(host)
    token = MCPConfig.token_for(host)
    client = _client_for(host, api_url, token)
    return _PROVIDER_CLASSES[host](client, api_url, token)


async def close_all_clients() -> None:
    """Закрывает переиспользуемые HTTP-клиенты — вызывается при остановке
    приложения (см. `app.py`, lifespan)."""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()

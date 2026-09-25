"""
mcp_server.actions
=====================

Исполнители инструментов, работающих не с Git-хостингами:

- `git_pull` и `run_git_command` — подпроцесс git над локальной рабочей
  копией (группа «локальный Git», `MCP_LOCAL_GIT_ENABLED`);
- `http_fetch` — произвольный HTTP-запрос (`MCP_HTTP_FETCH_ENABLED`).

Раньше это были «действия» периодических задач встроенного планировщика;
планировщик вынесен в отдельный сервис, а действия стали обычными
MCP-инструментами — их можно вызвать из чата или поставить на расписание
в планировщике."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx


#: Таймаут `git pull` подпроцесса (секунды) — защита от зависшего
#: интерактивного запроса пароля/passphrase у git.
GIT_PULL_TIMEOUT = 60

#: Таймаут `execute_git_command` (MCP-инструмент, ЛЮБАЯ git-команда, не
#: только pull — см. `run_git_command`).
GIT_COMMAND_TIMEOUT = 60


class ActionError(Exception):
    """Ошибка выполнения — инструмент вернёт `{"error": ...}`."""


async def git_pull(repo_path: str, remote: Optional[str] = None, branch: Optional[str] = None) -> Dict[str, Any]:
    if not repo_path:
        raise ActionError("git_pull требует параметр 'repo_path'")
    remote = remote or "origin"
    args = ["git", "-C", repo_path, "pull", remote]
    if branch:
        args.append(branch)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_PULL_TIMEOUT)
    except FileNotFoundError as exc:
        raise ActionError(f"git не найден в PATH: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise ActionError(f"git pull не завершился за {GIT_PULL_TIMEOUT} с (repo_path={repo_path!r})") from exc
    result = {
        "repo_path": repo_path, "remote": remote, "branch": branch,
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
    if proc.returncode != 0:
        raise ActionError(f"git pull завершился с кодом {proc.returncode}: {result['stderr'].strip()[:500]}")
    return result


async def http_fetch(
    url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None, body: Any = None,
    timeout: float = 30,
) -> Dict[str, Any]:
    if not url:
        raise ActionError("http_fetch требует параметр 'url'")
    method = (method or "GET").upper()
    headers = headers or {}
    timeout = float(timeout or 30)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers, json=body if body is not None else None)
    except httpx.HTTPError as exc:
        raise ActionError(f"Сетевая ошибка при запросе к {url!r}: {exc}") from exc
    text = resp.text
    truncated = text if len(text) <= 5000 else text[:5000] + f"... (обрезано, всего {len(text)} симв.)"
    return {
        "url": url, "method": method, "status_code": resp.status_code,
        "headers": dict(resp.headers), "body": truncated,
    }


async def run_git_command(repo_path: str, command: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
    """`execute_git_command` MCP-инструмент (см. `server.py`) — ЛЮБАЯ
    git-команда над локальной рабочей копией (`git status`, `git log`,
    `git checkout <branch>`, ...), не только `pull` (для периодического
    `git_pull` есть отдельный, более узкий инструмент выше). Нарочно НЕ через
    shell (`create_subprocess_exec`, а не `_shell`) — `command`/`args`
    никогда не интерпретируются как единая командная строка, значит,
    классическая инъекция через `; rm -rf /` в аргументе невозможна; сама
    по себе команда всё равно достаточно мощная (например, `push --force`),
    так что вызывающая сторона (агент через AgentsCore, либо AgentsApp)
    должна доверять источнику вызова — см. README, раздел "Безопасность"."""
    if not repo_path:
        raise ActionError("execute_git_command требует параметр 'repo_path'")
    if not command:
        raise ActionError("execute_git_command требует параметр 'command'")
    cmd_args = ["git", "-C", repo_path, command, *(args or [])]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_COMMAND_TIMEOUT)
    except FileNotFoundError as exc:
        raise ActionError(f"git не найден в PATH: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise ActionError(f"'git {command}' не завершился за {GIT_COMMAND_TIMEOUT} с (repo_path={repo_path!r})") from exc
    result = {
        "repo_path": repo_path, "command": command, "args": args or [],
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
    if proc.returncode != 0:
        # Ненулевой код возврата — ошибка (инструмент вернёт {"error": ...}),
        # а не «успешный» результат с кодом 1.
        raise ActionError(f"'git {command}' завершился с кодом {proc.returncode}: {result['stderr'].strip()[:500]}")
    return result

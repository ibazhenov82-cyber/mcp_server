"""
mcp_server.actions
=====================

Исполнители трёх видов действий периодических задач (`ScheduledTool.action`,
см. `models.ACTION_KINDS`): `git_pull` (subprocess над локальной рабочей
копией), `http_fetch` (произвольный HTTP-запрос через `httpx`),
`git_host_poll` (один из `git_host_*` методов над публичным/приватным
Git-хостингом). Вызывается и планировщиком (`scheduler.py`, по
расписанию), и напрямую из MCP-инструментов сервера — единая точка
"выполнить действие по параметрам", не знающая ничего про сохранение
результата (это делает вызывающий код через `store.SchedulerStore`)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from .git_hosts import GitHostError, get_provider

#: Таймаут `git pull` подпроцесса (секунды) — защита от зависшего
#: интерактивного запроса пароля/passphrase у git.
GIT_PULL_TIMEOUT = 60

#: Таймаут `execute_git_command` (MCP-инструмент, ЛЮБАЯ git-команда, не
#: только pull — см. `run_git_command`).
GIT_COMMAND_TIMEOUT = 60


class ActionError(Exception):
    """Ошибка выполнения действия — сообщение уходит в `scheduled_tool_runs.error`."""


async def execute_action(action: str, params: Dict[str, Any]) -> Any:
    if action == "git_pull":
        return await _execute_git_pull(params)
    if action == "http_fetch":
        return await _execute_http_fetch(params)
    if action == "git_host_poll":
        return await _execute_git_host_poll(params)
    raise ActionError(f"Неизвестное действие: {action!r}")


async def _execute_git_pull(params: Dict[str, Any]) -> Dict[str, Any]:
    repo_path = params.get("repo_path")
    if not repo_path:
        raise ActionError("git_pull требует параметр 'repo_path'")
    remote = params.get("remote") or "origin"
    branch = params.get("branch")
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


async def _execute_http_fetch(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    if not url:
        raise ActionError("http_fetch требует параметр 'url'")
    method = (params.get("method") or "GET").upper()
    headers = params.get("headers") or {}
    body = params.get("body")
    timeout = float(params.get("timeout") or 30)
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


async def _execute_git_host_poll(params: Dict[str, Any]) -> Any:
    host = params.get("host")
    owner = params.get("owner")
    repo = params.get("repo")
    resource = params.get("resource", "commits")
    if not host or not owner or not repo:
        raise ActionError("git_host_poll требует параметры 'host', 'owner', 'repo'")
    try:
        provider = get_provider(host)
        if resource == "commits":
            return await provider.list_commits(
                owner, repo, since=params.get("since"), until=params.get("until"),
                branch=params.get("branch"), limit=int(params.get("limit") or 50),
            )
        if resource in ("pull_requests", "merge_requests"):
            return await provider.list_pull_requests(
                owner, repo, state=params.get("state", "all"), since=params.get("since"),
                limit=int(params.get("limit") or 50),
            )
        if resource == "issues":
            return await provider.list_issues(
                owner, repo, state=params.get("state", "all"), since=params.get("since"),
                limit=int(params.get("limit") or 50),
            )
        if resource == "releases":
            return await provider.list_releases(owner, repo, limit=int(params.get("limit") or 20))
        if resource == "repo":
            return await provider.get_repo(owner, repo)
        if resource == "file":
            path = params.get("path")
            if not path:
                raise ActionError("git_host_poll с resource='file' требует параметр 'path'")
            return await provider.get_file(owner, repo, path, ref=params.get("ref"))
        raise ActionError(
            f"Неизвестный resource={resource!r} для git_host_poll: ожидается один из "
            "'commits'|'pull_requests'|'issues'|'releases'|'repo'|'file'"
        )
    except (GitHostError, ValueError) as exc:
        raise ActionError(str(exc)) from exc


async def run_git_command(repo_path: str, command: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
    """`execute_git_command` MCP-инструмент (см. `server.py`) — ЛЮБАЯ
    git-команда над локальной рабочей копией (`git status`, `git log`,
    `git checkout <branch>`, ...), не только `pull` (для периодического
    `git_pull` есть отдельное, более узкое действие выше). Нарочно НЕ через
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
        # Тот же принцип, что и в `_execute_git_pull`: ненулевой код возврата
        # — это ошибка действия (уйдёт в `scheduled_tool_runs.error`/
        # {"error": ...} инструмента), а не "успешный" результат с кодом 1.
        raise ActionError(f"'git {command}' завершился с кодом {proc.returncode}: {result['stderr'].strip()[:500]}")
    return result

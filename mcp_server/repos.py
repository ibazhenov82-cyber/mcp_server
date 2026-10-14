"""
mcp_server.repos
==================

Группа «Репозиторий» (`MCP_REPOS_ENABLED`) — рабочие копии проектов для
ассистента разработчика (День 31):

- `open(link)` — проект по ссылке: URL GitHub / GitLab / Gitea (неглубокий
  клон в `MCP_REPOS_DIR`, при повторном открытии — `git fetch`) или
  локальный путь внутри `MCP_PROJECT_ROOTS` (рабочая копия используется на
  месте). Возвращает `repo_id` — его принимают все остальные операции;
  произвольный путь они не принимают.
- чтение: `status`, `log`, `show`, `grep`, `diff`, `blame`, `list_files`,
  `read_file`;
- запись (только локальная рабочая копия): `commit` и `undo_commit` —
  откат только коммита, сделанного через `commit` (журнал коммитов в
  `MCP_DATA_DIR/repo_commits.json`), пока он не отправлен в origin.

`repo_id` вычисляется из ссылки и не требует состояния сервера (несколько
экземпляров сервера с общим `MCP_DATA_DIR` понимают один и тот же id):
`git:<host>/<owner>/<repo>#<branch>` или `local:<абсолютный путь>`.

git запускается без shell (`create_subprocess_exec`), ссылки и пути
проверяются (`-` в начале, `..`, выход за пределы копии). Токены хостингов
передаются git через переменные окружения `GIT_CONFIG_*` (заголовок
`Authorization` только для своего хоста) — не попадают ни в аргументы
процесса, ни в `.git/config`, ни в ответы.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class RepoError(Exception):
    """Ожидаемая ошибка — инструмент вернёт `{"error": ...}`."""


#: Сколько символов текста отдаётся в одном ответе по умолчанию.
DEFAULT_MAX_CHARS = 20000
MAX_CHARS_LIMIT = 100000
GIT_TIMEOUT = 60
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/@{}~^+-]+$")
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_RE = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")


@dataclass
class RepoRef:
    repo_id: str
    kind: str  # "remote" | "local"
    path: str
    host: Optional[str] = None
    slug: Optional[str] = None
    branch: Optional[str] = None

    @property
    def name(self) -> str:
        return self.slug or os.path.basename(self.path.rstrip("/\\")) or self.path


def _clip(text: str, max_chars: int) -> Tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _limit(value: Optional[int], default: int, low: int, high: int) -> int:
    try:
        value = int(value) if value is not None else default
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _safe_branch_dir(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", branch.replace("/", "__"))


def check_ref(ref: str, what: str = "ref") -> str:
    ref = (ref or "").strip()
    if not ref:
        raise RepoError(f"{what}: пустое значение")
    if ref.startswith("-") or not _SAFE_REF_RE.match(ref) or ".." in ref:
        raise RepoError(f"{what}: недопустимое значение {ref!r}")
    return ref


def check_rel_path(path: Optional[str], what: str = "path") -> str:
    """Путь внутри репозитория: относительный, без `..` и `-` в начале."""
    path = (path or "").strip().replace("\\", "/").strip("/")
    if path in ("", "."):
        return ""
    if path.startswith("-") or os.path.isabs(path) or _WIN_PATH_RE.match(path):
        raise RepoError(f"{what}: нужен путь относительно корня репозитория, получено {path!r}")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise RepoError(f"{what}: «..» в пути недопустимо")
    if parts and parts[0] == ".git":
        raise RepoError(f"{what}: служебная папка .git недоступна")
    return "/".join(parts)


def is_local_link(link: str) -> bool:
    link = link.strip()
    return (link.startswith(("/", "~", "file://", "\\\\")) or bool(_WIN_PATH_RE.match(link)))


def parse_remote_link(link: str) -> Tuple[str, str, Optional[str]]:
    """URL репозитория → (хост, owner/repo, ветка или None).

    Понимает `https://github.com/o/r`, `.../tree/<ветка>`, `.../blob/<ветка>/…`,
    GitLab `group/sub/repo/-/tree/<ветка>`, Gitea `.../src/branch/<ветка>`,
    `git@host:o/r.git` и адрес без схемы (`github.com/o/r`)."""
    link = link.strip().rstrip("/")
    scp = _SCP_RE.match(link)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        if "://" not in link:
            link = "https://" + link
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https", "ssh", "git"):
            raise RepoError(f"неподдерживаемая ссылка {link!r}")
        host = (parsed.hostname or "").lower()
        path = parsed.path
    if not host or "." not in host:
        raise RepoError(f"не удалось определить хост в ссылке {link!r}")
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    branch: Optional[str] = None
    if "-" in parts:  # GitLab: group/repo/-/tree/<ветка>
        i = parts.index("-")
        rest = parts[i + 1:]
        if len(rest) >= 2 and rest[0] in ("tree", "blob"):
            branch = rest[1] if rest[0] == "blob" else "/".join(rest[1:])
        parts = parts[:i]
    elif len(parts) >= 4 and parts[2] == "src" and parts[3] == "branch":  # Gitea
        rest = parts[4:]
        branch = rest[0] if rest else None
        parts = parts[:2]
    elif len(parts) >= 3 and parts[2] in ("tree", "blob", "commits", "commit", "issues", "pulls", "pull",
                                         "releases", "actions", "wiki", "src"):
        if parts[2] == "tree" and len(parts) > 3:
            branch = "/".join(parts[3:])
        elif parts[2] == "blob" and len(parts) > 3:
            branch = parts[3]
        parts = parts[:2]
    elif host in ("github.com", "www.github.com"):
        parts = parts[:2]
    if len(parts) < 2:
        raise RepoError(f"в ссылке {link!r} нет пути owner/repo")
    if host == "www.github.com":
        host = "github.com"
    slug = "/".join(parts)
    if not all(re.match(r"^[A-Za-z0-9._-]+$", p) and p not in (".", "..") for p in parts):
        raise RepoError(f"недопустимый путь репозитория {slug!r}")
    if branch is not None:
        branch = check_ref(branch, "ветка")
    return host, slug, branch


class CommitLedger:
    """Коммиты, сделанные через `commit`, по репозиториям — `undo_commit`
    откатывает только их."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, List[str]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: Dict[str, List[str]]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, repo_id: str, sha: str) -> None:
        with self._lock:
            data = self._read()
            items = [s for s in data.get(repo_id, []) if s != sha] + [sha]
            data[repo_id] = items[-200:]
            self._write(data)

    def contains(self, repo_id: str, sha: str) -> bool:
        with self._lock:
            return sha in self._read().get(repo_id, [])

    def remove(self, repo_id: str, sha: str) -> None:
        with self._lock:
            data = self._read()
            data[repo_id] = [s for s in data.get(repo_id, []) if s != sha]
            self._write(data)


class RepoManager:
    def __init__(
        self,
        repos_dir: str,
        project_roots: List[str],
        *,
        data_dir: str,
        clone_depth: int = 200,
        fetch_interval: float = 60.0,
        clone_timeout: float = 300.0,
        allowed_hosts: Optional[List[str]] = None,
        credentials: Optional[Callable[[str], Optional[Tuple[str, str]]]] = None,
        remote_url: Optional[Callable[[str, str], str]] = None,
    ):
        self.repos_dir = os.path.abspath(repos_dir)
        self.project_roots = [os.path.realpath(os.path.expanduser(r)) for r in project_roots if r.strip()]
        self.clone_depth = max(0, int(clone_depth))
        self.fetch_interval = fetch_interval
        self.clone_timeout = clone_timeout
        #: None или ["*"] — любой хост.
        self.allowed_hosts = [h.lower() for h in (allowed_hosts or [])]
        self.credentials = credentials or (lambda host: None)
        #: Адрес клона по хосту и owner/repo (тесты подменяют на локальные bare-репозитории).
        self.remote_url = remote_url or (lambda host, slug: f"https://{host}/{slug}.git")
        self.ledger = CommitLedger(os.path.join(data_dir, "repo_commits.json"))
        self._locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # git
    # ------------------------------------------------------------------

    def _env(self, host: Optional[str]) -> Dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_ASKPASS", "echo")
        creds = self.credentials(host) if host else None
        if creds:
            user, token = creds
            basic = base64.b64encode(f"{user}:{token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = f"http.https://{host}/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
        return env

    async def _git(
        self, cwd: Optional[str], *args: str, host: Optional[str] = None, timeout: float = GIT_TIMEOUT,
        check: bool = True, ok_codes: Tuple[int, ...] = (0,), stdin: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        cmd = ["git"] + (["-C", cwd] if cwd else []) + list(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                env=self._env(host),
            )
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode("utf-8") if stdin is not None else None), timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RepoError("git не найден в PATH") from exc
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            raise RepoError(f"git {args[0]} не завершился за {int(timeout)} с") from exc
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        if check and proc.returncode not in ok_codes:
            message = self._scrub(stderr.strip() or stdout.strip())[:600]
            raise RepoError(f"git {args[0]}: {message or f'код {proc.returncode}'}")
        return proc.returncode, stdout, stderr

    @staticmethod
    def _scrub(text: str) -> str:
        return re.sub(r"https://[^/@\s]+@", "https://", text)

    # ------------------------------------------------------------------
    # repo_id
    # ------------------------------------------------------------------

    def _remote_dir(self, host: str, slug: str, branch: str) -> str:
        return os.path.join(self.repos_dir, host, *slug.split("/"), _safe_branch_dir(branch))

    def _in_roots(self, real: str) -> bool:
        for root in self.project_roots:
            if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False

    def _check_local(self, path: str) -> str:
        if not self.project_roots:
            raise RepoError("локальные проекты не разрешены: не задан MCP_PROJECT_ROOTS")
        if path.startswith("file://"):
            path = path[len("file://"):]
        real = os.path.realpath(os.path.expanduser(path))
        if not self._in_roots(real):
            raise RepoError(f"путь {path} вне разрешённых папок MCP_PROJECT_ROOTS")
        if not os.path.isdir(real):
            raise RepoError(f"папка {path} не найдена")
        return real

    def _check_host(self, host: str) -> None:
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            return
        if host not in self.allowed_hosts:
            raise RepoError(f"хост {host} не разрешён (MCP_REPO_ALLOWED_HOSTS: {', '.join(self.allowed_hosts)})")

    def resolve(self, repo_id: str) -> RepoRef:
        repo_id = (repo_id or "").strip()
        if repo_id.startswith("local:"):
            real = self._check_local(repo_id[len("local:"):])
            if not os.path.exists(os.path.join(real, ".git")):
                raise RepoError(f"{real} — не корень git-репозитория")
            return RepoRef(repo_id=f"local:{real}", kind="local", path=real)
        if repo_id.startswith("git:"):
            body = repo_id[len("git:"):]
            location, _, branch = body.partition("#")
            host, _, slug = location.partition("/")
            if not host or not slug or not branch:
                raise RepoError(f"неверный repo_id {repo_id!r}")
            host, slug, _ = parse_remote_link(f"https://{host}/{slug}")
            branch = check_ref(branch, "ветка")
            path = self._remote_dir(host, slug, branch)
            if not os.path.isdir(os.path.join(path, ".git")):
                raise RepoError(f"репозиторий {repo_id} не открыт — вызовите repo_open")
            return RepoRef(repo_id=f"git:{host}/{slug}#{branch}", kind="remote", path=path, host=host,
                           slug=slug, branch=branch)
        raise RepoError(f"неверный repo_id {repo_id!r}: ожидается значение из repo_open")

    # ------------------------------------------------------------------
    # repo_open
    # ------------------------------------------------------------------

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def open(self, link: str, branch: Optional[str] = None, refresh: bool = True) -> Dict[str, Any]:
        link = (link or "").strip().strip("<>\"'`")
        if not link:
            raise RepoError("link: пустая ссылка")
        if is_local_link(link):
            real = self._check_local(link)
            _, top, _ = await self._git(real, "rev-parse", "--show-toplevel")
            top = os.path.realpath(top.strip())
            if not self._in_roots(top):
                raise RepoError("корень репозитория вне разрешённых папок MCP_PROJECT_ROOTS")
            ref = RepoRef(repo_id=f"local:{top}", kind="local", path=top)
            info = await self.info(ref)
            if branch and branch != info.get("branch"):
                info["note"] = (f"ветка локальной копии не переключается: текущая ветка {info.get('branch')}; "
                                f"переключите её сами, если нужна {branch}")
            return info
        host, slug, link_branch = parse_remote_link(link)
        self._check_host(host)
        branch = check_ref(branch, "ветка") if branch else link_branch
        url = self.remote_url(host, slug)
        if not branch:
            branch = await self._default_branch(url, host)
        path = self._remote_dir(host, slug, branch)
        ref = RepoRef(repo_id=f"git:{host}/{slug}#{branch}", kind="remote", path=path, host=host, slug=slug,
                      branch=branch)
        async with self._lock(path):
            if not os.path.isdir(os.path.join(path, ".git")):
                await self._clone(ref)
            elif refresh:
                await self._refresh(ref)
        return await self.info(ref)

    async def _default_branch(self, url: str, host: str) -> str:
        _, out, _ = await self._git(None, "ls-remote", "--symref", url, "HEAD", host=host, timeout=self.clone_timeout)
        for line in out.splitlines():
            if line.startswith("ref:") and line.rstrip().endswith("HEAD"):
                ref = line.split()[1]
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/"):]
        raise RepoError("не удалось определить ветку по умолчанию — укажите ветку в ссылке (/tree/<ветка>)")

    def _depth_args(self) -> List[str]:
        return [f"--depth={self.clone_depth}"] if self.clone_depth else []

    async def _clone(self, ref: RepoRef) -> None:
        parent = os.path.dirname(ref.path)
        os.makedirs(parent, exist_ok=True)
        tmp = os.path.join(parent, f".clone-{uuid.uuid4().hex[:8]}")
        try:
            await self._git(None, "clone", *self._depth_args(), "--branch", ref.branch, "--",
                            self.remote_url(ref.host, ref.slug), tmp,
                            host=ref.host, timeout=self.clone_timeout)
            self._mark_fetched(tmp)
            os.replace(tmp, ref.path)
        finally:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _marker(path: str) -> str:
        return os.path.join(path, ".git", "agentscore_fetched")

    def _mark_fetched(self, path: str) -> None:
        try:
            with open(self._marker(path), "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
        except OSError:
            pass

    def _fetched_at(self, path: str) -> Optional[int]:
        try:
            return int(os.path.getmtime(self._marker(path)))
        except OSError:
            return None

    async def _refresh(self, ref: RepoRef) -> None:
        fetched = self._fetched_at(ref.path)
        if fetched is not None and time.time() - fetched < self.fetch_interval:
            return
        await self._git(ref.path, "fetch", *self._depth_args(), "origin",
                        f"+refs/heads/{ref.branch}:refs/remotes/origin/{ref.branch}",
                        host=ref.host, timeout=self.clone_timeout)
        await self._git(ref.path, "reset", "--hard", f"refs/remotes/origin/{ref.branch}")
        self._mark_fetched(ref.path)

    async def info(self, ref: RepoRef) -> Dict[str, Any]:
        _, branch, _ = await self._git(ref.path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        head = await self._head(ref)
        default_branch = ref.branch if ref.kind == "remote" else None
        if ref.kind == "local":
            code, out, _ = await self._git(ref.path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
            if code == 0 and out.strip():
                default_branch = out.strip().split("/", 1)[-1]
        code, remote, _ = await self._git(ref.path, "remote", "get-url", "origin", check=False)
        remote_url = self._scrub(remote.strip()) if code == 0 else None
        info: Dict[str, Any] = {
            "repo_id": ref.repo_id, "kind": ref.kind, "name": ref.name, "path": ref.path,
            "branch": branch.strip() or ref.branch, "default_branch": default_branch,
            "remote": remote_url, "head": head, "writable": ref.kind == "local",
        }
        if ref.kind == "remote":
            info["host"] = ref.host
            info["fetched_at"] = self._fetched_at(ref.path)
            info["shallow"] = bool(self.clone_depth)
        return info

    async def _head(self, ref: RepoRef) -> Optional[Dict[str, Any]]:
        code, out, _ = await self._git(ref.path, "log", "-1", "--date=iso-strict",
                                       "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s", check=False)
        if code != 0 or not out.strip():
            return None
        sha, short, author, date, subject = (out.split("\x1f") + [""] * 5)[:5]
        return {"sha": sha, "short": short, "author": author, "date": date, "subject": subject}

    # ------------------------------------------------------------------
    # Ссылки на коммиты
    # ------------------------------------------------------------------

    async def _verify(self, ref: RepoRef, rev: str) -> bool:
        code, _, _ = await self._git(ref.path, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
        return code == 0

    async def resolve_rev(self, ref: RepoRef, rev: str) -> str:
        """Ветка, тег или коммит → выражение, которое понимает git. В клоне по
        URL отсутствующая ветка догружается (`git fetch origin <ветка>`)."""
        rev = check_ref(rev, "ref")
        if await self._verify(ref, rev):
            return rev
        if await self._verify(ref, f"origin/{rev}"):
            return f"origin/{rev}"
        if ref.kind == "remote":
            code, _, _ = await self._git(
                ref.path, "fetch", *self._depth_args(), "origin",
                f"+refs/heads/{rev}:refs/remotes/origin/{rev}", host=ref.host, timeout=self.clone_timeout, check=False,
            )
            if code == 0 and await self._verify(ref, f"origin/{rev}"):
                return f"origin/{rev}"
        hint = " (клон неглубокий: старые коммиты могут быть недоступны)" if ref.kind == "remote" else ""
        raise RepoError(f"ветка, тег или коммит {rev!r} не найдены{hint}")

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    async def status(self, ref: RepoRef) -> Dict[str, Any]:
        _, out, _ = await self._git(ref.path, "status", "--porcelain=v1", "-b", "--untracked-files=normal")
        lines = out.splitlines()
        upstream, ahead, behind = None, 0, 0
        files: List[Dict[str, Any]] = []
        for line in lines:
            if line.startswith("## "):
                head = line[3:]
                m = re.search(r"\.\.\.(\S+)", head)
                upstream = m.group(1) if m else None
                a = re.search(r"ahead (\d+)", head)
                b = re.search(r"behind (\d+)", head)
                ahead = int(a.group(1)) if a else 0
                behind = int(b.group(1)) if b else 0
                continue
            if len(line) < 4:
                continue
            x, y, path = line[0], line[1], line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            files.append({
                "path": path, "index": x, "worktree": y,
                "staged": x not in (" ", "?"), "unstaged": y not in (" ", "?"), "untracked": x == "?",
                "status": _status_title(x, y),
            })
        info = await self.info(ref)
        return {
            **info, "upstream": upstream, "ahead": ahead, "behind": behind,
            "clean": not files, "changed_files": len(files),
            "staged": sum(1 for f in files if f["staged"]),
            "unstaged": sum(1 for f in files if f["unstaged"]),
            "untracked": sum(1 for f in files if f["untracked"]),
            "files": files[:300], "files_truncated": len(files) > 300,
        }

    async def log(
        self, ref: RepoRef, path: Optional[str] = None, rev: Optional[str] = None, limit: int = 20,
        author: Optional[str] = None, grep: Optional[str] = None, since: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = _limit(limit, 20, 1, 100)
        args = ["log", f"--max-count={limit}", "--date=iso-strict", "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1e"]
        if author:
            args.append(f"--author={author}")
        if grep:
            args += [f"--grep={grep}", "-i"]
        if since:
            if since.startswith("-"):
                raise RepoError("since: недопустимое значение")
            args.append(f"--since={since}")
        if rev:
            args.append(await self.resolve_rev(ref, rev))
        args.append("--")
        rel = check_rel_path(path)
        if rel:
            args.append(rel)
        _, out, _ = await self._git(ref.path, *args)
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, short, a, date, subject = (record.split("\x1f") + [""] * 5)[:5]
            commits.append({"sha": sha, "short": short, "author": a, "date": date, "subject": subject})
        result: Dict[str, Any] = {"repo_id": ref.repo_id, "path": rel or None, "commits": commits}
        if ref.kind == "remote" and self.clone_depth:
            result["shallow"] = True
        return result

    async def show(self, ref: RepoRef, commit: str, path: Optional[str] = None,
                   max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
        max_chars = _limit(max_chars, DEFAULT_MAX_CHARS, 1000, MAX_CHARS_LIMIT)
        rev = await self.resolve_rev(ref, commit)
        _, meta, _ = await self._git(ref.path, "show", "-s", "--date=iso-strict",
                                     "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%b", rev)
        sha, short, author, date, subject, body = (meta.split("\x1f") + [""] * 6)[:6]
        rel = check_rel_path(path)
        tail = ["--", rel] if rel else []
        _, numstat, _ = await self._git(ref.path, "show", "--format=", "--numstat", rev, *tail)
        _, patch, _ = await self._git(ref.path, "show", "--format=", "--patch", "--no-color", rev, *tail)
        patch, truncated = _clip(patch, max_chars)
        return {
            "sha": sha, "short": short, "author": author, "date": date, "subject": subject,
            "body": body.strip(), "files": _parse_numstat(numstat), "patch": patch, "truncated": truncated,
        }

    async def grep(
        self, ref: RepoRef, pattern: str, path: Optional[str] = None, ignore_case: bool = True,
        fixed: bool = False, max_results: int = 50, rev: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not pattern:
            raise RepoError("pattern: пустой шаблон")
        max_results = _limit(max_results, 50, 1, 300)
        args = ["grep", "-n", "-I", "--full-name", "--no-color"]
        if ignore_case:
            args.append("-i")
        args.append("-F" if fixed else "-E")
        args += ["-e", pattern]
        resolved = await self.resolve_rev(ref, rev) if rev else None
        if resolved:
            args.append(resolved)
        args.append("--")
        rel = check_rel_path(path)
        if rel:
            args.append(rel)
        code, out, _ = await self._git(ref.path, *args, ok_codes=(0, 1))
        matches = []
        total = 0
        for line in out.splitlines():
            if resolved and line.startswith(resolved + ":"):
                line = line[len(resolved) + 1:]
            parts = line.split(":", 2)
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            total += 1
            if len(matches) < max_results:
                text = parts[2]
                matches.append({"path": parts[0], "line": int(parts[1]),
                                "text": text if len(text) <= 300 else text[:300] + "…"})
        return {"pattern": pattern, "matches": matches, "total": total, "truncated": total > len(matches)}

    async def _diff_args(self, ref: RepoRef, spec: Optional[str]) -> Tuple[List[str], str]:
        spec = (spec or "").strip()
        if not spec or spec in ("working", "worktree", "all"):
            if not await self._verify(ref, "HEAD"):
                return ["--cached"], "все изменения (коммитов ещё нет)"
            return ["HEAD"], "незакоммиченные изменения (рабочая копия и индекс против HEAD)"
        if spec in ("staged", "cached"):
            return ["--cached"], "подготовленные к коммиту изменения (индекс против HEAD)"
        for sep in ("...", ".."):
            if sep in spec:
                a, _, b = spec.partition(sep)
                left = await self.resolve_rev(ref, a or "HEAD")
                right = await self.resolve_rev(ref, b or "HEAD")
                title = (f"{a or 'HEAD'} → {b or 'HEAD'}" if sep == ".." else
                         f"изменения {b or 'HEAD'} с момента ответвления от {a or 'HEAD'}")
                return [f"{left}{sep}{right}"], title
        rev = await self.resolve_rev(ref, spec)
        return [rev], f"рабочая копия против {spec}"

    async def diff(self, ref: RepoRef, spec: Optional[str] = None, path: Optional[str] = None,
                   max_chars: int = 30000) -> Dict[str, Any]:
        max_chars = _limit(max_chars, 30000, 1000, MAX_CHARS_LIMIT)
        rev_args, title = await self._diff_args(ref, spec)
        rel = check_rel_path(path)
        tail = ["--", rel] if rel else ["--"]
        _, names, _ = await self._git(ref.path, "diff", "-M", "--name-status", *rev_args, *tail)
        _, numstat, _ = await self._git(ref.path, "diff", "-M", "--numstat", *rev_args, *tail)
        _, patch, _ = await self._git(ref.path, "diff", "-M", "--no-color", *rev_args, *tail)
        files = []
        stats = _parse_numstat(numstat)
        for i, line in enumerate(names.splitlines()):
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            code = cols[0][:1]
            entry = {"path": cols[-1], "status": _DIFF_STATUS.get(code, code)}
            if code in ("R", "C") and len(cols) >= 3:
                entry["old_path"] = cols[1]
            if i < len(stats):
                entry.update({k: stats[i][k] for k in ("additions", "deletions", "binary")})
            files.append(entry)
        untracked: List[str] = []
        if not (spec or "").strip() or (spec or "").strip() in ("working", "worktree", "all"):
            _, out, _ = await self._git(ref.path, "ls-files", "--others", "--exclude-standard", *tail)
            untracked = [p for p in out.splitlines() if p][:200]
        patch, truncated = _clip(patch, max_chars)
        return {
            "spec": (spec or "").strip() or None, "title": title, "files": files, "untracked": untracked,
            "stats": {"files": len(files), "additions": sum(f.get("additions") or 0 for f in files),
                      "deletions": sum(f.get("deletions") or 0 for f in files)},
            "patch": patch, "truncated": truncated,
        }

    async def blame(self, ref: RepoRef, path: str, start_line: Optional[int] = None,
                    end_line: Optional[int] = None, rev: Optional[str] = None) -> Dict[str, Any]:
        rel = check_rel_path(path)
        if not rel:
            raise RepoError("path: укажите файл")
        start = _limit(start_line, 1, 1, 10_000_000)
        end = _limit(end_line, start + 199, start, start + 399) if end_line else start + 199
        args = ["blame", "--line-porcelain", "-L", f"{start},{end}"]
        if rev:
            args.append(await self.resolve_rev(ref, rev))
        code, out, err = await self._git(ref.path, *args, "--", rel, check=False)
        if code != 0:
            if "has only" in err:  # диапазон за концом файла
                code, out, err = await self._git(ref.path, *args[:2], *(args[4:]), "--", rel, check=False)
            if code != 0:
                raise RepoError(f"git blame: {err.strip()[:400]}")
        lines, commits = [], {}
        current: Dict[str, Any] = {}
        meta: Dict[str, Dict[str, Any]] = {}
        for raw in out.splitlines():
            if raw.startswith("\t"):
                sha = current.get("sha", "")
                info = meta.setdefault(sha, {})
                info.update({k: v for k, v in current.items() if k != "line"})
                lines.append({"line": current.get("line"), "sha": sha[:8], "author": info.get("author", ""),
                              "date": info.get("date", ""), "summary": info.get("summary", ""), "text": raw[1:]})
                c = commits.setdefault(sha, {"sha": sha[:8], "author": info.get("author", ""),
                                             "date": info.get("date", ""), "summary": info.get("summary", ""),
                                             "lines": 0})
                c["lines"] += 1
                current = {}
                continue
            head = raw.split(" ")
            if not current and len(head) >= 3 and re.match(r"^[0-9a-f]{40}$", head[0]):
                current = {"sha": head[0], "line": int(head[2])}
                current.update(meta.get(head[0], {}))
                current["line"] = int(head[2])
            elif raw.startswith("author "):
                current["author"] = raw[7:]
            elif raw.startswith("author-time "):
                current["date"] = time.strftime("%Y-%m-%d", time.gmtime(int(raw[12:])))
            elif raw.startswith("summary "):
                current["summary"] = raw[8:]
        return {"path": rel, "lines": lines,
                "commits": sorted(commits.values(), key=lambda c: -c["lines"])}

    async def _all_files(self, ref: RepoRef, rel: str) -> List[str]:
        tail = ["--", rel] if rel else []
        _, out, _ = await self._git(ref.path, "ls-files", *tail)
        files = [p for p in out.splitlines() if p]
        if ref.kind == "local":
            _, extra, _ = await self._git(ref.path, "ls-files", "--others", "--exclude-standard", *tail)
            files += [p for p in extra.splitlines() if p]
        return sorted(set(files))

    async def list_files(self, ref: RepoRef, path: Optional[str] = None, depth: int = 2,
                         pattern: Optional[str] = None, max_entries: int = 300) -> Dict[str, Any]:
        rel = check_rel_path(path)
        depth = _limit(depth, 2, 1, 10)
        max_entries = _limit(max_entries, 300, 10, 2000)
        files = await self._all_files(ref, rel)
        if pattern:
            pat = pattern.strip()
            matched = [f for f in files if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(os.path.basename(f), pat)]
            return {"path": rel or ".", "pattern": pat, "entries": [{"path": f, "type": "file"} for f in matched[:max_entries]],
                    "total_files": len(matched), "truncated": len(matched) > max_entries}
        prefix = rel + "/" if rel else ""
        dirs: Dict[str, int] = {}
        plain: List[str] = []
        for f in files:
            sub = f[len(prefix):] if f.startswith(prefix) else f
            parts = sub.split("/")
            if len(parts) <= depth:
                plain.append(f)
            else:
                key = prefix + "/".join(parts[:depth])
                dirs[key] = dirs.get(key, 0) + 1
        entries = [{"path": d + "/", "type": "dir", "files": n} for d, n in dirs.items()]
        entries += [{"path": f, "type": "file"} for f in plain]
        entries.sort(key=lambda e: e["path"])
        return {"path": rel or ".", "depth": depth, "entries": entries[:max_entries], "total_files": len(files),
                "truncated": len(entries) > max_entries}

    async def read_file(self, ref: RepoRef, path: str, rev: Optional[str] = None, start_line: Optional[int] = None,
                        end_line: Optional[int] = None, max_chars: int = DEFAULT_MAX_CHARS,
                        line_numbers: bool = True) -> Dict[str, Any]:
        rel = check_rel_path(path)
        if not rel:
            raise RepoError("path: укажите файл")
        max_chars = _limit(max_chars, DEFAULT_MAX_CHARS, 500, MAX_CHARS_LIMIT)
        if rev:
            resolved = await self.resolve_rev(ref, rev)
            code, _, err = await self._git(ref.path, "cat-file", "-e", f"{resolved}:{rel}", check=False)
            if code != 0:
                raise RepoError(f"файла {rel} нет в {rev}")
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", ref.path, "show", f"{resolved}:{rel}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env(None),
            )
            data, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
        else:
            full = os.path.realpath(os.path.join(ref.path, rel))
            root = os.path.realpath(ref.path)
            if not full.startswith(root + os.sep):
                raise RepoError("path: выход за пределы репозитория")
            if not os.path.isfile(full):
                raise RepoError(f"файл {rel} не найден")
            if os.path.getsize(full) > 5 * 1024 * 1024:
                raise RepoError(f"файл {rel} больше 5 МБ")
            with open(full, "rb") as fh:
                data = fh.read()
        if b"\x00" in data[:8000]:
            raise RepoError(f"{rel} — двоичный файл")
        text = data.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)
        start = _limit(start_line, 1, 1, max(1, total))
        end = _limit(end_line, total, start, max(start, total)) if end_line else total
        chunk = all_lines[start - 1:end]
        width = len(str(end))
        out_lines, size, last = [], 0, start - 1
        for i, line in enumerate(chunk, start=start):
            rendered = f"{str(i).rjust(width)}| {line}" if line_numbers else line
            if size + len(rendered) + 1 > max_chars and out_lines:
                break
            out_lines.append(rendered)
            size += len(rendered) + 1
            last = i
        return {"path": rel, "rev": rev, "start_line": start, "end_line": last, "total_lines": total,
                "truncated": last < end, "content": "\n".join(out_lines)}

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    @staticmethod
    def _require_local(ref: RepoRef) -> None:
        if ref.kind != "local":
            raise RepoError("коммит и откат возможны только в локальной рабочей копии (проект открыт по URL)")

    async def commit(self, ref: RepoRef, message: str, files: Optional[List[str]] = None) -> Dict[str, Any]:
        self._require_local(ref)
        message = (message or "").strip()
        if not message:
            raise RepoError("message: пустое сообщение коммита")
        if files:
            paths = [check_rel_path(f, "files") for f in files]
            paths = [p for p in paths if p]
            if not paths:
                raise RepoError("files: пустой список")
            await self._git(ref.path, "add", "-A", "--", *paths)
        code, _, _ = await self._git(ref.path, "diff", "--cached", "--quiet", check=False)
        if code == 0:
            raise RepoError("нет подготовленных изменений для коммита — передайте files")
        await self._git(ref.path, "commit", "-F", "-", stdin=message + "\n")
        head = await self._head(ref)
        _, names, _ = await self._git(ref.path, "show", "--format=", "--name-only", "HEAD")
        self.ledger.add(ref.repo_id, head["sha"])
        _, branch, _ = await self._git(ref.path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        return {"repo_id": ref.repo_id, "commit": head["sha"], "short": head["short"], "subject": head["subject"],
                "branch": branch.strip(), "files": [n for n in names.splitlines() if n]}

    async def undo_commit(self, ref: RepoRef, commit: str) -> Dict[str, Any]:
        self._require_local(ref)
        commit = check_ref(commit, "commit")
        code, sha, _ = await self._git(ref.path, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
        sha = sha.strip()
        if code != 0 or not sha:
            raise RepoError(f"коммит {commit} не найден")
        if not self.ledger.contains(ref.repo_id, sha):
            raise RepoError("откатить можно только коммит, сделанный через repo_commit")
        _, head, _ = await self._git(ref.path, "rev-parse", "HEAD")
        if head.strip() != sha:
            raise RepoError("коммит уже не последний в ветке — откат через reset невозможен")
        _, remotes, _ = await self._git(ref.path, "branch", "-r", "--contains", sha)
        if remotes.strip():
            raise RepoError("коммит уже отправлен в удалённый репозиторий — откат не выполняется")
        if not await self._verify(ref, f"{sha}~1"):
            raise RepoError("это первый коммит репозитория — откат не выполняется")
        await self._git(ref.path, "reset", "--soft", "HEAD~1")
        self.ledger.remove(ref.repo_id, sha)
        new_head = await self._head(ref)
        return {"repo_id": ref.repo_id, "undone": sha, "head": new_head,
                "note": "изменения коммита остались в индексе (git reset --soft)"}


_DIFF_STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied", "T": "type_changed"}


def _status_title(x: str, y: str) -> str:
    if x == "?":
        return "новый"
    code = x if x not in (" ",) else y
    return {"M": "изменён", "A": "добавлен", "D": "удалён", "R": "переименован", "C": "скопирован",
            "U": "конфликт", "T": "тип изменён"}.get(code, code)


def _parse_numstat(text: str) -> List[Dict[str, Any]]:
    result = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        binary = cols[0] == "-"
        path = cols[2]
        if " => " in path:
            m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", path)
            path = f"{m.group(1)}{m.group(3)}{m.group(4)}".replace("//", "/") if m else path.split(" => ")[-1]
        result.append({"path": path, "additions": 0 if binary else int(cols[0]),
                       "deletions": 0 if binary else int(cols[1]), "binary": binary})
    return result

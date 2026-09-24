"""
mcp_server.git_hosts.gitea
=============================

Gitea REST API v1 (`https://gitea.com/api/v1` по умолчанию — как правило,
self-hosted, поэтому `GITEA_API_URL` почти всегда переопределяется). API во
многом повторяет форму GitHub (те же поля у коммитов/issues/PR), но
пагинация — `limit`/`page`, а не `per_page`, и rate-limiting по умолчанию
выключен (заголовков лимита может не быть вовсе — тогда `get_status()`
вернёт `rate_limit_remaining=None`, это ожидаемо, не ошибка).
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import GitHostProvider, NormalizedItem, parse_since, rate_limit_from_headers
from ..models import GitHostStatus


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GiteaProvider(GitHostProvider):
    host = "gitea"

    async def get_status(self) -> GitHostStatus:
        resp = await self._get("/version")
        limits = rate_limit_from_headers(resp.headers)
        return GitHostStatus(
            host=self.host, configured=self.configured, api_url=self.api_url,
            rate_limit_remaining=limits["remaining"], rate_limit_reset_at=limits["reset_at"],
        )

    async def list_commits(
        self, owner: str, repo: str, *, since: Optional[str] = None, until: Optional[str] = None,
        branch: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        since_dt = parse_since(since)
        params: Dict[str, Any] = {"limit": min(limit, 50)}
        if branch:
            params["sha"] = branch
        resp = await self._get(f"/repos/{owner}/{repo}/commits", params=params)
        items = []
        for c in resp.json():
            commit = c.get("commit", {})
            author = commit.get("author", {})
            dt = _parse_dt(author.get("date"))
            items.append(NormalizedItem(timestamp=dt, data={
                "sha": c.get("sha"),
                "message": commit.get("message"),
                "author": author.get("name"),
                "author_email": author.get("email"),
                "date": author.get("date"),
                "url": c.get("html_url"),
            }))
        return self._apply_since_and_limit(items, since_dt, limit)

    async def list_pull_requests(
        self, owner: str, repo: str, *, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        since_dt = parse_since(since)
        params = {"state": state, "limit": min(limit, 50)}
        resp = await self._get(f"/repos/{owner}/{repo}/pulls", params=params)
        items = []
        for pr in resp.json():
            dt = _parse_dt(pr.get("updated_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "number": pr.get("number"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "author": (pr.get("user") or {}).get("login"),
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at"),
                "merged": bool(pr.get("merged")),
                "url": pr.get("html_url"),
            }))
        return self._apply_since_and_limit(items, since_dt, limit)

    async def list_issues(
        self, owner: str, repo: str, *, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        since_dt = parse_since(since)
        params: Dict[str, Any] = {"state": state, "limit": min(limit, 50)}
        if since_dt:
            params["since"] = since_dt.isoformat()
        resp = await self._get(f"/repos/{owner}/{repo}/issues", params=params)
        items = []
        for issue in resp.json():
            if issue.get("pull_request") is not None:  # как и GitHub, Gitea мешает PR'ы в issues
                continue
            dt = _parse_dt(issue.get("updated_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "number": issue.get("number"),
                "title": issue.get("title"),
                "state": issue.get("state"),
                "author": (issue.get("user") or {}).get("login"),
                "created_at": issue.get("created_at"),
                "updated_at": issue.get("updated_at"),
                "url": issue.get("html_url"),
            }))
        return self._apply_since_and_limit(items, since_dt, limit)

    async def list_releases(self, owner: str, repo: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        resp = await self._get(f"/repos/{owner}/{repo}/releases", params={"limit": min(limit, 50)})
        items = []
        for r in resp.json():
            dt = _parse_dt(r.get("published_at") or r.get("created_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "tag": r.get("tag_name"),
                "name": r.get("name"),
                "created_at": r.get("created_at"),
                "published_at": r.get("published_at"),
                "draft": r.get("draft", False),
                "prerelease": r.get("prerelease", False),
                "url": r.get("html_url"),
            }))
        return self._apply_since_and_limit(items, None, limit)

    async def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        resp = await self._get(f"/repos/{owner}/{repo}")
        r = resp.json()
        return {
            "full_name": r.get("full_name"),
            "description": r.get("description"),
            "default_branch": r.get("default_branch"),
            "stars": r.get("stars_count"),
            "forks": r.get("forks_count"),
            "open_issues": r.get("open_issues_count"),
            "url": r.get("html_url"),
        }

    async def get_file(self, owner: str, repo: str, path: str, *, ref: Optional[str] = None) -> Dict[str, Any]:
        params = {"ref": ref} if ref else None
        resp = await self._get(f"/repos/{owner}/{repo}/contents/{path}", params=params)
        body = resp.json()
        encoded = body.get("content", "")
        content = base64.b64decode(encoded).decode("utf-8", errors="replace") if encoded else ""
        return {
            "path": body.get("path"),
            "content": content,
            "encoding": "utf-8",
            "sha": body.get("sha"),
            "size": body.get("size"),
        }

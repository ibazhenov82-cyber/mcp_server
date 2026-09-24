"""
mcp_server.git_hosts.gitlab
==============================

GitLab REST API v4 (`https://gitlab.com/api/v4` по умолчанию — совместимо
и с self-hosted GitLab через `GITLAB_API_URL`). Проект адресуется как
`owner/repo`, URL-кодируется в `:id` (стандартный способ GitLab API, если
числовой id проекта неизвестен).

GitLab использует свои названия состояний ("opened", а не "open") — здесь
эта разница скрыта: наружу (и в другие провайдеры) уходит общий
словарь "open"/"closed"/"all".
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .base import GitHostProvider, NormalizedItem, parse_since
from ..models import GitHostStatus

_STATE_MAP = {"open": "opened", "closed": "closed", "all": None}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitLabProvider(GitHostProvider):
    host = "gitlab"

    @staticmethod
    def _project_id(owner: str, repo: str) -> str:
        return quote(f"{owner}/{repo}", safe="")

    async def get_status(self) -> GitHostStatus:
        # У GitLab нет отдельного эндпоинта "остаток лимита" — берём
        # заголовки любого дешёвого публичного запроса.
        resp = await self._get("/version")
        from .base import rate_limit_from_headers
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
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if branch:
            params["ref_name"] = branch
        if since_dt:
            params["since"] = since_dt.isoformat()
        if until:
            params["until"] = parse_since(until).isoformat() if until.startswith("-") else until
        resp = await self._get(f"/projects/{self._project_id(owner, repo)}/repository/commits", params=params)
        items = []
        for c in resp.json():
            dt = _parse_dt(c.get("committed_date") or c.get("created_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "sha": c.get("id"),
                "message": c.get("message"),
                "author": c.get("author_name"),
                "author_email": c.get("author_email"),
                "date": c.get("committed_date"),
                "url": c.get("web_url"),
            }))
        return self._apply_since_and_limit(items, since_dt, limit)

    async def list_pull_requests(
        self, owner: str, repo: str, *, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        since_dt = parse_since(since)
        params: Dict[str, Any] = {"per_page": min(limit, 100), "order_by": "updated_at", "sort": "desc"}
        mapped_state = _STATE_MAP.get(state, state)
        if mapped_state:
            params["state"] = mapped_state
        if since_dt:
            params["updated_after"] = since_dt.isoformat()
        resp = await self._get(f"/projects/{self._project_id(owner, repo)}/merge_requests", params=params)
        items = []
        for mr in resp.json():
            dt = _parse_dt(mr.get("updated_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "number": mr.get("iid"),
                "title": mr.get("title"),
                "state": mr.get("state"),
                "author": (mr.get("author") or {}).get("username"),
                "created_at": mr.get("created_at"),
                "updated_at": mr.get("updated_at"),
                "merged": mr.get("state") == "merged",
                "url": mr.get("web_url"),
            }))
        return self._apply_since_and_limit(items, since_dt, limit)

    async def list_issues(
        self, owner: str, repo: str, *, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        since_dt = parse_since(since)
        params: Dict[str, Any] = {"per_page": min(limit, 100), "order_by": "updated_at", "sort": "desc"}
        mapped_state = _STATE_MAP.get(state, state)
        if mapped_state:
            params["state"] = mapped_state
        if since_dt:
            params["updated_after"] = since_dt.isoformat()
        resp = await self._get(f"/projects/{self._project_id(owner, repo)}/issues", params=params)
        items = []
        for issue in resp.json():
            dt = _parse_dt(issue.get("updated_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "number": issue.get("iid"),
                "title": issue.get("title"),
                "state": issue.get("state"),
                "author": (issue.get("author") or {}).get("username"),
                "created_at": issue.get("created_at"),
                "updated_at": issue.get("updated_at"),
                "url": issue.get("web_url"),
            }))
        return self._apply_since_and_limit(items, since_dt, limit)

    async def list_releases(self, owner: str, repo: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        resp = await self._get(f"/projects/{self._project_id(owner, repo)}/releases", params={"per_page": min(limit, 100)})
        items = []
        for r in resp.json():
            dt = _parse_dt(r.get("released_at") or r.get("created_at"))
            items.append(NormalizedItem(timestamp=dt, data={
                "tag": r.get("tag_name"),
                "name": r.get("name"),
                "created_at": r.get("created_at"),
                "published_at": r.get("released_at"),
                "draft": False,
                "prerelease": False,
                "url": (r.get("_links") or {}).get("self"),
            }))
        return self._apply_since_and_limit(items, None, limit)

    async def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        resp = await self._get(f"/projects/{self._project_id(owner, repo)}")
        r = resp.json()
        return {
            "full_name": r.get("path_with_namespace"),
            "description": r.get("description"),
            "default_branch": r.get("default_branch"),
            "stars": r.get("star_count"),
            "forks": r.get("forks_count"),
            "open_issues": r.get("open_issues_count"),
            "url": r.get("web_url"),
        }

    async def get_file(self, owner: str, repo: str, path: str, *, ref: Optional[str] = None) -> Dict[str, Any]:
        encoded_path = quote(path, safe="")
        params = {"ref": ref or "HEAD"}
        resp = await self._get(
            f"/projects/{self._project_id(owner, repo)}/repository/files/{encoded_path}", params=params,
        )
        body = resp.json()
        encoded = body.get("content", "")
        content = base64.b64decode(encoded).decode("utf-8", errors="replace") if encoded else ""
        return {
            "path": body.get("file_path"),
            "content": content,
            "encoding": "utf-8",
            "sha": body.get("blob_id"),
            "size": body.get("size"),
        }

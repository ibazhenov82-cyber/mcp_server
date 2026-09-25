"""
mcp_server.models
====================

Дата-классы сервера (только данные).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class GitHostStatus:
    host: str
    configured: bool
    api_url: str
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset_at: Optional[int] = None

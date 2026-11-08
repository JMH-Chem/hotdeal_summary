"""공용 HTTP 클라이언트."""

from __future__ import annotations

import httpx

from .config import get_settings


def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": settings.user_agent,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        },
    )

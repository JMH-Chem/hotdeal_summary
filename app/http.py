"""공용 HTTP 클라이언트 + 호스트별 요청 간격(robots.txt Crawl-delay 준수)."""

from __future__ import annotations

import asyncio
import time

import httpx

from .config import get_settings

# robots.txt 의 Crawl-delay (2026-11 확인). 없는 호스트는 DEFAULT_DELAY 간격.
CRAWL_DELAYS: dict[str, float] = {
    "search.danawa.com": 10.0,
    "eomisae.co.kr": 5.0,
}
DEFAULT_DELAY = 1.0
NO_DELAY_HOSTS = {"openapi.naver.com", "api-sg.aliexpress.com", "api.telegram.org", "discord.com"}

_last_request: dict[str, float] = {}
_locks: dict[tuple[int, str], asyncio.Lock] = {}


async def polite_wait(request: httpx.Request) -> None:
    host = request.url.host
    if host in NO_DELAY_HOSTS:
        return
    delay = CRAWL_DELAYS.get(host, DEFAULT_DELAY)
    key = (id(asyncio.get_running_loop()), host)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        wait = _last_request.get(host, 0.0) + delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request[host] = time.monotonic()


def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": settings.user_agent,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        },
        event_hooks={"request": [polite_wait]},
    )

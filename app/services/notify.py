"""알림: 앱 내 알림함 + (설정 시) 텔레그램/디스코드 웹훅."""

from __future__ import annotations

import logging
import sqlite3

import httpx

from ..config import get_settings
from ..db import utcnow_iso

log = logging.getLogger(__name__)


async def notify(conn: sqlite3.Connection, kind: str, title: str, body: str = "", url: str | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
    conn.execute(
        "INSERT INTO notifications (kind, title, body, url, created_at) VALUES (?,?,?,?,?)",
        (kind, title, body, url, utcnow_iso()),
    )
    conn.commit()
    settings = get_settings()
    text = f"[{kind}] {title}\n{body}\n{url or ''}".strip()
    if not (settings.telegram_bot_token and settings.telegram_chat_id) and not settings.discord_webhook_url:
        return
    own = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        if settings.telegram_bot_token and settings.telegram_chat_id:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    json={"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": False},
                )
            except httpx.HTTPError as exc:
                log.warning("telegram notify failed: %s", exc)
        if settings.discord_webhook_url:
            try:
                await client.post(settings.discord_webhook_url, json={"content": text[:1900]})
            except httpx.HTTPError as exc:
                log.warning("discord notify failed: %s", exc)
    finally:
        if own:
            await client.aclose()

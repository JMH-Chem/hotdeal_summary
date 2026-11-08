"""쇼핑 행사 캘린더(큐레이션 JSON) + 관련 핫딜/쿠폰 자동 연결."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..parsing import KST

EVENTS_FILE = DATA_DIR / "events.json"


@dataclass
class ShoppingEvent:
    slug: str
    name: str
    platform: str
    start: datetime
    end: datetime
    pre_start: datetime | None = None
    url: str | None = None
    keywords: list[str] = field(default_factory=list)
    verified: bool = False
    source: str = ""
    source_url: str | None = None
    highlights: list[str] = field(default_factory=list)

    def status(self, now: datetime | None = None) -> str:
        now = now or datetime.now(KST)
        if now > self.end:
            return "ended"
        if now >= self.start:
            return "ongoing"
        if self.pre_start and now >= self.pre_start:
            return "warmup"
        return "upcoming"

    def status_label(self, now: datetime | None = None) -> str:
        now = now or datetime.now(KST)
        st = self.status(now)
        if st == "ongoing":
            left = (self.end - now).days
            return f"진행중 · 종료 D-{left}" if left > 0 else "진행중 · 오늘 종료"
        if st == "warmup":
            return f"예열중 · 시작 D-{(self.start - now).days + 1}"
        if st == "upcoming":
            return f"D-{(self.start.date() - now.date()).days}"
        return "종료"


def load_events(path: Path = EVENTS_FILE) -> list[ShoppingEvent]:
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for e in raw:
        events.append(
            ShoppingEvent(
                slug=e["slug"], name=e["name"], platform=e.get("platform", ""),
                start=datetime.fromisoformat(e["start"]), end=datetime.fromisoformat(e["end"]),
                pre_start=datetime.fromisoformat(e["pre_start"]) if e.get("pre_start") else None,
                url=e.get("url"), keywords=e.get("keywords", []), verified=bool(e.get("verified")),
                source=e.get("source", ""), source_url=e.get("source_url"), highlights=e.get("highlights", []),
            )
        )
    order = {"ongoing": 0, "warmup": 1, "upcoming": 2, "ended": 3}
    events.sort(key=lambda ev: (order[ev.status()], ev.start))
    return events


def related_deals(conn: sqlite3.Connection, event: ShoppingEvent, limit: int = 20) -> list[sqlite3.Row]:
    if not event.keywords:
        return []
    clauses = " OR ".join("title LIKE ?" for _ in event.keywords)
    params = [f"%{k}%" for k in event.keywords]
    if event.platform == "AliExpress":
        clauses = f"({clauses}) AND is_aliexpress=1"
    return conn.execute(
        f"SELECT * FROM deals WHERE {clauses} ORDER BY posted_at DESC LIMIT ?", (*params, limit)
    ).fetchall()


def related_coupons(conn: sqlite3.Connection, event: ShoppingEvent) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM coupons WHERE event_slug=? ORDER BY COALESCE(min_spend, 0)", (event.slug,)
    ).fetchall()

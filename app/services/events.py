"""쇼핑 행사 캘린더(큐레이션 JSON) + 관련 핫딜/쿠폰 자동 연결."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


# 큐레이션 목록 밖의 행사도 잡기 위해, 최근 핫딜 제목에서 행사 키워드 언급량을 센다.
TRENDING_PATTERNS: list[tuple[str, str, str]] = [
    ("광군제 (11.11)", r"광군제|11\s*[.·]\s*11|싱글즈\s*데이", "광군제"),
    ("블랙프라이데이", r"블랙\s*프라이데이|블프|black\s*friday", "블프"),
    ("사이버먼데이", r"사이버\s*먼데이|cyber\s*monday", "사이버먼데이"),
    ("11번가 십일절", r"십일절", "십일절"),
    ("G마켓·옥션 빅스마일데이", r"빅스마일\s*데이|빅스마일", "빅스마일"),
    ("SSG 쓱데이", r"쓱\s*데이", "쓱데이"),
    ("코리아세일페스타", r"코리아\s*세일\s*페스타|코세페", "코세페"),
    ("올리브영 올영세일", r"올영\s*세일|올영\s*데이", "올영"),
    ("무신사 무진장", r"무진장|무신사\s*블프", "무진장"),
    ("아마존 프라임데이", r"프라임\s*데이|prime\s*day", "프라임데이"),
    ("12.12 (더블트웰브)", r"12\s*[.·]\s*12|더블\s*트웰브", "12.12"),
    ("618 쇼핑축제", r"\b618\b", "618"),
]


@dataclass
class TrendingEvent:
    name: str
    count: int
    query: str
    examples: list[sqlite3.Row]


def trending_events(conn: sqlite3.Connection, days: int = 7, now: datetime | None = None) -> list[TrendingEvent]:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).astimezone(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT * FROM deals WHERE posted_at >= ? ORDER BY posted_at DESC", (since,)
    ).fetchall()
    out = []
    for name, pattern, query in TRENDING_PATTERNS:
        rx = re.compile(pattern, re.I)
        hits = [r for r in rows if rx.search(r["title"])]
        if hits:
            out.append(TrendingEvent(name, len(hits), query, hits[:3]))
    out.sort(key=lambda t: -t.count)
    return out

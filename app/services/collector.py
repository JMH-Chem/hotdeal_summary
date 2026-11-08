"""핫딜 수집 파이프라인: 소스 조회 → 제목 파싱 → 저장(중복 제거) → 쿠폰 추출 → 키워드 알림."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

import httpx

from ..config import get_settings
from ..db import utcnow_iso
from ..http import make_client
from ..parsing import dedupe_key, guess_category, is_aliexpress, looks_like_coupon, parse_title
from ..sources.community import CommunitySource, RawDeal, enabled_sources
from .coupons import detect_platform, extract_coupons, save_coupons
from .events import ShoppingEvent, load_events
from .notify import notify
from .tracker import keyword_matches

log = logging.getLogger(__name__)
MAX_DETAIL_FETCH_PER_RUN = 10


def _active_events() -> list[ShoppingEvent]:
    try:
        return [e for e in load_events() if e.status() in ("ongoing", "warmup")]
    except (OSError, ValueError):
        return []


@dataclass
class SourceRun:
    source: str
    ok: bool
    fetched: int = 0
    inserted: int = 0
    coupons: int = 0
    message: str = ""


def store_deal(conn: sqlite3.Connection, raw: RawDeal, tags: tuple[str, ...] = ()) -> int | None:
    """새로 저장되면 row id, 이미 있으면 None."""
    parsed = parse_title(raw.title)
    ali = is_aliexpress(raw.title, parsed.mall) or "aliexpress" in tags
    coupon = "coupon" in tags or looks_like_coupon(raw.title)
    cur = conn.execute(
        """INSERT OR IGNORE INTO deals (source, source_name, board, external_id, title, url, mall, price, currency,
              shipping, category, author, summary, thumbnail, views, posted_at, collected_at, dedupe_key,
              is_aliexpress, is_coupon)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (raw.source, raw.source_name, raw.board, raw.external_id, raw.title, raw.url, parsed.mall, parsed.price,
         parsed.currency, parsed.shipping, guess_category(raw.title, raw.category), raw.author, raw.summary,
         raw.thumbnail, raw.views, (raw.posted_at.isoformat() if raw.posted_at else utcnow_iso()), utcnow_iso(),
         dedupe_key(raw.title, parsed.mall, parsed.price), int(ali), int(coupon)),
    )
    if cur.rowcount == 0:
        if raw.views is not None:
            conn.execute("UPDATE deals SET views=? WHERE source=? AND external_id=?",
                         (raw.views, raw.source, raw.external_id))
        return None
    return int(cur.lastrowid)


async def _coupon_pass(conn: sqlite3.Connection, client: httpx.AsyncClient, src: CommunitySource,
                       raw: RawDeal, budget: list[int]) -> int:
    text = f"{raw.title}\n{raw.summary or ''}"
    if budget[0] > 0 and src.detail_selector:
        budget[0] -= 1
        try:
            detail = await src.fetch_detail_text(client, raw.url)
            if detail:
                text = f"{raw.title}\n{detail}"
        except httpx.HTTPError as exc:
            log.info("detail fetch failed %s: %s", raw.url, exc)
        await asyncio.sleep(0.5)
    ali = "aliexpress" in src.tags or is_aliexpress(raw.title)
    platform = "AliExpress" if ali else detect_platform(raw.title, "기타")
    ev_slug = ev_end = None
    for ev in _active_events():
        if ev.platform == platform:
            ev_slug, ev_end = ev.slug, ev.end.date().isoformat()
            break
    found = extract_coupons(text, default_platform=platform, source_name=f"{src.name} {src.board}",
                            title=raw.title, event_slug=ev_slug, event_end=ev_end)
    return save_coupons(conn, found, raw.url) if found else 0


async def _keyword_pass(conn: sqlite3.Connection, raw: RawDeal, client: httpx.AsyncClient) -> None:
    parsed = parse_title(raw.title)
    for alert in conn.execute("SELECT * FROM keyword_alerts").fetchall():
        if not keyword_matches(alert["keyword"], raw.title):
            continue
        if alert["max_price"] and (parsed.price is None or parsed.currency != "KRW" or parsed.price > alert["max_price"]):
            continue
        await notify(conn, "키워드핫딜", raw.title, f"{raw.source_name} {raw.board} · 키워드 '{alert['keyword']}'",
                     raw.url, client)


async def collect_source(conn: sqlite3.Connection, client: httpx.AsyncClient, src: CommunitySource,
                         budget: list[int], first_run: bool) -> SourceRun:
    run = SourceRun(src.key, ok=True)
    try:
        raws = await src.fetch(client)
    except Exception as exc:  # noqa: BLE001
        run.ok, run.message = False, f"{type(exc).__name__}: {exc}"[:300]
        return run
    run.fetched = len(raws)
    for raw in raws:
        new_id = store_deal(conn, raw, src.tags)
        if new_id is None:
            continue
        run.inserted += 1
        if "coupon" in src.tags or looks_like_coupon(raw.title, raw.summary):
            run.coupons += await _coupon_pass(conn, client, src, raw, budget)
        if not first_run:  # 최초 적재 때는 과거 글로 알림 폭탄이 가지 않도록 생략
            await _keyword_pass(conn, raw, client)
    conn.commit()
    return run


async def collect_all(conn: sqlite3.Connection, only: tuple[str, ...] = ()) -> list[SourceRun]:
    settings = get_settings()
    sources = enabled_sources(only or settings.enabled_sources)
    first_run = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 0
    budget = [MAX_DETAIL_FETCH_PER_RUN]
    runs: list[SourceRun] = []
    async with make_client() as client:
        for src in sources:
            run = await collect_source(conn, client, src, budget, first_run)
            runs.append(run)
            conn.execute(
                """INSERT INTO source_status (source, last_run, ok, fetched, inserted, message) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, ok=excluded.ok,
                   fetched=excluded.fetched, inserted=excluded.inserted, message=excluded.message""",
                (src.key, utcnow_iso(), int(run.ok), run.fetched, run.inserted, run.message),
            )
            conn.commit()
    return runs

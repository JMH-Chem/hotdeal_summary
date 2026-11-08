"""가격 추적(폴센트 스타일): 주기적으로 가격을 기록하고 목표가/급락/역대최저 알림."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import httpx

from ..config import get_settings
from ..db import utcnow_iso
from ..http import make_client
from ..parsing import format_won
from ..sources.shopping import aliexpress_product
from .compare import compare, landed_price
from .notify import notify

log = logging.getLogger(__name__)


@dataclass
class PriceReading:
    price: int
    mall: str | None
    provider: str | None
    url: str | None


def evaluate_alerts(prev_last: int | None, prev_lowest: int | None, new_price: int,
                    target_price: int | None, drop_percent: float | None, history_count: int) -> list[str]:
    """순수 함수: 어떤 알림을 보낼지 결정. history_count는 이번 기록 이전의 기록 수."""
    reasons = []
    if target_price and new_price <= target_price and (prev_last is None or prev_last > target_price):
        reasons.append(f"목표가 도달: {format_won(new_price)} (목표 {format_won(target_price)})")
    if drop_percent and prev_last and new_price < prev_last:
        pct = (prev_last - new_price) / prev_last * 100
        if pct >= drop_percent:
            reasons.append(f"가격 {pct:.1f}% 하락: {format_won(prev_last)} → {format_won(new_price)}")
    if history_count >= 2 and prev_lowest is not None and new_price < prev_lowest:
        reasons.append(f"역대 최저가 갱신: {format_won(new_price)} (이전 최저 {format_won(prev_lowest)})")
    return reasons


async def read_price(item: sqlite3.Row, client: httpx.AsyncClient) -> PriceReading | None:
    if item["kind"] == "aliexpress":
        offer = await aliexpress_product(client, get_settings(), item["product_ref"])
        if offer is None:
            return None
        return PriceReading(offer.price, offer.mall, "aliexpress", offer.url)
    result = await compare(item["query"] or item["name"], client=client, use_cache=False)
    if not result.cheapest:
        return None
    best = result.cheapest
    return PriceReading(landed_price(best), best.mall, best.provider, best.url)


def add_item(conn: sqlite3.Connection, *, name: str, kind: str, query: str | None = None,
             product_ref: str | None = None, url: str | None = None, target_price: int | None = None,
             drop_percent: float | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO tracked_items (name, kind, query, product_ref, url, target_price, drop_percent, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name, kind, query, product_ref, url, target_price, drop_percent, utcnow_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


async def record_price(conn: sqlite3.Connection, item: sqlite3.Row, reading: PriceReading) -> list[str]:
    history_count = conn.execute("SELECT COUNT(*) FROM price_history WHERE item_id=?", (item["id"],)).fetchone()[0]
    reasons = evaluate_alerts(item["last_price"], item["lowest_price"], reading.price,
                              item["target_price"], item["drop_percent"], history_count)
    now = utcnow_iso()
    conn.execute("INSERT INTO price_history (item_id, price, mall, provider, checked_at) VALUES (?,?,?,?,?)",
                 (item["id"], reading.price, reading.mall, reading.provider, now))
    lowest = min(x for x in (item["lowest_price"], reading.price) if x is not None)
    highest = max(x for x in (item["highest_price"], reading.price) if x is not None)
    conn.execute(
        """UPDATE tracked_items SET last_price=?, last_mall=?, lowest_price=?, highest_price=?, last_checked=?,
           url=COALESCE(url, ?) WHERE id=?""",
        (reading.price, reading.mall, lowest, highest, now, reading.url, item["id"]),
    )
    conn.commit()
    for reason in reasons:
        await notify(conn, "가격알림", f"{item['name']}", f"{reason} · {reading.mall or ''}", reading.url)
    return reasons


async def check_item(conn: sqlite3.Connection, item_id: int, client: httpx.AsyncClient | None = None) -> list[str]:
    item = conn.execute("SELECT * FROM tracked_items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        return []
    own = client is None
    client = client or make_client()
    try:
        reading = await read_price(item, client)
    finally:
        if own:
            await client.aclose()
    if reading is None:
        conn.execute("UPDATE tracked_items SET last_checked=? WHERE id=?", (utcnow_iso(), item_id))
        conn.commit()
        return []
    return await record_price(conn, item, reading)


async def check_all(conn: sqlite3.Connection) -> int:
    ids = [r[0] for r in conn.execute("SELECT id FROM tracked_items WHERE active=1")]
    async with make_client() as client:
        for item_id in ids:
            try:
                await check_item(conn, item_id, client)
            except Exception as exc:  # noqa: BLE001 - 개별 실패가 전체를 막지 않도록
                log.warning("price check failed for %s: %s", item_id, exc)
    return len(ids)


def keyword_matches(keyword: str, title: str) -> bool:
    hay = title.lower().replace(" ", "")
    return all(tok in hay for tok in keyword.lower().split())

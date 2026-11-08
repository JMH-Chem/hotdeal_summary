"""FastAPI 웹앱: 핫딜 / 가격비교 / 가격추적 / 행사 / 쿠폰."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .db import connect, init_db, utcnow_iso
from .parsing import KST, format_won, to_kst_str
from .services.collector import collect_all
from .services.compare import compare, landed_price
from .services.customs import DUTY_RATES
from .services.events import load_events, related_coupons, related_deals
from .services.tracker import add_item, check_all, check_item
from .sources.community import SOURCES
from .sources.shopping import aliexpress_product_id

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["won"] = format_won
templates.env.filters["kst"] = to_kst_str
PAGE_SIZE = 40


def db():
    conn = connect()
    init_db(conn)
    return conn


async def _loop(name: str, interval: int, job) -> None:
    await asyncio.sleep(3)
    while True:
        try:
            conn = db()
            try:
                await job(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s job failed: %s", name, exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    conn = db()
    conn.close()
    tasks = []
    if settings.enable_scheduler:
        tasks.append(asyncio.create_task(_loop("collect", settings.collect_interval_sec, collect_all)))
        tasks.append(asyncio.create_task(_loop("prices", settings.price_check_interval_sec, check_all)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="핫딜 요약", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    conn = db()
    try:
        unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    finally:
        conn.close()
    return templates.TemplateResponse(request, name, {"unread": unread, "now": datetime.now(KST), **ctx})


# ------------------------------------------------------------------ 핫딜

def query_deals(conn, q: str = "", source: str = "", only_ali: bool = False, only_coupon: bool = False,
                page: int = 1, dedupe: bool = True):
    where, params = [], []
    if q:
        for tok in q.split():
            where.append("title LIKE ?")
            params.append(f"%{tok}%")
    if source:
        where.append("source_name = ?")
        params.append(source)
    if only_ali:
        where.append("is_aliexpress = 1")
    if only_coupon:
        where.append("is_coupon = 1")
    sql = "SELECT * FROM deals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if dedupe:
        sql = f"SELECT * FROM ({sql}) GROUP BY COALESCE(dedupe_key, id)"
    sql += " ORDER BY posted_at DESC LIMIT ? OFFSET ?"
    params += [PAGE_SIZE, (page - 1) * PAGE_SIZE]
    return conn.execute(sql, params).fetchall()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", source: str = "", ali: int = 0, coupon: int = 0, page: int = Query(1, ge=1)):
    conn = db()
    try:
        deals = query_deals(conn, q, source, bool(ali), bool(coupon), page)
        source_names = [r[0] for r in conn.execute("SELECT DISTINCT source_name FROM deals ORDER BY 1")]
        coupons = conn.execute("SELECT * FROM coupons ORDER BY last_seen DESC LIMIT 8").fetchall()
        status = conn.execute("SELECT * FROM source_status ORDER BY source").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    finally:
        conn.close()
    events = [e for e in load_events() if e.status() != "ended"][:3]
    return render(request, "index.html", deals=deals, q=q, source=source, ali=ali, coupon=coupon, page=page,
                  source_names=source_names, coupons=coupons, events=events, status=status, total=total,
                  page_size=PAGE_SIZE)


@app.post("/admin/collect")
async def admin_collect():
    conn = db()
    try:
        runs = await collect_all(conn)
    finally:
        conn.close()
    return JSONResponse([asdict(r) for r in runs])


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    conn = db()
    try:
        status = {r["source"]: r for r in conn.execute("SELECT * FROM source_status")}
    finally:
        conn.close()
    return render(request, "status.html", sources=SOURCES, status=status, settings=get_settings())


# ------------------------------------------------------------------ 쿠폰 / 행사

@app.get("/coupons", response_class=HTMLResponse)
def coupons_page(request: Request, platform: str = "", event: str = ""):
    conn = db()
    try:
        where, params = [], []
        if platform:
            where.append("platform = ?")
            params.append(platform)
        if event:
            where.append("event_slug = ?")
            params.append(event)
        sql = "SELECT * FROM coupons" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY (valid_until IS NOT NULL AND valid_until < date('now')), platform, COALESCE(min_spend, 0)"
        rows = conn.execute(sql, params).fetchall()
        platforms = [r[0] for r in conn.execute("SELECT DISTINCT platform FROM coupons ORDER BY 1")]
        coupon_posts = conn.execute("SELECT * FROM deals WHERE is_coupon=1 ORDER BY posted_at DESC LIMIT 30").fetchall()
    finally:
        conn.close()
    events = {e.slug: e for e in load_events()}
    today = datetime.now(KST).date().isoformat()
    return render(request, "coupons.html", coupons=rows, platforms=platforms, platform=platform, event=event,
                  events=events, today=today, coupon_posts=coupon_posts)


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    conn = db()
    try:
        events = load_events()
        blocks = [(e, related_coupons(conn, e), related_deals(conn, e, 12)) for e in events]
    finally:
        conn.close()
    return render(request, "events.html", blocks=blocks)


# ------------------------------------------------------------------ 가격비교

@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request, q: str = "", category: str = "general"):
    result = await compare(q, category) if q.strip() else None
    return render(request, "compare.html", q=q, result=result, category=category, categories=DUTY_RATES,
                  landed=landed_price, settings=get_settings())


@app.get("/api/compare")
async def api_compare(q: str, category: str = "general"):
    result = await compare(q, category)
    offers = []
    for o in result.offers:
        d = asdict(o)
        tax = o.extra.get("tax")
        d["extra"] = {k: v for k, v in o.extra.items() if k != "tax"}
        d["extra"]["tax"] = asdict(tax) if tax else None
        offers.append(d)
    return {"query": result.query, "offers": offers,
            "providers": [{"provider": p.provider, "ok": p.ok, "message": p.message, "count": len(p.offers)}
                          for p in result.providers]}


# ------------------------------------------------------------------ 가격추적 / 알림

def _int_or_none(value: str | None) -> int | None:
    value = (value or "").replace(",", "").strip()
    return int(value) if value.isdigit() else None


@app.get("/track", response_class=HTMLResponse)
def track_page(request: Request):
    conn = db()
    try:
        items = conn.execute("SELECT * FROM tracked_items ORDER BY active DESC, id DESC").fetchall()
        keywords = conn.execute("SELECT * FROM keyword_alerts ORDER BY id DESC").fetchall()
        histories = {
            it["id"]: [r["price"] for r in conn.execute(
                "SELECT price FROM price_history WHERE item_id=? ORDER BY checked_at DESC LIMIT 60", (it["id"],))][::-1]
            for it in items
        }
    finally:
        conn.close()
    return render(request, "track.html", items=items, keywords=keywords, histories=histories,
                  ali_enabled=get_settings().ali_enabled)


@app.post("/track")
async def track_add(name: str = Form(""), target: str = Form(""), target_price: str = Form(""),
                    drop_percent: str = Form("")):
    target = target.strip()
    if not target:
        raise HTTPException(400, "검색어 또는 알리 상품 URL을 입력하세요")
    ali_id = aliexpress_product_id(target) if "aliexpress" in target or target.isdigit() else None
    conn = db()
    try:
        item_id = add_item(
            conn,
            name=name.strip() or target[:60],
            kind="aliexpress" if ali_id else "query",
            query=None if ali_id else target,
            product_ref=ali_id,
            url=target if target.startswith("http") else None,
            target_price=_int_or_none(target_price),
            drop_percent=float(drop_percent) if drop_percent.strip() else None,
        )
        try:
            await check_item(conn, item_id)
        except Exception as exc:  # noqa: BLE001 - 첫 조회 실패해도 등록은 유지
            log.warning("initial check failed: %s", exc)
    finally:
        conn.close()
    return RedirectResponse(f"/track/{item_id}", status_code=303)


@app.get("/track/{item_id}", response_class=HTMLResponse)
def track_detail(request: Request, item_id: int):
    conn = db()
    try:
        item = conn.execute("SELECT * FROM tracked_items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise HTTPException(404)
        history = conn.execute("SELECT * FROM price_history WHERE item_id=? ORDER BY checked_at", (item_id,)).fetchall()
    finally:
        conn.close()
    return render(request, "track_detail.html", item=item, history=history)


@app.post("/track/{item_id}/check")
async def track_check(item_id: int):
    conn = db()
    try:
        await check_item(conn, item_id)
    finally:
        conn.close()
    return RedirectResponse(f"/track/{item_id}", status_code=303)


@app.post("/track/{item_id}/delete")
def track_delete(item_id: int):
    conn = db()
    try:
        conn.execute("DELETE FROM tracked_items WHERE id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/track", status_code=303)


@app.post("/alerts/keywords")
def keyword_add(keyword: str = Form(...), max_price: str = Form("")):
    conn = db()
    try:
        conn.execute("INSERT OR IGNORE INTO keyword_alerts (keyword, max_price, created_at) VALUES (?,?,?)",
                     (keyword.strip(), _int_or_none(max_price), utcnow_iso()))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/track", status_code=303)


@app.post("/alerts/keywords/{alert_id}/delete")
def keyword_delete(alert_id: int):
    conn = db()
    try:
        conn.execute("DELETE FROM keyword_alerts WHERE id=?", (alert_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/track", status_code=303)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    conn = db()
    try:
        rows = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 200").fetchall()
        conn.execute("UPDATE notifications SET is_read=1 WHERE is_read=0")
        conn.commit()
    finally:
        conn.close()
    return render(request, "notifications.html", rows=rows)


# ------------------------------------------------------------------ JSON API

@app.get("/api/deals")
def api_deals(q: str = "", source: str = "", ali: int = 0, coupon: int = 0, page: int = Query(1, ge=1)):
    conn = db()
    try:
        return [dict(r) for r in query_deals(conn, q, source, bool(ali), bool(coupon), page)]
    finally:
        conn.close()


@app.get("/api/coupons")
def api_coupons(platform: str = ""):
    conn = db()
    try:
        sql, params = "SELECT * FROM coupons", []
        if platform:
            sql, params = sql + " WHERE platform=?", [platform]
        return [dict(r) for r in conn.execute(sql + " ORDER BY last_seen DESC", params)]
    finally:
        conn.close()


@app.get("/api/events")
def api_events():
    return [{**{k: v for k, v in asdict(e).items() if k not in ("start", "end", "pre_start")},
             "start": e.start.isoformat(), "end": e.end.isoformat(),
             "pre_start": e.pre_start.isoformat() if e.pre_start else None,
             "status": e.status()} for e in load_events()]

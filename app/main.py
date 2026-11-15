"""FastAPI 웹앱: 핫딜 / 가격비교 / 가격추적 / 행사 / 쿠폰."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .db import connect, init_db, utcnow_iso
from .parsing import KST, deal_search_query, format_won, to_kst_str
from .services.collector import collect_all
from .services.compare import compare, landed_price
from .services.customs import DUTY_RATES
from .services.deals import PAGE_SIZE, query_deals
from .services.events import load_events, related_coupons, related_deals, trending_events
from .services import research as research_service
from .services.evidence import is_safe_link, kst_today
from .services.tracker import add_item, check_all, check_item
from .sources.community import SOURCES
from .sources.shopping import aliexpress_product_id

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["won"] = format_won
templates.env.filters["kst"] = to_kst_str
templates.env.filters["deal_query"] = deal_search_query
templates.env.filters["safe_url"] = lambda url: url if is_safe_link(url) else "#"  # javascript: 등 차단
templates.env.globals.update(
    VERIFY_LABELS={"system": "수집기 확인", "page": "페이지 확인", "citation": "검색 인용 확인",
                   "stale": "오래된 정보", "unverified": "미확인", "url_unseen": "출처 불명 링크"},
    FLAG_LABELS={"suspicious_low": "비정상 저가 의심", "unknown_shop": "처음 보는 판매처 · 사기몰 주의",
                 "list_page": "목록 페이지 기준", "community": "커뮤니티 글 기준", "unsafe_link": "위험 링크 제거"},
    CHANNEL_LABELS={"open_market": "오픈마켓", "mall": "종합몰", "home_shopping": "홈쇼핑", "brand_store": "브랜드몰",
                    "price_compare": "가격비교", "community": "커뮤니티", "overseas": "해외직구", "other": "기타"},
    MATCH_LABELS={"exact": "정확히 일치", "close": "비슷함", "different": "다른 상품"},
    STATUS_LABELS={"queued": "대기 중", "running": "조사 중", "done": "완료", "error": "실패", "interrupted": "중단됨"},
)
_initialized_paths: set[str] = set()


def db():
    """요청마다 새 연결. 스키마 생성/마이그레이션은 DB 경로당 한 번만."""
    path = get_settings().db_path
    fresh = path not in _initialized_paths or (path != ":memory:" and not Path(path).exists())
    conn = connect(path)
    if fresh:
        init_db(conn)
        _initialized_paths.add(path)
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
    try:
        swept = research_service.sweep_interrupted(conn)  # 워커 1개 전제: 이전 프로세스의 미완료 조사 정리
        if swept:
            log.info("marked %s unfinished AI research runs as interrupted", swept)
    finally:
        conn.close()
    tasks = []
    if settings.enable_scheduler:
        tasks.append(asyncio.create_task(_loop("collect", settings.collect_interval_sec, collect_all)))
        tasks.append(asyncio.create_task(_loop("prices", settings.price_check_interval_sec, check_all)))
        # 1시간마다 깨어나 KST AI_DAILY_HOUR 이후 '오늘 아직 조사 안 한' 추적 상품만 조사 (재시작해도 중복 없음)
        tasks.append(asyncio.create_task(_loop("ai_daily", 3600, research_service.daily_job)))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await research_service.shutdown_tasks()


app = FastAPI(title="핫딜 요약", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    conn = db()
    try:
        unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    finally:
        conn.close()
    return templates.TemplateResponse(request, name, {"unread": unread, "now": datetime.now(KST), **ctx})


def require_same_origin(request: Request) -> None:
    """돈이 드는 POST(AI 조사)는 다른 사이트에서 보낸 요청(CSRF)을 거부한다."""
    host = request.headers.get("host", "")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            if urlsplit(value).netloc != host:
                raise HTTPException(403, "다른 사이트에서 보낸 요청은 처리하지 않습니다")
            return


# ------------------------------------------------------------------ 핫딜

@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", source: str = "", ali: int = 0, coupon: int = 0, sort: str = "new",
          page: int = Query(1, ge=1)):
    conn = db()
    try:
        deals = query_deals(conn, q, source, bool(ali), bool(coupon), page, sort=sort)
        source_names = [r[0] for r in conn.execute("SELECT DISTINCT source_name FROM deals ORDER BY 1")]
        coupons = conn.execute("SELECT * FROM coupons ORDER BY last_seen DESC LIMIT 8").fetchall()
        status = conn.execute("SELECT * FROM source_status ORDER BY source").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    finally:
        conn.close()
    events = [e for e in load_events() if e.status() != "ended"][:3]
    return render(request, "index.html", deals=deals, q=q, source=source, ali=ali, coupon=coupon, sort=sort, page=page,
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
        budget = _ai_budget(conn)
    finally:
        conn.close()
    return render(request, "status.html", sources=SOURCES, status=status, settings=get_settings(), ai=budget)


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
        trending = trending_events(conn)
    finally:
        conn.close()
    return render(request, "events.html", blocks=blocks, trending=trending)


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


# ------------------------------------------------------------------ AI 최저가 탐색

def _ai_budget(conn) -> dict:
    settings = get_settings()
    today = kst_today()
    spent = research_service.today_spent(conn, today)
    return {"configured": settings.ai_configured, "model": settings.ai_model, "effort": settings.ai_effort,
            "budget": settings.ai_daily_budget_usd, "spent": spent, "reserve": settings.ai_run_reserve_usd,
            "left": max(0.0, settings.ai_daily_budget_usd - spent), "daily_hour": settings.ai_daily_hour,
            "today": today, "max_searches": settings.ai_max_searches}


@app.get("/research", response_class=HTMLResponse)
def research_page(request: Request, q: str = "", category: str = "general", error: str = ""):
    conn = db()
    try:
        runs = conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT 40").fetchall()
        budget = _ai_budget(conn)
        active = conn.execute("SELECT * FROM research_runs WHERE status IN ('queued','running') LIMIT 1").fetchone()
    finally:
        conn.close()
    return render(request, "research.html", runs=runs, ai=budget, q=q, category=category, categories=DUTY_RATES,
                  error=error, active=active)


@app.post("/research")
async def research_start(request: Request, q: str = Form(""), category: str = Form("general"),
                         include_overseas: str = Form(""), allow_used: str = Form(""), force: str = Form(""),
                         item_id: str = Form("")):
    require_same_origin(request)
    conn = db()
    try:
        result = research_service.create_run(
            conn, q, category=category if category in DUTY_RATES else "general",
            include_overseas=bool(include_overseas), allow_used=bool(allow_used), force=bool(force),
            item_id=int(item_id) if item_id.isdigit() else None,
        )
    finally:
        conn.close()
    if result.error:
        target = f"/research?q={quote(q)}&error={quote(result.error)}"
        return RedirectResponse(target, status_code=303)
    if not result.reused:
        research_service.start_run_task(result.run_id)
    return RedirectResponse(f"/research/{result.run_id}", status_code=303)


@app.get("/research/{run_id}", response_class=HTMLResponse)
def research_detail(request: Request, run_id: int):
    conn = db()
    try:
        view = research_service.run_view(conn, run_id)
        if view is None:
            raise HTTPException(404)
        budget = _ai_budget(conn)
    finally:
        conn.close()
    return render(request, "research_detail.html", ai=budget, **view)


@app.get("/api/research/{run_id}")
def api_research(run_id: int):
    conn = db()
    try:
        view = research_service.run_view(conn, run_id)
        if view is None:
            raise HTTPException(404)
        ranking = view["ranking"]
        return {
            "run": dict(view["run"]),
            "options": view["options"],
            "best": ranking.best if ranking else None,
            "confirmed": ranking.confirmed if ranking else [],
            "cheaper_unconfirmed": ranking.cheaper_unconfirmed if ranking else [],
            "others": ranking.others if ranking else [],
            "sources": [dict(s) for s in view["sources"]],
            "diagnostics": view["diagnostics"],
        }
    finally:
        conn.close()


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
async def track_add(request: Request, name: str = Form(""), target: str = Form(""), target_price: str = Form(""),
                    drop_percent: str = Form(""), ai_daily: str = Form("")):
    require_same_origin(request)
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
        if ai_daily:
            conn.execute("UPDATE tracked_items SET ai_daily=1 WHERE id=?", (item_id,))
            conn.commit()
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
        ai_runs = conn.execute(
            """SELECT r.*, c.seller AS best_seller, c.url AS best_url, c.verification AS best_verification
               FROM research_runs r LEFT JOIN research_candidates c ON c.id = r.best_candidate_id
               WHERE r.item_id=? ORDER BY r.id DESC LIMIT 30""", (item_id,)).fetchall()
        budget = _ai_budget(conn)
    finally:
        conn.close()
    return render(request, "track_detail.html", item=item, history=history, ai_runs=ai_runs, ai=budget)


@app.post("/track/{item_id}/ai")
def track_ai_toggle(request: Request, item_id: int, enabled: str = Form("")):
    require_same_origin(request)
    conn = db()
    try:
        conn.execute("UPDATE tracked_items SET ai_daily=? WHERE id=?", (1 if enabled else 0, item_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/track/{item_id}", status_code=303)


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

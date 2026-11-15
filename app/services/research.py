"""AI 최저가 조사 실행 관리.

- 생성: 하루 예산·동시 실행 1개·같은 날 같은 질의 재사용을 한 트랜잭션(BEGIN IMMEDIATE)으로 확인
- 실행: 작업마다 새 DB 연결, 쓰기는 짧게 커밋(쓰기 트랜잭션을 연 채 await 금지), 응답마다 사용량 저장
- 저장: 우리 수집기 결과(S#) + AI 후보(C#)를 검증 등급과 함께 저장하고 '확인된 오늘의 최저가'를 고름
- 추적 상품: '매일 AI 조사'를 켠 상품은 하루 한 번 조사하고, AI 일별 최저가끼리 비교해 알림
단일 프로세스(uvicorn 워커 1개) 실행을 전제로 한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..config import get_settings
from ..db import connect, utcnow_iso
from ..parsing import KST
from .ai_research import ResearchError, ResearchInput, ResearchOutcome, research
from .compare import compare, landed_price
from .costs import UsageTotals
from .deals import query_deals
from .evidence import Ranking, is_safe_link, kst_today, rank_candidates, verify_candidate
from .notify import notify
from .tracker import evaluate_alerts

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
Runner = Callable[..., Awaitable[ResearchOutcome]]
_tasks: set[asyncio.Task] = set()


# ------------------------------------------------------------------ 생성 (예산·중복·동시 실행)

def query_key(query: str, category: str = "general", include_overseas: bool = True, allow_used: bool = False) -> str:
    norm = re.sub(r"[^0-9a-z가-힣]+", " ", query.lower()).strip()
    return f"{norm}|{category}|{int(include_overseas)}|{int(allow_used)}"


def today_spent(conn: sqlite3.Connection, run_date: str, exclude_id: int | None = None) -> float:
    """오늘 쓴 돈: 끝난 실행은 실제 비용, 진행 중인 실행은 max(비용, 예약금)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(MAX(cost_usd, CASE WHEN status IN ('queued','running') THEN reserved_usd ELSE 0 END)), 0)
           FROM research_runs WHERE run_date=? AND id != ?""",
        (run_date, exclude_id or -1),
    ).fetchone()
    return float(row[0] or 0)


@dataclass
class CreateResult:
    run_id: int | None
    reused: bool = False
    error: str | None = None


def create_run(conn: sqlite3.Connection, query: str, *, category: str = "general", include_overseas: bool = True,
               allow_used: bool = False, item_id: int | None = None, force: bool = False,
               now: datetime | None = None) -> CreateResult:
    settings = get_settings()
    query = " ".join(query.split())[:200]
    if not query:
        return CreateResult(None, error="상품명을 입력하세요.")
    if not settings.ai_configured:
        return CreateResult(None, error="AI 조사를 쓰려면 ANTHROPIC_API_KEY를 설정하세요 (또는 AI_ENABLED=1).")
    today = kst_today(now)
    key = query_key(query, category, include_overseas, allow_used)
    conn.commit()  # 열린 암묵 트랜잭션이 있으면 정리
    conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT id, query_key FROM research_runs WHERE status IN ('queued','running') ORDER BY id LIMIT 1"
        ).fetchone()
        if active:
            conn.rollback()
            if active["query_key"] == key:
                return CreateResult(active["id"], reused=True)
            return CreateResult(active["id"], error="다른 AI 조사가 진행 중입니다. 끝난 뒤 다시 시도하세요.")
        if not force:
            done = conn.execute(
                "SELECT id FROM research_runs WHERE run_date=? AND query_key=? AND status='done' ORDER BY id DESC LIMIT 1",
                (today, key),
            ).fetchone()
            if done:
                conn.rollback()
                return CreateResult(done["id"], reused=True)
        spent = today_spent(conn, today)
        if spent + settings.ai_run_reserve_usd > settings.ai_daily_budget_usd:
            conn.rollback()
            return CreateResult(None, error=f"오늘 AI 조사 예산(${settings.ai_daily_budget_usd:.2f})을 다 썼습니다 "
                                            f"(사용 약 ${spent:.2f}). AI_DAILY_BUDGET_USD로 조정할 수 있습니다.")
        options = {"category": category, "include_overseas": include_overseas, "allow_used": allow_used}
        cur = conn.execute(
            """INSERT INTO research_runs (query, query_key, options, status, run_date, model, item_id, reserved_usd,
                                          created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (query, key, json.dumps(options), "queued", today, settings.ai_model, item_id,
             settings.ai_run_reserve_usd, utcnow_iso()),
        )
        conn.commit()
        return CreateResult(int(cur.lastrowid))
    except BaseException:
        conn.rollback()
        raise


# ------------------------------------------------------------------ 실행

def related_deals_for(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[sqlite3.Row]:
    toks = [t for t in query.split() if len(t) >= 2]
    rows = query_deals(conn, " ".join(toks), limit=limit) if toks else []
    if not rows:
        model_toks = [t for t in toks if re.search(r"\d", t) and len(t) >= 3]
        if model_toks:
            rows = query_deals(conn, " ".join(model_toks), limit=limit)
    return rows


async def build_input(conn: sqlite3.Connection, run: sqlite3.Row) -> ResearchInput:
    opts = json.loads(run["options"] or "{}")
    category = opts.get("category", "general")
    include_overseas = bool(opts.get("include_overseas", True))
    result = await compare(run["query"], category)  # 네트워크 대기 동안 DB 쓰기 트랜잭션 없음
    baseline = []
    for offer in result.offers:  # 캐시된 결과를 공유하므로 읽기만 한다
        if offer.is_overseas and not include_overseas:
            continue
        baseline.append({
            "ref": f"S{len(baseline) + 1}", "provider": offer.provider, "provider_name": offer.provider_name,
            "mall": offer.mall, "title": offer.title, "price": offer.price, "shipping": offer.shipping,
            "landed_price": landed_price(offer), "url": offer.url, "is_overseas": offer.is_overseas,
        })
        if len(baseline) >= 10:
            break
    deals = [{"ref": f"D{i}", "source": f"{r['source_name']} {r['board'] or ''}".strip(), "title": r["title"],
              "url": r["url"], "posted": (r["posted_at"] or "")[:10]}
             for i, r in enumerate(related_deals_for(conn, run["query"]), 1)]
    return ResearchInput(query=run["query"], today=run["run_date"], category=category,
                         include_overseas=include_overseas, allow_used=bool(opts.get("allow_used", False)),
                         baseline=baseline, hot_deals=deals)


def save_usage(conn: sqlite3.Connection, run_id: int, usage: UsageTotals) -> None:
    conn.execute(
        """UPDATE research_runs SET cost_usd=?, input_tokens=?, output_tokens=?, cache_read_tokens=?,
           cache_write_tokens=?, web_searches=?, web_fetches=? WHERE id=?""",
        (round(usage.cost_usd, 6), usage.input_tokens, usage.output_tokens, usage.cache_read, usage.cache_write,
         usage.web_searches, usage.web_fetches, run_id),
    )
    conn.commit()


def _finish_error(conn: sqlite3.Connection, run_id: int, status: str, message: str, charge_reserve: bool) -> None:
    conn.rollback()
    cost_sql = "MAX(cost_usd, reserved_usd)" if charge_reserve else "cost_usd"
    conn.execute(f"UPDATE research_runs SET status=?, error=?, finished_at=?, cost_usd={cost_sql} WHERE id=?",
                 (status, message[:500], utcnow_iso(), run_id))
    conn.commit()


def candidates_for_ranking(inp: ResearchInput, outcome: ResearchOutcome, usd_krw: float,
                           now: datetime) -> list[dict[str, Any]]:
    baseline_match = {b.ref: b.match for b in outcome.findings.baseline}
    cands: list[dict[str, Any]] = []
    for b in inp.baseline:
        cand = {
            "ref": b["ref"], "origin": "system", "seller": b.get("mall") or b["provider_name"],
            "channel": "overseas" if b["is_overseas"] else "price_compare", "title": b["title"], "url": b["url"],
            "price": b["price"], "shipping": b["shipping"] if b.get("shipping") is not None else -1,
            "conditional_price": -1, "conditions": "", "original_currency": "KRW", "original_amount": -1,
            "is_overseas": b["is_overseas"], "origin_country": "CN" if b["provider"] == "aliexpress" else "KR",
            "tax_included": "no", "match": baseline_match.get(b["ref"], "close"), "in_stock": "unknown",
            "time_limited": "", "evidence": f"{b['provider_name']} 수집기", "notes": "",
        }
        cands.append(cand)
    for c in outcome.candidates:
        price, notes = c.price_krw, c.notes
        if c.original_currency == "USD" and c.original_amount > 0:
            price = int(round(c.original_amount * usd_krw))  # 환산은 우리가 직접
            notes = f"{notes} (${c.original_amount:g} × {usd_krw:g}원)".strip()
        elif c.original_currency not in ("KRW", "USD") and c.original_amount > 0:
            notes = f"{notes} (원화 금액은 AI 환산)".strip()
        origin = (c.origin_country or "UNKNOWN").upper()
        cands.append({
            "ref": c.ref, "origin": "ai", "seller": c.seller, "channel": c.channel, "title": c.title, "url": c.url,
            "price": price, "shipping": c.shipping_krw, "conditional_price": c.conditional_price_krw,
            "conditions": c.conditions, "original_currency": c.original_currency, "original_amount": c.original_amount,
            "is_overseas": c.channel == "overseas" or origin not in ("KR", "UNKNOWN", ""), "origin_country": origin,
            "tax_included": c.tax_included, "match": c.match, "in_stock": c.in_stock, "time_limited": c.time_limited,
            "evidence": c.evidence, "notes": " ".join(x for x in (notes, c.unit_note) if x),
        })
    for cand in cands:
        verdict = verify_candidate(cand, outcome.evidence, now)
        cand["verification"], cand["verify_note"], cand["flags"] = verdict.level, verdict.note, list(verdict.flags)
        if cand["url"] and not is_safe_link(cand["url"]):
            cand["flags"].append("unsafe_link")
            cand["url"] = ""
    return cands


_CAND_COLUMNS = ("ref", "origin", "seller", "channel", "title", "url", "price", "shipping", "conditional_price",
                 "conditions", "original_currency", "original_amount", "is_overseas", "origin_country", "landed_price",
                 "tax_note", "match", "in_stock", "time_limited", "evidence", "verification", "verify_note", "flags",
                 "notes")


def save_outcome(conn: sqlite3.Connection, run_id: int, inp: ResearchInput, outcome: ResearchOutcome,
                 now: datetime | None = None) -> Ranking:
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    cands = candidates_for_ranking(inp, outcome, settings.usd_krw, now)
    ranking = rank_candidates(cands, settings.usd_krw, inp.category)
    ids: dict[int, int] = {}
    for cand in cands:
        row = {k: cand.get(k) for k in _CAND_COLUMNS}
        row["flags"] = ",".join(cand.get("flags") or [])
        row["is_overseas"] = int(bool(cand.get("is_overseas")))
        cur = conn.execute(
            f"INSERT INTO research_candidates (run_id, {', '.join(_CAND_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' for _ in _CAND_COLUMNS)})",
            (run_id, *row.values()),
        )
        ids[id(cand)] = int(cur.lastrowid)
    seen: set[tuple[str, str]] = set()
    sources: list[tuple[str, str, str]] = []
    ev = outcome.evidence
    for kind, items in (("fetch", [(d.url, d.title) for d in ev.fetched.values()]),
                        ("citation", [(c.url, c.title) for c in ev.citations]),
                        ("search", [(h.url, h.title) for h in ev.search_hits.values()])):
        for url, title in items:
            if is_safe_link(url) and (url, kind) not in seen and len(sources) < 80:
                seen.add((url, kind))
                sources.append((url, title, kind))
    conn.executemany("INSERT INTO research_sources (run_id, url, title, kind) VALUES (?,?,?,?)",
                     [(run_id, u, t, k) for u, t, k in sources])
    best = ranking.best
    diagnostics = {
        **ev.diagnostics(),
        "dropped": outcome.dropped,
        "notes": outcome.notes,
        "partial": outcome.partial,
        "baseline_count": len(inp.baseline),
        "hot_deal_count": len(inp.hot_deals),
        "profile": outcome.findings.profile.model_dump(),
        "baseline_median": ranking.baseline_median,
        "conditional_best_id": ids.get(id(ranking.conditional_best)) if ranking.conditional_best else None,
    }
    conn.execute(
        """UPDATE research_runs SET status='done', summary=?, report=?, diagnostics=?, best_price=?,
           best_candidate_id=?, finished_at=?, error=NULL WHERE id=?""",
        (outcome.findings.summary, ev.report_text[:20000], json.dumps(diagnostics, ensure_ascii=False),
         best["landed_price"] if best else None, ids.get(id(best)) if best else None, utcnow_iso(), run_id),
    )
    conn.commit()
    return ranking


async def alert_tracked_item(conn: sqlite3.Connection, run_id: int) -> list[str]:
    """AI 일별 최저가끼리 비교해 목표가·하락·역대 최저 알림 (시간별 카탈로그 가격과 섞지 않음)."""
    run = conn.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["item_id"] is None or run["best_price"] is None:
        return []
    item = conn.execute("SELECT * FROM tracked_items WHERE id=?", (run["item_id"],)).fetchone()
    if item is None:
        return []
    prev = [r[0] for r in conn.execute(
        """SELECT best_price FROM research_runs WHERE item_id=? AND status='done' AND best_price IS NOT NULL
           AND id < ? ORDER BY id""", (item["id"], run_id))]
    reasons = evaluate_alerts(prev[-1] if prev else None, min(prev) if prev else None, run["best_price"],
                              item["target_price"], item["drop_percent"], len(prev))
    best = conn.execute("SELECT * FROM research_candidates WHERE id=?", (run["best_candidate_id"],)).fetchone()
    for reason in reasons:
        await notify(conn, "AI최저가", item["name"], f"{reason} · {best['seller'] if best else ''}",
                     (best["url"] if best else None) or f"/research/{run_id}")
    return reasons


async def execute_run(run_id: int, runner: Runner | None = None) -> None:
    settings = get_settings()
    runner = runner or research
    conn = connect()
    try:
        run = conn.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["status"] != "queued":
            return
        conn.execute("UPDATE research_runs SET status='running', started_at=? WHERE id=?", (utcnow_iso(), run_id))
        conn.commit()
        try:
            async with asyncio.timeout(settings.ai_run_timeout_sec):
                inp = await build_input(conn, run)
                budget_left = max(0.0, settings.ai_daily_budget_usd - today_spent(conn, run["run_date"], run_id))

                async def on_response(usage: UsageTotals) -> None:
                    save_usage(conn, run_id, usage)

                outcome = await runner(inp, settings, budget_left_usd=budget_left, on_response=on_response)
            save_usage(conn, run_id, outcome.usage)
            save_outcome(conn, run_id, inp, outcome)
            await alert_tracked_item(conn, run_id)
        except asyncio.CancelledError:
            _finish_error(conn, run_id, "interrupted", "작업이 중단되었습니다 (서버 종료 등).", charge_reserve=True)
            raise
        except TimeoutError:
            _finish_error(conn, run_id, "error", f"시간 초과 ({settings.ai_run_timeout_sec}초)", charge_reserve=True)
        except ResearchError as exc:
            _finish_error(conn, run_id, "error", str(exc), charge_reserve=exc.uncertain_cost)
        except Exception as exc:  # noqa: BLE001
            log.exception("research run %s failed", run_id)
            _finish_error(conn, run_id, "error", f"내부 오류: {type(exc).__name__}: {exc}", charge_reserve=True)
    finally:
        conn.close()


def start_run_task(run_id: int, runner: Runner | None = None) -> asyncio.Task:
    task = asyncio.create_task(execute_run(run_id, runner), name=f"research-{run_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def shutdown_tasks() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def sweep_interrupted(conn: sqlite3.Connection) -> int:
    """서버 시작 시: 이전 프로세스에서 끝나지 못한 실행을 '중단됨'으로 정리 (예약금으로 비용 계상)."""
    cur = conn.execute(
        """UPDATE research_runs SET status='interrupted', error='서버가 다시 시작되어 중단됨', finished_at=?,
           cost_usd=MAX(cost_usd, reserved_usd) WHERE status IN ('queued','running')""",
        (utcnow_iso(),),
    )
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ 추적 상품 '매일 AI 조사'

async def daily_job(conn: sqlite3.Connection, now: datetime | None = None, runner: Runner | None = None) -> int:
    """1시간마다 호출. KST 기준 AI_DAILY_HOUR 이후, 오늘 시도하지 않은 상품만 순서대로 조사."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    if not settings.ai_configured or now.astimezone(KST).hour < settings.ai_daily_hour:
        return 0
    today = kst_today(now)
    items = conn.execute(
        """SELECT * FROM tracked_items WHERE ai_daily=1 AND active=1 AND id NOT IN (
             SELECT item_id FROM research_runs WHERE run_date=? AND item_id IS NOT NULL
             AND status IN ('done','queued','running','error')) ORDER BY id""",
        (today,),
    ).fetchall()
    ran = 0
    for item in items:
        res = create_run(conn, item["query"] or item["name"], item_id=item["id"], now=now)
        if res.error or res.run_id is None:
            log.info("daily AI research stopped: %s", res.error)
            break
        if res.reused:  # 같은 날 같은 질의를 이미 수동 조사했으면 그 결과를 이 상품에 연결
            conn.execute("UPDATE research_runs SET item_id=? WHERE id=? AND item_id IS NULL", (item["id"], res.run_id))
            conn.commit()
            await alert_tracked_item(conn, res.run_id)
            continue
        await execute_run(res.run_id, runner)
        ran += 1
    return ran


# ------------------------------------------------------------------ 화면용 조회

def candidate_dicts(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM research_candidates WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = [f for f in (r["flags"] or "").split(",") if f]
        out.append(d)
    return out


def run_view(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    run = conn.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        return None
    settings = get_settings()
    cands = candidate_dicts(conn, run_id)
    opts = json.loads(run["options"] or "{}")
    ranking = rank_candidates(cands, settings.usd_krw, opts.get("category", "general")) if cands else None
    diagnostics = json.loads(run["diagnostics"] or "{}")
    sources = conn.execute("SELECT * FROM research_sources WHERE run_id=? ORDER BY kind, id", (run_id,)).fetchall()
    history = conn.execute(
        """SELECT run_date, MIN(best_price) AS best_price, MAX(id) AS id FROM research_runs
           WHERE query_key=? AND status='done' GROUP BY run_date ORDER BY run_date DESC LIMIT 30""",
        (run["query_key"],),
    ).fetchall()
    return {"run": run, "options": opts, "ranking": ranking, "diagnostics": diagnostics, "sources": sources,
            "history": history, "candidate_count": len(cands)}

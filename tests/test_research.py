"""AI 조사 실행 관리 테스트 — 가짜 비교 결과·가짜 실행기로 네트워크/API 없이 검증."""

import asyncio
from datetime import datetime, timezone

import pytest

from app.db import connect, init_db
from app.services import research as R
from app.services.ai_research import (
    BaselineVerdict,
    FoundCandidate,
    Findings,
    Profile,
    ResearchError,
    ResearchOutcome,
)
from app.services.compare import CompareResult
from app.services.costs import UsageTotals
from app.services.evidence import build_evidence
from app.sources.shopping import Offer

COUPANG = "https://www.coupang.com/vp/products/7654321"
DANAWA = "https://prod.danawa.com/info/?pcode=77379380"
NOON_KST = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)   # KST 12:00
EARLY_KST = datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc)  # KST 06:00


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTDEAL_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("AI_ENABLED", "1")
    monkeypatch.setenv("AI_DAILY_BUDGET_USD", "2.0")
    monkeypatch.setenv("AI_RUN_RESERVE_USD", "1.0")

    async def fake_compare(query, category="general", client=None, use_cache=True):
        offer = Offer("danawa", "다나와", "WD SN850X 1TB", 340000, DANAWA, mall="다나와 최저가(15개 몰)")
        offer.extra["landed_price"] = offer.total
        return CompareResult(query=query, offers=[offer], providers=[])

    monkeypatch.setattr(R, "compare", fake_compare)
    conn = connect()
    init_db(conn)
    yield conn
    conn.close()


def outcome(price: int = 329000, cost: float = 0.4) -> ResearchOutcome:
    responses = [{"model": "claude-opus-5-5", "stop_reason": "end_turn", "content": [
        {"type": "server_tool_use", "id": "f1", "name": "web_fetch", "input": {"url": COUPANG}},
        {"type": "web_fetch_tool_result", "tool_use_id": "f1", "content": {
            "type": "web_fetch_result", "url": COUPANG, "retrieved_at": "2026-11-15T02:00:00Z",
            "content": {"type": "document", "title": "쿠팡", "source": {"type": "text", "data": f"판매가 {price:,}원"}}}},
        {"type": "text", "text": f"[C1]\n판매처: 쿠팡\nURL: {COUPANG}\n가격: {price:,}원\n요약: 쿠팡 최저"},
    ]}]
    cand = FoundCandidate(ref="C1", seller="쿠팡", channel="open_market", title="WD SN850X 1TB", url=COUPANG,
                          price_krw=price, original_currency="KRW", original_amount=-1, shipping_krw=0,
                          conditional_price_krw=-1, conditions="", origin_country="KR", tax_included="unknown",
                          unit_note="", in_stock="yes", time_limited="", match="exact", evidence=f"{price:,}원",
                          notes="")
    findings = Findings(profile=Profile(canonical_name="WD SN850X 1TB", model_codes=[], aliases=[], variant="1TB"),
                        candidates=[cand], baseline=[BaselineVerdict(ref="S1", match="exact", reason="같음")],
                        summary="쿠팡이 최저")
    usage = UsageTotals(input_tokens=1000, output_tokens=100, web_searches=3, cost_usd=cost, responses=2)
    return ResearchOutcome(findings, [cand], [], build_evidence(responses, [DANAWA]), usage, False, [])


def runner_returning(result):
    async def runner(inp, settings, *, budget_left_usd, on_response=None):
        assert inp.baseline[0]["ref"] == "S1" and budget_left_usd > 0
        if on_response:
            await on_response(result.usage)
        return result
    return runner


def test_create_run_budget_active_and_dedupe(env):
    first = R.create_run(env, "WD SN850X 1TB", now=NOON_KST)
    assert first.run_id and not first.reused and not first.error
    same = R.create_run(env, "wd  sn850x 1tb", now=NOON_KST)            # 진행 중인 같은 질의 → 재사용
    assert same.reused and same.run_id == first.run_id
    other = R.create_run(env, "에어팟 프로 2", now=NOON_KST)              # 다른 질의는 동시 실행 금지
    assert other.error and "진행 중" in other.error
    env.execute("UPDATE research_runs SET status='done', cost_usd=0.8 WHERE id=?", (first.run_id,))
    env.commit()
    again = R.create_run(env, "WD SN850X 1TB", now=NOON_KST)            # 같은 날 끝난 조사 → 재사용
    assert again.reused and again.run_id == first.run_id
    over = R.create_run(env, "WD SN850X 1TB", force=True, now=NOON_KST)  # 0.8 + 예약 1.0 > 2.0? 아니오
    assert over.run_id and not over.error
    env.execute("UPDATE research_runs SET status='done', cost_usd=0.5 WHERE id=?", (over.run_id,))
    env.commit()
    broke = R.create_run(env, "에어팟 프로 2", now=NOON_KST)               # 1.3 + 1.0 > 2.0 → 예산 초과
    assert broke.error and "예산" in broke.error and broke.run_id is None


def test_create_run_requires_credentials(env, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "0")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    assert "ANTHROPIC_API_KEY" in R.create_run(env, "x", now=NOON_KST).error


def test_execute_run_saves_candidates_best_sources_and_usage(env):
    run_id = R.create_run(env, "WD SN850X 1TB").run_id
    asyncio.run(R.execute_run(run_id, runner_returning(outcome())))
    run = env.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "done" and run["best_price"] == 329000 and run["cost_usd"] == pytest.approx(0.4)
    assert run["web_searches"] == 3 and run["summary"] == "쿠팡이 최저"
    cands = {c["ref"]: c for c in R.candidate_dicts(env, run_id)}
    assert cands["C1"]["verification"] == "page" and cands["S1"]["verification"] == "system"
    assert cands["S1"]["match"] == "exact" and cands["S1"]["shipping"] == -1
    view = R.run_view(env, run_id)
    assert view["ranking"].best["ref"] == "C1" and [h["best_price"] for h in view["history"]] == [329000]
    assert any(s["kind"] == "fetch" and s["url"] == COUPANG for s in view["sources"])


def test_execute_run_error_and_uncertain_cost(env):
    run_id = R.create_run(env, "x 1").run_id

    async def failing(inp, settings, *, budget_left_usd, on_response=None):
        raise ResearchError("Anthropic API 키가 없거나 잘못되었습니다")

    asyncio.run(R.execute_run(run_id, failing))
    run = env.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "error" and "API 키" in run["error"] and run["cost_usd"] == 0

    run2 = R.create_run(env, "x 2").run_id

    async def cut(inp, settings, *, budget_left_usd, on_response=None):
        raise ResearchError("연결 끊김", uncertain_cost=True)

    asyncio.run(R.execute_run(run2, cut))
    assert env.execute("SELECT cost_usd FROM research_runs WHERE id=?", (run2,)).fetchone()[0] == 1.0


def test_execute_run_timeout_and_cancel(env, monkeypatch):
    monkeypatch.setenv("AI_RUN_TIMEOUT_SEC", "1")

    async def slow(inp, settings, *, budget_left_usd, on_response=None):
        await asyncio.sleep(5)

    run_id = R.create_run(env, "slow 1").run_id
    asyncio.run(R.execute_run(run_id, slow))
    row = env.execute("SELECT status, error, cost_usd FROM research_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "error" and "시간 초과" in row["error"] and row["cost_usd"] == 1.0

    monkeypatch.setenv("AI_RUN_TIMEOUT_SEC", "60")
    run2 = R.create_run(env, "slow 2").run_id

    async def main():
        task = R.start_run_task(run2, slow)
        await asyncio.sleep(0.2)
        await R.shutdown_tasks()
        assert task.cancelled()

    asyncio.run(main())
    assert env.execute("SELECT status FROM research_runs WHERE id=?", (run2,)).fetchone()[0] == "interrupted"


def test_sweep_interrupted(env):
    run_id = R.create_run(env, "stuck").run_id
    env.execute("UPDATE research_runs SET status='running' WHERE id=?", (run_id,))
    env.commit()
    assert R.sweep_interrupted(env) == 1
    row = env.execute("SELECT status, cost_usd FROM research_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "interrupted" and row["cost_usd"] == 1.0


def _item(env, **kw):
    cols = {"name": "SN850X", "kind": "query", "query": "WD SN850X 1TB", "created_at": "2026-11-01", "ai_daily": 1}
    cols.update(kw)
    cur = env.execute(f"INSERT INTO tracked_items ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                      tuple(cols.values()))
    env.commit()
    return cur.lastrowid


def test_ai_series_alerts_fire_once(env, monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    item_id = _item(env, target_price=330000, drop_percent=5)
    for price in (340000, 320000, 320000):
        res = R.create_run(env, "WD SN850X 1TB", item_id=item_id, force=True)
        asyncio.run(R.execute_run(res.run_id, runner_returning(outcome(price, cost=0.1))))
    kinds = [r["body"] for r in env.execute("SELECT body FROM notifications WHERE kind='AI최저가' ORDER BY id")]
    # 둘째 날: 목표가 도달 + 하락 (역대 최저 알림은 이전 기록 2개 이상부터), 셋째 날(같은 가격): 반복 없음
    assert len(kinds) == 2
    assert "목표가" in kinds[0] and "하락" in kinds[1]


def test_daily_job_runs_due_items_only(env, monkeypatch):
    calls = []

    async def runner(inp, settings, *, budget_left_usd, on_response=None):
        calls.append(inp.query)
        return outcome()

    due = _item(env)
    _item(env, query="에어팟", name="에어팟", ai_daily=0)
    assert asyncio.run(R.daily_job(env, now=EARLY_KST, runner=runner)) == 0  # KST 9시 전
    assert asyncio.run(R.daily_job(env, now=NOON_KST, runner=runner)) == 1
    assert calls == ["WD SN850X 1TB"]
    assert asyncio.run(R.daily_job(env, now=NOON_KST, runner=runner)) == 0  # 오늘 이미 조사함
    linked = env.execute("SELECT COUNT(*) FROM research_runs WHERE item_id=?", (due,)).fetchone()[0]
    assert linked == 1

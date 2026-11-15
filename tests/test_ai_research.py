"""AI 조사 2단계 호출 테스트 — 가짜 send/parse로 실제 API 호출 없이 검증."""

import asyncio
import dataclasses

import pytest

from app.config import get_settings
from app.services.ai_research import (
    FINAL_TURN_TEXT,
    BaselineVerdict,
    FoundCandidate,
    Findings,
    ParseResult,
    Profile,
    RefusalError,
    ResearchError,
    ResearchInput,
    SentResponse,
    build_phase_a_params,
    build_user_message,
    cross_check,
    research,
    run_phase_a,
)
from app.services.costs import UsageTotals

COUPANG = "https://www.coupang.com/vp/products/7654321"
REPORT = (
    "[C1]\n판매처: 쿠팡\n채널: 오픈마켓\n상품명: WD SN850X 1TB\nURL: " + COUPANG + "\n가격: 329,000원\n"
    "배송비: 무료\n일치도: 정확히 일치\n근거: \"판매가 329,000원\"\n"
    "[C2]\n판매처: 수상한몰\nURL: https://cheap.example.xyz/p/1\n가격: 199,000원\n"
    "[S1] 일치도: 정확히 일치 — 같은 모델\n요약: 쿠팡이 가장 쌉니다."
)


def inp() -> ResearchInput:
    return ResearchInput(
        query="WD SN850X 1TB", today="2026-11-15",
        baseline=[{"ref": "S1", "provider_name": "다나와", "mall": "다나와 최저가", "title": "WD SN850X",
                   "price": 340140, "url": "https://prod.danawa.com/info/?pcode=1"}],
        hot_deals=[{"ref": "D1", "source": "뽐뿌", "title": "[쿠팡] SN850X 1TB (329,000원)",
                    "url": "https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no=1", "posted": "2026-11-14"}],
    )


def resp(stop: str, content: list, searches: int = 0, container: str | None = None, **extra) -> SentResponse:
    data = {"model": "claude-opus-5-5", "stop_reason": stop, "content": content,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "server_tool_use": {"web_search_requests": searches}}}
    if container:
        data["container"] = {"id": container}
    data.update(extra)
    return SentResponse(data, content)


class FakeSend:
    def __init__(self, *responses: SentResponse):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, params: dict) -> SentResponse:
        self.calls.append(params)
        return self.responses.pop(0)


def settings(**overrides):
    return dataclasses.replace(get_settings(), **overrides)


PENDING = [{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}}]
DONE = PENDING + [{"type": "web_search_tool_result", "tool_use_id": "s1", "content": []}]
FINAL = [{"type": "text", "text": REPORT}]


def test_request_params_are_fixed_and_safe():
    params = build_phase_a_params(settings(), inp())
    tools = {t["name"]: t for t in params["tools"]}
    assert tools["web_search"]["type"] == "web_search_20260318" and tools["web_search"]["max_uses"] == 8
    assert tools["web_search"]["user_location"]["country"] == "KR"
    assert tools["web_fetch"]["use_cache"] is False and tools["web_fetch"]["response_inclusion"] == "full"
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["model"] == "claude-opus-5-5" and params["output_config"] == {"effort": "high"}
    assert "tool_choice" not in params
    text = build_user_message(inp())
    assert "[S1]" in text and "prod.danawa.com" in text and "[D1]" in text and "2026-11-15" in text


def test_pause_turn_continues_with_container_and_same_prefix():
    fake = FakeSend(resp("pause_turn", PENDING, searches=2, container="cont_1"), resp("end_turn", FINAL, searches=1))
    usage = UsageTotals()
    result = asyncio.run(run_phase_a(fake, settings(), inp(), usage, budget_left_usd=5))
    assert result.stop_reason == "end_turn" and not result.partial
    first, second = fake.calls
    assert second["container"] == "cont_1"
    assert second["messages"][0] == first["messages"][0]                 # 첫 사용자 메시지 그대로
    assert second["messages"][1] == {"role": "assistant", "content": PENDING}
    assert len(second["messages"]) == 2 and "tool_choice" not in second  # "계속" 같은 메시지를 붙이지 않음
    assert usage.web_searches == 3 and usage.responses == 2


def test_fallback_turn_is_not_continued():
    content = PENDING + [{"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}}]
    fake = FakeSend(resp("pause_turn", content))
    result = asyncio.run(run_phase_a(fake, settings(), inp(), UsageTotals(), budget_left_usd=5))
    assert result.partial and len(fake.calls) == 1


def test_refusal_raises():
    fake = FakeSend(resp("refusal", [], stop_details={"type": "refusal", "category": "cyber"}))
    with pytest.raises(RefusalError, match="cyber"):
        asyncio.run(run_phase_a(fake, settings(), inp(), UsageTotals(), budget_left_usd=5))


def test_cap_reached_sends_one_final_report_turn():
    fake = FakeSend(resp("pause_turn", DONE), resp("end_turn", FINAL))
    result = asyncio.run(run_phase_a(fake, settings(ai_max_continuations=0), inp(), UsageTotals(), budget_left_usd=5))
    final_call = fake.calls[1]
    assert final_call["tool_choice"] == {"type": "none"}
    assert final_call["messages"][-1] == {"role": "user", "content": FINAL_TURN_TEXT}
    assert result.stop_reason == "end_turn" and "보고서를 작성하게 함" in result.notes[0]


def test_cap_reached_with_pending_tool_stops_partial():
    fake = FakeSend(resp("pause_turn", PENDING))
    result = asyncio.run(run_phase_a(fake, settings(ai_max_continuations=0), inp(), UsageTotals(), budget_left_usd=5))
    assert result.partial and len(fake.calls) == 1


def test_budget_cap_stops_continuation():
    fake = FakeSend(resp("pause_turn", DONE), resp("end_turn", FINAL))
    result = asyncio.run(run_phase_a(fake, settings(), inp(), UsageTotals(), budget_left_usd=0.0001))
    assert "비용 상한" in result.notes[0] and fake.calls[1]["tool_choice"] == {"type": "none"}


def candidate(**kw) -> FoundCandidate:
    base = dict(ref="C1", seller="쿠팡", channel="open_market", title="WD SN850X 1TB", url=COUPANG, price_krw=329000,
                original_currency="KRW", original_amount=-1, shipping_krw=0, conditional_price_krw=-1, conditions="",
                origin_country="KR", tax_included="unknown", unit_note="", in_stock="yes", time_limited="",
                match="exact", evidence="판매가 329,000원", notes="")
    base.update(kw)
    return FoundCandidate(**base)


def findings(*cands: FoundCandidate) -> Findings:
    return Findings(profile=Profile(canonical_name="WD BLACK SN850X 1TB", model_codes=["WDS100T2X0E"],
                                    aliases=["SN850X"], variant="1TB"),
                    candidates=list(cands),
                    baseline=[BaselineVerdict(ref="S1", match="exact", reason="같은 모델")], summary="쿠팡 최저")


def test_cross_check_drops_rows_that_disagree_with_report():
    kept, dropped = cross_check(findings(
        candidate(),
        candidate(ref="C2", seller="수상한몰", url="https://cheap.example.xyz/p/1", price_krw=99000),  # 가격 불일치
        candidate(ref="C3", url="https://www.coupang.com/vp/products/1"),                             # 보고서에 없는 ref
    ), REPORT)
    assert [c.ref for c in kept] == ["C1", "C3"]
    assert dropped[0][0] == "C2" and "가격" in dropped[0][1]
    kept, dropped = cross_check(findings(candidate(url="https://www.coupang.com/vp/products/999")), REPORT)
    assert not kept and "URL" in dropped[0][1]


def test_research_end_to_end_with_fakes():
    fake = FakeSend(resp("end_turn", FINAL, searches=3))
    seen_costs = []

    async def fake_parse(params):
        assert params["output_format"] is Findings and "<report>" in params["messages"][0]["content"]
        return ParseResult(findings(candidate()), {"model": "claude-opus-5-5",
                                                   "usage": {"input_tokens": 500, "output_tokens": 200}})

    async def on_response(usage):
        seen_costs.append(round(usage.cost_usd, 6))

    outcome = asyncio.run(research(inp(), settings(), budget_left_usd=5, send=fake, parse=fake_parse,
                                   on_response=on_response))
    assert [c.ref for c in outcome.candidates] == ["C1"] and outcome.findings.baseline[0].match == "exact"
    assert outcome.usage.web_searches == 3 and len(seen_costs) == 2 and seen_costs[1] > seen_costs[0]
    assert "prod.danawa.com" in str(outcome.evidence.seen_keys) or "danawa:1" in outcome.evidence.seen_keys


def test_research_without_report_text_fails_cleanly():
    fake = FakeSend(resp("end_turn", [{"type": "text", "text": "   "}]))

    async def never(params):  # pragma: no cover
        raise AssertionError("2단계를 부르면 안 됨")

    with pytest.raises(ResearchError, match="보고서"):
        asyncio.run(research(inp(), settings(), budget_left_usd=5, send=fake, parse=never))

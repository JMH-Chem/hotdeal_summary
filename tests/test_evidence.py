"""AI 조사 응답(문서에 나온 JSON 형태) → 증거·검증·순위·비용 테스트. 실제 API 호출 없음."""

from datetime import datetime, timezone

from app.services.costs import UsageTotals, token_cost
from app.services.evidence import (
    build_evidence,
    compute_landed,
    extract_amounts,
    is_known_shop,
    is_safe_link,
    normalize_url,
    page_age_days,
    parse_report_lines,
    price_in_text,
    rank_candidates,
    report_line_price,
    report_line_url,
    verify_candidate,
)

NOW = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
COUPANG = "https://www.coupang.com/vp/products/7654321?itemId=1&vendorItemId=2&sourceType=srp"
GMARKET = "https://item.gmarket.co.kr/Item?goodscode=3456789012&utm_source=x"
DANAWA_SEARCH = "https://search.danawa.com/dsearch.php?query=sn850x"


def research_response() -> dict:
    """검색 → 페치 → 인용이 달린 보고서. 중첩(caller) 페치 결과도 포함."""
    return {
        "model": "claude-opus-5-5",
        "stop_reason": "end_turn",
        "content": [
            {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "WD SN850X 1TB 최저가"}},
            {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [
                {"type": "web_search_result", "url": GMARKET, "title": "G마켓 SN850X", "encrypted_content": "x",
                 "page_age": "1 day ago"},
                {"type": "web_search_result", "url": "https://old.example-shop.kr/p/1", "title": "옛 글",
                 "encrypted_content": "y", "page_age": "October 1, 2026"},
            ]},
            {"type": "server_tool_use", "id": "srv_2", "name": "web_fetch", "input": {"url": COUPANG}},
            {"type": "web_fetch_tool_result", "tool_use_id": "srv_2", "content": {
                "type": "web_fetch_result", "url": COUPANG, "retrieved_at": "2026-11-15T02:59:00Z",
                "content": {"type": "document", "title": "쿠팡 SN850X",
                            "source": {"type": "text", "media_type": "text/plain",
                                       "data": "WD BLACK SN850X 1TB 판매가 329,000원 무료배송"}}}},
            {"type": "server_tool_use", "id": "srv_3", "name": "web_fetch", "input": {"url": "https://shop.blocked.kr/item/9"}},
            {"type": "web_fetch_tool_result", "tool_use_id": "srv_3",
             "content": {"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"}},
            {"type": "bash_code_execution_tool_result", "content": {"nested": [
                {"type": "web_fetch_tool_result", "tool_use_id": "srv_9", "caller": {"type": "code_execution_20260120"},
                 "content": {"type": "web_fetch_result", "url": DANAWA_SEARCH, "retrieved_at": "2026-11-15T03:00:00Z",
                             "content": {"type": "document", "title": "다나와 검색",
                                         "source": {"type": "text", "data": "SN850X 1TB 최저가 318,500원 외 다수"}}}},
            ]}},
            {"type": "text", "text": "[C1]\n판매처: 쿠팡\nURL: " + COUPANG + "\n가격: 329,000원\n"},
            {"type": "text", "text": "[C2]\n판매처: G마켓\nURL: " + GMARKET + "\n"},
            {"type": "text", "text": "가격: 312,000원", "citations": [
                {"type": "web_search_result_location", "url": GMARKET, "title": "G마켓 SN850X",
                 "encrypted_index": "e", "cited_text": "WD SN850X 1TB 312,000원 쿠폰적용"}]},
            {"type": "text", "text": "\n[C3]\n판매처: 옛가게\nURL: https://old.example-shop.kr/p/1\n"},
            {"type": "text", "text": "가격: 290,000원", "citations": [
                {"type": "web_search_result_location", "url": "https://old.example-shop.kr/p/1", "title": "옛 글",
                 "encrypted_index": "e2", "cited_text": "SN850X 1TB 29만원 특가"}]},
        ],
        "usage": {"input_tokens": 20000, "output_tokens": 3000, "cache_read_input_tokens": 10000,
                  "cache_creation_input_tokens": 0, "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 2}},
    }


def test_extract_amounts_formats():
    krw, usd = extract_amounts("정가 1,234,000원 → 12.9만원, 123만 4천원, ₩98,000 / 990원 / $129.99 / 15달러")
    assert {1234000, 129000, 1234000, 98000, 990} <= krw
    assert {129.99, 15.0} <= usd
    krw, _ = extract_amounts("모델 990 PRO, $1,299.99")
    assert 990 not in krw and 1299 not in krw  # 단위 없는 3자리, 달러 소수점 앞자리는 원화로 보지 않음
    assert price_in_text("판매가 329,000원", 329000)
    assert price_in_text("price $129.99", 182000, 129.99, "USD")
    assert not price_in_text("판매가 329,000원", 329)


def test_normalize_url_retailer_keys():
    assert normalize_url(COUPANG) == "coupang:7654321"
    assert normalize_url("https://m.coupang.com/vm/products/7654321?x=1") == "coupang:7654321"
    assert normalize_url(GMARKET) == "gmarket:3456789012"
    assert normalize_url("https://ko.aliexpress.com/item/1005006123456789.html?spm=a") == "ali:1005006123456789"
    assert normalize_url("https://smartstore.naver.com/shop/products/555?NaPm=ct") == "naverstore:555"
    assert normalize_url("https://prod.danawa.com/info/?pcode=77379380&keyword=x") == "danawa:77379380"
    assert normalize_url("https://www.example.com/a/?b=2&utm_source=x&a=1#frag") == "example.com/a?a=1&b=2"


def test_safe_link_and_known_shop():
    assert is_safe_link("https://coupang.com/x") and not is_safe_link("javascript:alert(1)")
    assert not is_safe_link("data:text/html,hi") and not is_safe_link(None)
    assert is_known_shop(COUPANG) and is_known_shop("https://brand.naver.com/x")
    assert not is_known_shop("https://super-cheap-mall.xyz/p/1")


def test_build_evidence_collects_sources_and_maps_citations():
    ev = build_evidence([research_response()], seed_urls=["https://prod.danawa.com/info/?pcode=1"])
    assert ev.queries == ["WD SN850X 1TB 최저가"]
    assert "coupang:7654321" in ev.fetched and "329,000원" in ev.fetched["coupang:7654321"].text
    assert normalize_url(DANAWA_SEARCH) in ev.fetched  # 코드 실행 안에 중첩된 페치 결과
    assert ev.fetch_errors == [("https://shop.blocked.kr/item/9", "url_not_accessible")]
    assert [c.ref for c in ev.citations] == ["C2", "C3"]
    assert "danawa:1" in ev.seen_keys and "gmarket:3456789012" in ev.seen_keys
    assert "[C1]" in ev.report_text and ev.diagnostics()["fetched_pages"] == 2


def test_verify_levels():
    ev = build_evidence([research_response()])
    page = verify_candidate({"ref": "C1", "url": COUPANG, "price": 329000}, ev, NOW)
    assert page.level == "page"
    cited = verify_candidate({"ref": "C2", "url": GMARKET, "price": 312000}, ev, NOW)
    assert cited.level == "citation"
    stale = verify_candidate({"ref": "C3", "url": "https://old.example-shop.kr/p/1", "price": 290000}, ev, NOW)
    assert stale.level == "stale" and "unknown_shop" in stale.flags
    wrong = verify_candidate({"ref": "C1", "url": COUPANG, "price": 300000}, ev, NOW)
    assert wrong.level == "unverified"
    invented = verify_candidate({"ref": "C9", "url": "https://www.coupang.com/vp/products/1111", "price": 1000}, ev, NOW)
    assert invented.level == "url_unseen"
    listing = verify_candidate({"ref": "C4", "url": DANAWA_SEARCH, "price": 318500}, ev, NOW)
    assert listing.level == "citation" and "list_page" in listing.flags
    assert verify_candidate({"origin": "system", "url": COUPANG, "price": 1}, ev, NOW).level == "system"


def test_page_age_days():
    assert page_age_days("2 days ago", NOW) == 2
    assert 44 < page_age_days("October 1, 2026", NOW) < 46
    assert page_age_days("2026-11-14", NOW) < 2
    assert page_age_days("어제", NOW) is None


def test_report_lines_parse():
    fields = parse_report_lines("소개\n[C1]\n판매처: 쿠팡\nURL: " + COUPANG + "\n가격: 329,000원 (무료배송)\n[S2] 일치도: 다름")
    assert report_line_price(fields["C1"]) == 329000
    assert report_line_url(fields["C1"]) == COUPANG
    assert fields["S2"]["일치도"] == "다름"


def _cand(**kw):
    base = {"origin": "ai", "match": "exact", "in_stock": "yes", "shipping": 0, "is_overseas": False,
            "origin_country": "KR", "tax_included": "unknown", "conditional_price": -1, "flags": []}
    base.update(kw)
    return base


def test_rank_candidates_confirmed_vs_warnings():
    cands = [
        _cand(ref="S1", origin="system", price=340000, verification="system"),
        _cand(ref="S2", origin="system", price=350000, verification="system"),
        _cand(ref="C1", price=329000, verification="page", conditional_price=310000),
        _cand(ref="C2", price=312000, verification="unverified"),             # 더 싸지만 미확인 → 경고
        _cand(ref="C3", price=120000, verification="page"),                   # 중앙값 절반 미만 → 의심
        _cand(ref="C4", price=300000, verification="citation", match="different"),
        _cand(ref="C5", price=200000, verification="citation", is_overseas=True, origin_country="CN"),
    ]
    r = rank_candidates(cands, usd_krw=1400)
    assert r.best["ref"] == "C5"  # 해외: $142.9 ≤ $150 면세 → 200,000원
    assert [c["ref"] for c in r.confirmed] == ["C5", "C1", "S1", "S2"]
    assert {c["ref"] for c in r.cheaper_unconfirmed} == {"C3"}
    assert "suspicious_low" in next(c for c in cands if c["ref"] == "C3")["flags"]
    assert r.conditional_best["ref"] == "C1"
    assert next(c for c in cands if c["ref"] == "C4") in r.others


def test_rank_unverified_cheaper_listed_when_no_confirmed_best():
    cands = [_cand(ref="C1", price=10000, verification="unverified"),
             _cand(ref="C2", price=12000, verification="url_unseen", match="close")]
    r = rank_candidates(cands, usd_krw=1400)
    assert r.best is None and [c["ref"] for c in r.cheaper_unconfirmed] == ["C1", "C2"]


def test_compute_landed_overseas_tax_and_unknown_shipping():
    landed, note = compute_landed(_cand(price=300000, shipping=-1, is_overseas=True, origin_country="CN"), 1400)
    assert landed == 300000 + 24000 + 32400 and "배송비 미확인" in note and "관부가세" in note
    us, _ = compute_landed(_cand(price=266000, is_overseas=True, origin_country="US"), 1400)
    assert us == 266000  # 미국발 $190 ≤ $200 면세
    incl, _ = compute_landed(_cand(price=300000, is_overseas=True, origin_country="CN", tax_included="yes"), 1400)
    assert incl == 300000


def test_costs_tokens_search_and_fallback_iterations():
    totals = UsageTotals()
    cost = totals.add(research_response())
    expected = (20000 * 4 + 3000 * 20 + 10000 * 0.2) / 1e6 + 0.01
    assert abs(cost - expected) < 1e-9 and totals.web_searches == 1 and totals.web_fetches == 2
    fallback = {"model": "claude-opus-4-8", "usage": {
        "input_tokens": 999, "output_tokens": 999, "server_tool_use": {"web_search_requests": 2},
        "iterations": [
            {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 500},
        ]}}
    c2 = totals.add(fallback)
    assert abs(c2 - ((1000 * 4) / 1e6 + (1000 * 5 + 500 * 25) / 1e6 + 0.02)) < 1e-9
    assert totals.input_tokens == 22000 and totals.responses == 2
    assert token_cost("unknown-model", {"input_tokens": 1_000_000}) == 4.0

from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from app.config import get_settings
from app.parsing import KST
from app.services.compare import rank_offers, relevance
from app.services.coupons import extract_coupons
from app.services.customs import estimate_import_tax
from app.services.events import load_events
from app.services.tracker import evaluate_alerts, keyword_matches
from app.sources.shopping import Offer, ali_build_params, ali_sign, parse_ali_products, parse_naver

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 9, 5, 0, tzinfo=KST)


def test_extract_coupons_from_official_1111_post():
    html = (FIX / "clien_ali_1111.html").read_text(encoding="utf-8")
    text = BeautifulSoup(html, "lxml").select_one("div.post_article").get_text("\n", strip=True)
    found = {c.code: c for c in extract_coupons(text, title="[알리익스프레스] 11.11 광군제 정보 안내", now=NOW)}
    assert set(found) == {"KBTY04", "KBTY08", "KBTY13", "KBTY19", "KBTY28", "KBTY45"}
    c = found["KBTY19"]
    assert (c.platform, c.discount_amount, c.min_spend, c.currency) == ("AliExpress", 19, 148, "USD")
    assert c.valid_until == "2026-11-18" and c.event_slug == "aliexpress-1111-2026"


def test_extract_coupons_ignores_model_numbers():
    text = "NIPOGI AMD 라이젠7 7735HS 미니PC 판매가 : $497.27 할인코드 :$95 ( UPKBD19 )"
    codes = [c.code for c in extract_coupons(text, title="[특가] NiPoGi AMD 라이젠7 미니 PC", now=NOW)]
    assert codes == ["UPKBD19"]
    assert extract_coupons("[G마켓] AULA 독거미 F87S 기계식 키보드 쿠폰 적용가",
                           title="[G마켓] AULA 독거미 F87S 기계식 키보드", now=NOW) == []


def test_extract_coupons_code_list_and_amount():
    text = "블렌더 기본가 46,013원 코인할인 3,619원 할인코드 6,000원 &lt;GONS04 , KBTY04 , LJMW04 중 택1&gt;"
    found = extract_coupons(text, default_platform="AliExpress", now=NOW)
    assert [c.code for c in found] == ["GONS04", "KBTY04", "LJMW04"]
    assert all(c.discount_amount == 6000 and c.currency == "KRW" for c in found)


def test_customs_threshold_and_min_collect():
    free = estimate_import_tax(150 * 1400, usd_krw=1400)
    assert not free.taxable and free.total_tax == 0
    taxed = estimate_import_tax(300_000, 0, usd_krw=1400)
    assert taxed.taxable and taxed.duty == 24_000 and taxed.vat == 32_400 and taxed.landed_price == 356_400
    us = estimate_import_tax(190 * 1400, usd_krw=1400, origin="US")
    assert not us.taxable
    elec = estimate_import_tax(220_000, 0, usd_krw=1400, category="electronics")
    assert elec.duty == 0 and elec.vat == 22_000


def test_evaluate_alerts():
    assert evaluate_alerts(None, None, 9000, 10000, None, 0) == ["목표가 도달: 9,000원 (목표 10,000원)"]
    assert evaluate_alerts(9500, 9500, 9000, 10000, None, 3)[0].startswith("역대 최저가")  # 이미 목표가 아래 → 재알림 X
    drop = evaluate_alerts(10000, 10000, 8500, None, 10, 1)
    assert drop and "15.0% 하락" in drop[0]
    assert evaluate_alerts(10000, 9000, 9500, None, 10, 5) == []


def test_keyword_matches():
    assert keyword_matches("햇반 72", "[CJ더마켓] 햇반 210g x 72개")
    assert not keyword_matches("햇반 36", "[CJ더마켓] 햇반 210g x 72개")


def test_ali_sign_is_deterministic():
    params = {"app_key": "123", "method": "aliexpress.affiliate.product.query", "sign_method": "sha256",
              "timestamp": "1700000000000", "keywords": "ssd"}
    assert ali_sign(params, "secret") == ali_sign(dict(reversed(list(params.items()))), "secret")
    assert len(ali_sign(params, "secret")) == 64


def test_ali_build_params_and_parse(monkeypatch):
    monkeypatch.setenv("ALI_APP_KEY", "k")
    monkeypatch.setenv("ALI_APP_SECRET", "s")
    params = ali_build_params(get_settings(), "aliexpress.affiliate.product.query", {"keywords": "ssd"}, 1)
    assert params["sign"] == ali_sign({k: v for k, v in params.items() if k != "sign"}, "s")
    payload = {"aliexpress_affiliate_product_query_response": {"resp_result": {"result": {"products": {"product": [
        {"product_id": 1, "product_title": "SSD 1TB", "target_sale_price": "65000", "target_sale_price_currency": "KRW",
         "promotion_link": "https://s.click.aliexpress.com/x"}]}}}}}
    offers = parse_ali_products(payload, 1400)
    assert offers[0].price == 65000 and offers[0].is_overseas


def test_naver_parse_and_rank(monkeypatch):
    offers = parse_naver({"items": [
        {"title": "<b>삼성</b> SSD 990 PRO 1TB", "lprice": "150000", "mallName": "쿠팡", "link": "https://a"},
        {"title": "삼성 SSD 990 PRO 1TB 방열판", "lprice": "170000", "mallName": "11번가", "link": "https://b"},
        {"title": "전혀 다른 상품", "lprice": "1000", "mallName": "x", "link": "https://c"},
    ]})
    offers.append(Offer("aliexpress", "알리익스프레스", "Samsung SSD 990 PRO 1TB", 140000, "https://d", is_overseas=True))
    ranked = rank_offers("삼성 990 pro 1tb", offers, get_settings())
    assert [o.url for o in ranked][0] in ("https://d", "https://a")
    assert all(relevance("삼성 990 pro 1tb", o.title) >= 0.5 for o in ranked)
    assert "https://c" not in [o.url for o in ranked]


def test_events_file_loads():
    events = load_events()
    gw = next(e for e in events if e.slug == "aliexpress-1111-2026")
    assert gw.status(NOW) == "ongoing"
    assert gw.end.date().isoformat() == "2026-11-18"


def test_save_coupons_prefers_complete_info():
    from app.db import connect, init_db
    from app.services.coupons import CouponCandidate, save_coupons

    conn = connect(":memory:")
    init_db(conn)
    partial = CouponCandidate("AliExpress", "KBTY04", 6000, None, None, "KRW", None, "뽐뿌", "ctx1", None)
    full = CouponCandidate("AliExpress", "KBTY04", 4, None, 27, "USD", "2026-11-18", "클리앙", "ctx2", "aliexpress-1111-2026")
    save_coupons(conn, [partial], "https://a")
    save_coupons(conn, [full], "https://b")
    row = conn.execute("SELECT * FROM coupons").fetchone()
    assert (row["discount_amount"], row["min_spend"], row["currency"], row["source_url"]) == (4, 27, "USD", "https://b")
    assert row["event_slug"] == "aliexpress-1111-2026"


def test_aliexpress_product_web_finds_same_id(monkeypatch):
    import asyncio

    import httpx

    from app.sources.shopping import aliexpress_product_web

    search_html = (FIX / "aliexpress_search.html").read_text(encoding="utf-8")
    pid = "1005013363065046"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/item/" in request.url.path:
            return httpx.Response(200, text=f'<meta property="og:title" content="990 PRO M.2 2280 NVMe SSD - AliExpress" />')
        return httpx.Response(200, text=search_html)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await aliexpress_product_web(client, pid)

    offer = asyncio.run(run())
    assert offer is not None and offer.product_ref == pid and offer.price > 0


def test_polite_wait_spaces_same_host(monkeypatch):
    import asyncio
    import time

    import httpx

    from app import http as apphttp

    monkeypatch.setitem(apphttp.CRAWL_DELAYS, "example.test", 0.3)

    async def run():
        start = time.monotonic()
        for _ in range(3):
            await apphttp.polite_wait(httpx.Request("GET", "https://example.test/x"))
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.55


def test_extract_coupons_conditions_after_code_and_youtuber():
    yt = extract_coupons("유튜버 잇섭 전용 할인코드 ITSUB11 : $50 이상 구매시 $6 할인 (~11/18)", now=NOW)
    assert [(c.code, c.discount_amount, c.min_spend, c.currency, c.valid_until, c.source_name) for c in yt] == [
        ("ITSUB11", 6, 50, "USD", "2026-11-18", "잇섭")]
    multi = extract_coupons("광군제 쿠폰 정리: 코드 KR1111 (15,000/100,000), 코드 KR2222 (30,000/200,000)", now=NOW)
    assert [(c.code, c.discount_amount, c.min_spend) for c in multi] == [
        ("KR1111", 15000, 100000), ("KR2222", 30000, 200000)]
    km = extract_coupons("[알리] 테크몽 구독자 코드 TECHMONG8 입력하면 ₩60,000 이상 ₩8,000 할인", now=NOW)
    assert (km[0].platform, km[0].discount_amount, km[0].min_spend, km[0].source_name) == ("AliExpress", 8000, 60000, "테크몽")


def test_notify_sends_to_telegram_and_discord(monkeypatch):
    import asyncio
    import json

    import httpx

    from app.db import connect, init_db
    from app.services.notify import notify

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.host, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    conn = connect(":memory:")
    init_db(conn)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await notify(conn, "가격알림", "RTX 5070", "목표가 도달", "https://x", client)

    asyncio.run(run())
    assert sent[0][0] == "api.telegram.org" and sent[0][1] == "/bot123:abc/sendMessage"
    assert sent[0][2]["chat_id"] == "42" and "RTX 5070" in sent[0][2]["text"]
    assert sent[1][0] == "discord.com" and "목표가 도달" in sent[1][2]["content"]
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_record_price_triggers_alert(monkeypatch):
    import asyncio

    from app.db import connect, init_db
    from app.services.tracker import PriceReading, add_item, record_price

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    conn = connect(":memory:")
    init_db(conn)
    item_id = add_item(conn, name="햇반", kind="query", query="햇반", target_price=60000, drop_percent=10)

    def row():
        return conn.execute("SELECT * FROM tracked_items WHERE id=?", (item_id,)).fetchone()

    assert asyncio.run(record_price(conn, row(), PriceReading(70000, "쿠팡", "naver", "https://a"))) == []
    reasons = asyncio.run(record_price(conn, row(), PriceReading(59000, "G마켓", "naver", "https://b")))
    assert any("목표가" in r for r in reasons) and any("하락" in r for r in reasons)
    it = row()
    assert (it["last_price"], it["lowest_price"], it["highest_price"]) == (59000, 59000, 70000)
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == len(reasons)

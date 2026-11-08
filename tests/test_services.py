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

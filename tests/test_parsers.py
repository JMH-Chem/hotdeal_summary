from pathlib import Path

from app.parsing import dedupe_key, is_aliexpress, parse_title
from app.sources.community import SOURCE_MAP, parse_clien, parse_gnuboard, parse_rss
from app.sources.shopping import aliexpress_product_id, parse_danawa

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_title_krw_with_shipping():
    p = parse_title("[CJ더마켓] 햇반 210g x 72개 (67,684원/무배)")
    assert (p.mall, p.price, p.currency, p.shipping) == ("CJ더마켓", 67684, "KRW", "무료배송")


def test_parse_title_number_without_unit_in_paren():
    p = parse_title("[오늘의집] 헤드앤숄더 800ml 3개 + 미니샴푸 2개 증정(23,218 / 무료배송)")
    assert p.price == 23218 and p.shipping == "무료배송"


def test_parse_title_usd_and_man_won():
    assert parse_title("[특가] NiPoGi 미니 PC ($372)").currency == "USD"
    assert parse_title("[특가] NiPoGi 미니 PC ($372)").price == 372
    assert parse_title("[일마존] Audio Technica ATH-M50xGM /16만원").price == 160000


def test_aliexpress_detection_and_dedupe():
    assert is_aliexpress("[알리익스프레스] 광군제 코드")
    assert dedupe_key("[G마켓] 상품 A (1,000원)", "G마켓", 1000) == dedupe_key("[G마켓]  상품 A  (1,000원/무료)", "G마켓", 1000)


def test_ppomppu_rss():
    deals = parse_rss(SOURCE_MAP["ppomppu"], read("ppomppu.xml"))
    assert len(deals) >= 10
    d = deals[0]
    assert d.url.startswith("https://www.ppomppu.co.kr/zboard/view.php")
    assert d.external_id.isdigit() and d.posted_at is not None and d.views is not None


def test_ruliweb_and_eomisae_rss():
    ruli = parse_rss(SOURCE_MAP["ruliweb"], read("ruliweb.xml"))
    assert ruli and ruli[0].category
    eomi = parse_rss(SOURCE_MAP["eomisae"], read("eomisae.xml"))
    assert eomi and eomi[0].url.startswith("https://eomisae.co.kr/")


def test_clien_list():
    deals = parse_clien(SOURCE_MAP["clien"], read("clien.html"))
    assert len(deals) >= 20
    assert all("/service/board/jirum/" in d.url for d in deals)
    assert any("광군제" in d.title for d in deals)
    assert len({d.external_id for d in deals}) == len(deals)


def test_dealbada_list_skips_notice():
    deals = parse_gnuboard(SOURCE_MAP["dealbada"], read("dealbada.html"))
    assert deals and all("이용안내" not in d.title for d in deals)
    assert all("wr_id=" in d.url for d in deals)


def test_danawa_rsc_payload():
    offers = parse_danawa(read("danawa.html"))
    assert len(offers) >= 10
    assert all(o.price > 0 and o.url.startswith("https://") for o in offers)
    assert any("RTX 5070" in o.title for o in offers)


def test_aliexpress_product_id():
    assert aliexpress_product_id("https://ko.aliexpress.com/item/1005006123456789.html?spm=x") == "1005006123456789"
    assert aliexpress_product_id("1005006123456789") == "1005006123456789"
    assert aliexpress_product_id("https://example.com") is None


def test_aliexpress_search_html_fallback():
    from app.sources.shopping import ali_search_url, parse_ali_search_html

    offers = parse_ali_search_html(read("aliexpress_search.html"))
    assert len(offers) >= 10
    o = offers[0]
    assert o.provider == "aliexpress" and o.is_overseas and o.price > 1000
    assert o.url.startswith("https://ko.aliexpress.com/item/") and o.image.startswith("https://")
    assert ali_search_url("ssd 1tb") == "https://ko.aliexpress.com/w/wholesale-ssd-1tb.html"


def test_source_fetch_retries_transient_error():
    import asyncio

    import httpx

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, text=read("ppomppu.xml"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await SOURCE_MAP["ppomppu"].fetch(client)

    assert len(asyncio.run(run())) >= 10 and calls["n"] == 2


def test_danawa_overseas_flag():
    from app.sources.shopping import _OVERSEAS_TITLE

    assert _OVERSEAS_TITLE.search("Western Digital WD BLACK SN850X M.2 NVMe 해외구매")
    assert not _OVERSEAS_TITLE.search("Western Digital WD BLACK SN850X M.2 NVMe")

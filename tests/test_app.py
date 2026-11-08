import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTDEAL_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ENABLE_SCHEDULER", "0")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_pages_render(client):
    for path in ["/", "/coupons", "/events", "/track", "/notifications", "/status", "/compare"]:
        assert client.get(path).status_code == 200, path


def test_keyword_alert_crud(client):
    r = client.post("/alerts/keywords", data={"keyword": "햇반", "max_price": "50,000"}, follow_redirects=False)
    assert r.status_code == 303
    assert "햇반" in client.get("/track").text


def test_store_deal_and_filters(client):
    from app.db import connect
    from app.services.collector import store_deal
    from app.sources.community import RawDeal

    conn = connect()
    raw = RawDeal("ppomppu8", "뽐뿌", "알리뽐뿌", "1", "[알리] 테스트 상품 ($10)", "https://x/1")
    assert store_deal(conn, raw, ("aliexpress",)) is not None
    assert store_deal(conn, raw, ("aliexpress",)) is None  # 중복
    conn.commit()
    conn.close()
    assert "테스트 상품" in client.get("/?ali=1").text
    assert client.get("/api/deals?ali=1").json()[0]["is_aliexpress"] == 1

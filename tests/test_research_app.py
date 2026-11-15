"""AI 최저가 화면·라우트 테스트 (가짜 실행기, 네트워크/API 없음)."""

import time

import pytest
from fastapi.testclient import TestClient

from app.services import research as R
from app.services.compare import CompareResult
from app.sources.shopping import Offer
from tests.test_research import DANAWA, outcome


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTDEAL_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("AI_ENABLED", "1")

    async def fake_compare(query, category="general", client=None, use_cache=True):
        offer = Offer("danawa", "다나와", "WD SN850X 1TB", 340000, DANAWA, mall="다나와 최저가(15개 몰)")
        offer.extra["landed_price"] = offer.total
        return CompareResult(query=query, offers=[offer], providers=[], cheapest=offer, cheapest_domestic=offer)

    async def fake_research(inp, settings, *, budget_left_usd, on_response=None):
        return outcome()

    monkeypatch.setattr(R, "compare", fake_compare)
    monkeypatch.setattr(R, "research", fake_research)
    import app.main as main

    monkeypatch.setattr(main, "compare", fake_compare)
    with TestClient(main.app) as c:
        yield c


def wait_done(client, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/research/{run_id}").json()
        if data["run"]["status"] not in ("queued", "running"):
            return data
        time.sleep(0.05)
    raise AssertionError("조사가 끝나지 않음")


def test_research_flow_from_form_to_result(client):
    page = client.get("/research")
    assert page.status_code == 200 and "AI로 최저가 찾기" in page.text and "disabled" not in page.text.split("AI로 최저가 찾기")[0][-30:]
    r = client.post("/research", data={"q": "WD SN850X 1TB", "include_overseas": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/research/")
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    data = wait_done(client, run_id)
    assert data["run"]["status"] == "done" and data["best"]["ref"] == "C1" and data["best"]["landed_price"] == 329000
    html = client.get(f"/research/{run_id}").text
    assert "확인된 최저가" in html and "329,000원" in html and "페이지 확인" in html and "출처" in html
    assert 'http-equiv="refresh"' not in html
    again = client.post("/research", data={"q": "wd sn850x 1tb", "include_overseas": "1"}, follow_redirects=False)
    assert again.headers["location"] == f"/research/{run_id}"  # 같은 날 같은 질의 → 저장된 결과
    listing = client.get("/research").text
    assert "WD SN850X 1TB" in listing and "329,000원" in listing


def test_cross_site_post_is_rejected(client):
    r = client.post("/research", data={"q": "x"}, headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    ok = client.post("/research", data={"q": "x"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303


def test_research_disabled_without_credentials(client, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "0")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    assert "ANTHROPIC_API_KEY" in client.get("/research").text
    r = client.post("/research", data={"q": "x"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_compare_page_offers_ai_button_and_track_ai_toggle(client):
    html = client.get("/compare?q=WD%20SN850X%201TB").text
    assert "AI로 더 찾아보기" in html and 'action="/research"' in html
    r = client.post("/track", data={"target": "WD SN850X 1TB", "name": "SN850X", "ai_daily": "1"},
                    follow_redirects=False)
    item_url = r.headers["location"]
    detail = client.get(item_url).text
    assert "매일 조사 켜짐" in detail and "AI 일별 최저가" in detail
    client.post(f"{item_url}/ai", data={"enabled": ""}, follow_redirects=False)
    assert "매일 조사 꺼짐" in client.get(item_url).text


def test_unsafe_candidate_links_are_not_rendered(client):
    r = client.post("/research", data={"q": "WD SN850X 1TB"}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    wait_done(client, run_id)
    from app.db import connect

    conn = connect()
    conn.execute("UPDATE research_candidates SET url='javascript:alert(1)' WHERE run_id=?", (run_id,))
    conn.commit()
    conn.close()
    assert "javascript:alert" not in client.get(f"/research/{run_id}").text

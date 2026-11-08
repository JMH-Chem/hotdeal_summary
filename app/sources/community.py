"""커뮤니티 핫딜 게시판 수집기.

원칙: 제목·가격·링크 등 메타데이터만 저장하고 본문은 복제하지 않는다(요약 일부만).
해외 IP에서 차단되는 소스(퀘이사존/아카라이브/펨코/쿨엔조이)는 기본 비활성.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from lxml import etree

from ..parsing import KST, parse_kst, parse_rss_date


@dataclass
class RawDeal:
    source: str
    source_name: str
    board: str
    external_id: str
    title: str
    url: str
    author: str | None = None
    summary: str | None = None
    thumbnail: str | None = None
    category: str | None = None
    views: int | None = None
    posted_at: datetime | None = None


@dataclass
class CommunitySource:
    key: str
    name: str
    board: str
    url: str
    parser: Callable[["CommunitySource", str], list[RawDeal]]
    enabled_by_default: bool = True
    note: str = ""
    # 본문(쿠폰 추출용)을 가져올 때 쓸 CSS 셀렉터
    detail_selector: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    async def fetch(self, client: httpx.AsyncClient, retries: int = 1) -> list[RawDeal]:
        for attempt in range(retries + 1):
            try:
                resp = await client.get(self.url)
                resp.raise_for_status()
                return self.parser(self, resp.text)
            except (httpx.TransportError, httpx.RemoteProtocolError):
                if attempt >= retries:
                    raise
                await asyncio.sleep(2 * (attempt + 1))  # 일시적 연결 끊김은 한 번 더 시도
        return []

    async def fetch_detail_text(self, client: httpx.AsyncClient, url: str) -> str | None:
        if not self.detail_selector:
            return None
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        node = soup.select_one(self.detail_selector)
        if node is None:
            return None
        return node.get_text("\n", strip=True)


# ---------------------------------------------------------------- 파서

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def _clean_summary(html: str | None, limit: int = 300) -> tuple[str | None, str | None]:
    if not html:
        return None, None
    img = _IMG_RE.search(html)
    text = htmllib.unescape(_TAG_RE.sub(" ", html))
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:limit] or None), (img.group(1) if img else None)


def _last_digits(url: str) -> str:
    m = re.search(r"(?:no=|wr_id=|/)(\d{4,})(?:\D*$|[&#?])", url)
    if m:
        return m.group(1)
    nums = re.findall(r"\d{4,}", url)
    return nums[-1] if nums else url


def parse_rss(src: CommunitySource, text: str) -> list[RawDeal]:
    root = etree.fromstring(text.encode("utf-8") if isinstance(text, str) else text,
                            parser=etree.XMLParser(recover=True))
    deals: list[RawDeal] = []
    if root is None:
        return deals
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        summary, thumb = _clean_summary(item.findtext("description"))
        views = None
        hits = item.findtext("hits")  # 뽐뿌: " [댓글|조회|추천|비추]"
        if hits:
            nums = re.findall(r"\d+", hits)
            if len(nums) >= 2:
                views = int(nums[1])
        deals.append(
            RawDeal(
                source=src.key,
                source_name=src.name,
                board=src.board,
                external_id=_last_digits(link),
                title=title,
                url=link.replace("http://", "https://", 1),
                author=(item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip() or None,
                summary=summary,
                thumbnail=thumb,
                category=(item.findtext("category") or "").strip() or None,
                views=views,
                posted_at=parse_rss_date(item.findtext("pubDate")),
            )
        )
    return deals


def parse_clien(src: CommunitySource, text: str) -> list[RawDeal]:
    soup = BeautifulSoup(text, "lxml")
    deals: list[RawDeal] = []
    seen: set[str] = set()
    for row in soup.select("div.list_item.symph_row"):
        subject = row.select_one("span.list_subject")
        link = None
        if subject:
            link = next((a for a in subject.select("a[href]") if "list_reply" not in (a.get("class") or [])), None)
        if link is None:
            link = row.select_one("a.list_subject[href]")
        if link is None:
            continue
        href = link["href"].split("?")[0]
        if "/service/board/jirum/" not in href:
            continue
        ext_id = href.rstrip("/").split("/")[-1]
        if ext_id in seen:
            continue
        seen.add(ext_id)
        title = (subject.get("title") if subject is not None and subject.get("title") else link.get_text(" ", strip=True)).strip()
        nick = row.select_one("span.nickname")
        ts = row.select_one("span.timestamp")
        hit = row.select_one("span.hit")
        deals.append(
            RawDeal(
                source=src.key,
                source_name=src.name,
                board=src.board,
                external_id=ext_id,
                title=title,
                url=urljoin("https://www.clien.net", href),
                author=nick.get_text(strip=True) if nick else None,
                posted_at=parse_kst(ts.get_text(strip=True)) if ts else None,
                views=_parse_hit(hit.get_text(strip=True)) if hit else None,
            )
        )
    return deals


def _parse_hit(text: str) -> int | None:
    m = re.match(r"([\d.]+)\s*(k)?", text.replace(",", "").strip(), re.I)
    if not m:
        return None
    val = float(m.group(1))
    return int(val * 1000) if m.group(2) else int(val)


def parse_gnuboard(src: CommunitySource, text: str) -> list[RawDeal]:
    """그누보드 기본 목록(딜바다 등)."""
    soup = BeautifulSoup(text, "lxml")
    deals: list[RawDeal] = []
    now_kst = datetime.now(KST)
    for tr in soup.select("tr"):
        cell = tr.select_one("td.td_subject")
        if cell is None:
            continue
        a = cell.select_one("a[href*='wr_id=']")
        if a is None:
            continue
        num = tr.select_one("td.td_num")
        if num is not None and not num.get_text(strip=True).isdigit():
            continue  # 공지
        for junk in a.select(".sound_only, .cnt_cmt"):
            junk.decompose()
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        href = urljoin("https://www.dealbada.com/", a["href"])
        m = re.search(r"wr_id=(\d+)", href)
        if not title or not m:
            continue
        posted = None
        date_cell = tr.select_one("td.td_date")
        if date_cell:
            raw = date_cell.get_text(strip=True)
            if re.fullmatch(r"\d{1,2}:\d{2}", raw):
                h, mnt = map(int, raw.split(":"))
                posted = now_kst.replace(hour=h, minute=mnt, second=0, microsecond=0).astimezone(timezone.utc)
            elif re.fullmatch(r"\d{2}-\d{2}", raw):
                mo, d = map(int, raw.split("-"))
                year = now_kst.year if (mo, d) <= (now_kst.month, now_kst.day) else now_kst.year - 1
                posted = datetime(year, mo, d, tzinfo=KST).astimezone(timezone.utc)
        author = tr.select_one("td.td_name")
        deals.append(
            RawDeal(
                source=src.key,
                source_name=src.name,
                board=src.board,
                external_id=m.group(1),
                title=title,
                url=re.sub(r"&page=\d+", "", href).replace("&amp;", "&"),
                author=author.get_text(" ", strip=True)[:30] if author else None,
                posted_at=posted,
            )
        )
    return deals


def parse_unavailable(src: CommunitySource, text: str) -> list[RawDeal]:  # pragma: no cover
    raise RuntimeError(f"{src.name}: 아직 파서가 없습니다(해외 IP 차단으로 샘플 미확보). {src.note}")


# ---------------------------------------------------------------- 레지스트리

PPOMPPU_DETAIL = "td.board-contents, div.board-contents"

SOURCES: list[CommunitySource] = [
    CommunitySource("ppomppu", "뽐뿌", "국내뽐뿌", "https://www.ppomppu.co.kr/rss.php?id=ppomppu", parse_rss,
                    detail_selector=PPOMPPU_DETAIL),
    CommunitySource("ppomppu4", "뽐뿌", "해외뽐뿌", "https://www.ppomppu.co.kr/rss.php?id=ppomppu4", parse_rss,
                    detail_selector=PPOMPPU_DETAIL, tags=("overseas",)),
    CommunitySource("ppomppu8", "뽐뿌", "알리뽐뿌", "https://www.ppomppu.co.kr/rss.php?id=ppomppu8", parse_rss,
                    detail_selector=PPOMPPU_DETAIL, tags=("aliexpress", "overseas")),
    CommunitySource("ppomppu_coupon", "뽐뿌", "쿠폰게시판", "https://www.ppomppu.co.kr/rss.php?id=coupon", parse_rss,
                    detail_selector=PPOMPPU_DETAIL, tags=("coupon",)),
    CommunitySource("ppomppu_event", "뽐뿌", "이벤트게시판", "https://www.ppomppu.co.kr/rss.php?id=event", parse_rss,
                    detail_selector=PPOMPPU_DETAIL, tags=("event",)),
    CommunitySource("ruliweb", "루리웹", "유저 핫딜", "https://bbs.ruliweb.com/market/board/1020/rss", parse_rss,
                    detail_selector="div.view_content"),
    CommunitySource("clien", "클리앙", "알뜰구매", "https://www.clien.net/service/board/jirum", parse_clien,
                    detail_selector="div.post_article"),
    CommunitySource("dealbada", "딜바다", "국내핫딜", "https://www.dealbada.com/bbs/board.php?bo_table=deal_domestic",
                    parse_gnuboard, detail_selector="#bo_v_con"),
    CommunitySource("dealbada_oversea", "딜바다", "해외핫딜", "https://www.dealbada.com/bbs/board.php?bo_table=deal_oversea",
                    parse_gnuboard, detail_selector="#bo_v_con", tags=("overseas",)),
    CommunitySource("eomisae", "어미새", "패션 인기정보", "https://eomisae.co.kr/fs/rss", parse_rss,
                    detail_selector="div.xe_content, article .rd_body"),
    # --- 해외 IP 차단 확인된 소스: 국내 서버에서 ENABLED_SOURCES로 켜고 파서 보완 필요
    CommunitySource("quasarzone", "퀘이사존", "지름/핫딜", "https://quasarzone.com/bbs/qb_saleinfo", parse_unavailable,
                    enabled_by_default=False, note="Cloudflare 403"),
    CommunitySource("fmkorea", "에펨코리아", "핫딜", "https://www.fmkorea.com/hotdeal", parse_unavailable,
                    enabled_by_default=False, note="봇 차단 430"),
    CommunitySource("arca", "아카라이브", "핫딜 채널", "https://arca.live/b/hotdeal", parse_unavailable,
                    enabled_by_default=False, note="Cloudflare 403"),
    CommunitySource("coolenjoy", "쿨엔조이", "알뜰구매", "https://coolenjoy.net/bbs/jirum", parse_unavailable,
                    enabled_by_default=False, note="해외 IP 타임아웃"),
]

SOURCE_MAP = {s.key: s for s in SOURCES}


def enabled_sources(selected: tuple[str, ...] = ()) -> list[CommunitySource]:
    if selected:
        return [SOURCE_MAP[k] for k in selected if k in SOURCE_MAP]
    return [s for s in SOURCES if s.enabled_by_default]

"""커뮤니티 글에서 쿠폰/프로모션 코드를 추출한다.

광군제 등 행사 때 커뮤니티·유튜버 전용 코드가 여러 글에 흩어져 올라오므로,
'코드/쿠폰' 문맥 근처의 대문자+숫자 토큰만 코드로 인정해 모델명 오탐을 줄인다.
"""

from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..db import utcnow_iso
from ..parsing import KST

PLATFORMS: list[tuple[str, re.Pattern[str]]] = [
    ("AliExpress", re.compile(r"알리|aliexpress", re.I)),
    ("Temu", re.compile(r"테무|temu", re.I)),
    ("쿠팡", re.compile(r"쿠팡|coupang", re.I)),
    ("11번가", re.compile(r"11번가|11st", re.I)),
    ("G마켓/옥션", re.compile(r"g마켓|지마켓|gmarket|옥션|auction", re.I)),
    ("SSG", re.compile(r"ssg|신세계|이마트", re.I)),
    ("무신사", re.compile(r"무신사|musinsa", re.I)),
    ("컬리", re.compile(r"컬리|kurly", re.I)),
    ("네이버", re.compile(r"네이버|naver", re.I)),
    ("아마존", re.compile(r"아마존|amazon", re.I)),
    ("큐텐", re.compile(r"큐텐|qoo10", re.I)),
]

EVENT_KEYWORDS: list[tuple[str, re.Pattern[str]]] = [
    ("aliexpress-1111-2026", re.compile(r"광군제|11\s*[.·/]\s*11|1111|싱글즈\s*데이", re.I)),
    ("black-friday-2026", re.compile(r"블랙\s*프라이데이|블프|black\s*friday", re.I)),
    ("cyber-monday-2026", re.compile(r"사이버\s*먼데이|cyber\s*monday", re.I)),
]

_CUE = re.compile(r"코드|쿠폰|code|프로모션|promo|입력", re.I)
_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Z0-9]{4,20})(?![A-Za-z0-9])")
_UNIT_SUFFIX = re.compile(r"\d+(GB|TB|MB|ML|MM|CM|KG|W|WH|HZ|MAH|K|P|V|A|G|L|M|EA|MG|IN)$")
_STOP = {"HTTP", "HTTPS", "HTML", "USB", "HDMI", "NVME", "DDR4", "DDR5", "LTE", "USD", "KRW", "SSD", "CPU", "GPU",
         "RTX", "GTX", "OLED", "LED", "QLED", "FHD", "UHD", "WIFI", "PCIE", "TYPE", "VIP", "BEST", "SALE", "FREE",
         "CHOICE", "AMD", "INTEL", "APPLE", "IPHONE", "GALAXY", "NEW", "HOT"}
_MODEL_LIKE = re.compile(r"^(RTX|GTX|RX|SM|DDR|PCIE|USB|IP|GT|MX|ZEN|M\d|A\d{2}|S\d{2}|Z\d|X\d{3})", re.I)

_AMOUNT = r"(\d{1,3}(?:,\d{3})+|\d+)\s*(만)?\s*(?:원|₩|달러|불|USD)?"
_MIN_SPEND_RE = re.compile(r"(?:₩|\$|US\s*\$)?\s*" + _AMOUNT + r"\s*(?:이상|↑|over)", re.I)
_DISCOUNT_RE = re.compile(r"(?:₩|\$|US\s*\$)?\s*" + _AMOUNT + r"\s*(?:할인|off|DC|즉시)", re.I)
_PAIR_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*/\s*(\d{1,3}(?:,\d{3})+|\d+)")  # 15,000/100,000
_PERCENT_RE = re.compile(r"(\d{1,2})\s*%\s*(?:할인|off|쿠폰)", re.I)
_UNTIL_RE = re.compile(r"(?:~|까지|until)\s*(\d{1,2})\s*[./월]\s*(\d{1,2})|(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?\s*(?:까지|마감)")
_YOUTUBER_RE = re.compile(r"(?:유튜버|유튜브)\s*[\"'「]?([가-힣A-Za-z0-9_.]{2,20})|([가-힣A-Za-z0-9_.]{2,20})\s*(?:전용|단독|구독자)\s*(?:코드|쿠폰|할인)")


@dataclass
class CouponCandidate:
    platform: str
    code: str
    discount_amount: int | None
    discount_percent: int | None
    min_spend: int | None
    currency: str
    valid_until: str | None
    source_name: str | None
    context: str
    event_slug: str | None


def _to_int(num: str, man: str | None) -> int:
    value = int(num.replace(",", ""))
    return value * 10000 if man else value


def detect_platform(text: str, default: str = "기타") -> str:
    for name, pat in PLATFORMS:
        if pat.search(text):
            return name
    return default


def detect_event(text: str) -> str | None:
    for slug, pat in EVENT_KEYWORDS:
        if pat.search(text):
            return slug
    return None


def _is_code(token: str) -> bool:
    if token in _STOP or token.isdigit() or token.isalpha() and len(token) < 5:
        return False
    if not re.search(r"[A-Z]", token):
        return False
    if _UNIT_SUFFIX.search(token) or _MODEL_LIKE.match(token):
        return False
    if re.fullmatch(r"20\d{2}[A-Z]?", token):
        return False
    return True


def _segments(text: str) -> list[str]:
    parts = re.split(r"[\n\r]+|(?<=[.!?])\s+|\s{2,}|[|•·▶▷►]", text)
    return [p.strip() for p in parts if p.strip()]


_STRONG_CUE = re.compile(r"코드|쿠폰|code|promo|프로모션|입력", re.I)
_WEAK_CUE = re.compile(r"(?:➡|→|=>|>|<|:|：|\(|\[)\s*$")
_AMOUNT_TAIL = re.compile(r"(?:\$\s*(\d+(?:\.\d+)?)|(\d{1,3}(?:,\d{3})+|\d+)\s*(만)?\s*(원|달러|불|USD))", re.I)


def _code_in_context(tok: str, seg: str, start: int, prev_code_end: int) -> bool:
    before = seg[max(0, start - 30): start]
    if start - prev_code_end <= 6 and prev_code_end >= 0:  # "GONS04 , KBTY04" 처럼 코드 나열
        return True
    if tok.isalpha():  # 순수 영문 코드는 강한 단서(코드/쿠폰)가 바로 앞에 있어야 인정
        return bool(_STRONG_CUE.search(seg[max(0, start - 15): start]))
    return bool(_STRONG_CUE.search(before) or _WEAK_CUE.search(seg[max(0, start - 3): start]))


def _in_title_as_model(tok: str, title: str | None) -> bool:
    if not title or tok not in title:
        return False
    idx = title.index(tok)
    return not _STRONG_CUE.search(title[max(0, idx - 15): idx])


def _parse_until(m: re.Match[str], now: datetime) -> str | None:
    mo, d = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    try:
        year = now.year if int(mo) >= now.month - 1 else now.year + 1
        return datetime(year, int(mo), int(d)).date().isoformat()
    except ValueError:
        return None


def extract_coupons(text: str, *, default_platform: str | None = None, source_name: str | None = None,
                    title: str | None = None, event_slug: str | None = None, event_end: str | None = None,
                    now: datetime | None = None) -> list[CouponCandidate]:
    """코드 추출. event_slug/event_end: 진행 중인 행사(예: 광군제) 기간이면 기본으로 연결."""
    now = now or datetime.now(KST)
    text = html.unescape(text)
    whole_platform = default_platform or detect_platform(text)
    whole_event = detect_event(text) or event_slug
    youtuber = None
    ym = _YOUTUBER_RE.search(text)
    if ym:
        youtuber = ym.group(1) or ym.group(2)
    doc_until = list(_UNTIL_RE.finditer(text))
    results: dict[str, CouponCandidate] = {}
    segs = _segments(text)
    for i, seg in enumerate(segs):
        window = " ".join(segs[max(0, i - 1): i + 2])
        prev_end = -100
        for m in _TOKEN.finditer(seg):
            tok = m.group(1)
            if not _is_code(tok) or tok in results or _in_title_as_model(tok, title):
                continue
            if not _code_in_context(tok, seg, m.start(), prev_end):
                continue
            prev_end = m.end()
            before = seg[max(0, m.start() - 40): m.start()]
            line = seg[: m.start()]
            amount = percent = min_spend = None
            currency = "USD" if re.search(r"\$|달러|USD", line or seg) else "KRW"
            tails = list(_AMOUNT_TAIL.finditer(before))
            mm = _MIN_SPEND_RE.search(line) or _MIN_SPEND_RE.search(seg)
            if mm:
                min_spend = _to_int(mm.group(1), mm.group(2))
            if tails:
                t = tails[-1]
                amount = int(round(float(t.group(1)))) if t.group(1) else _to_int(t.group(2), t.group(3))
                if min_spend is not None and amount == min_spend and len(tails) == 1:
                    amount = None
            if amount is None:
                dm = _DISCOUNT_RE.search(line) or _DISCOUNT_RE.search(seg)
                if dm:
                    amount = _to_int(dm.group(1), dm.group(2))
            pair = _PAIR_RE.search(seg)
            if pair and amount is None and min_spend is None:
                a, b = (int(x.replace(",", "")) for x in pair.groups())
                amount, min_spend = min(a, b), max(a, b)
            pm = _PERCENT_RE.search(seg)
            if pm:
                percent = int(pm.group(1))
            if min_spend is not None and amount is not None and amount > min_spend:
                amount, min_spend = min_spend, amount
            um = _UNTIL_RE.search(window) or (doc_until[-1] if doc_until else None)
            valid_until = _parse_until(um, now) if um else None
            ev = detect_event(window) or whole_event
            if valid_until is None and ev and ev == event_slug and event_end:
                valid_until = event_end
            results[tok] = CouponCandidate(
                platform=detect_platform(window, whole_platform),
                code=tok,
                discount_amount=amount,
                discount_percent=percent,
                min_spend=min_spend,
                currency=currency,
                valid_until=valid_until,
                source_name=youtuber or source_name,
                context=seg[:160],
                event_slug=ev,
            )
    return list(results.values())


def save_coupons(conn: sqlite3.Connection, coupons: list[CouponCandidate], source_url: str) -> int:
    now = utcnow_iso()
    inserted = 0
    for c in coupons:
        cur = conn.execute(
            """INSERT INTO coupons (platform, code, discount_amount, discount_percent, min_spend, currency,
                                    valid_until, source_name, source_url, context, event_slug, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(platform, code) DO UPDATE SET
                 last_seen=excluded.last_seen,
                 -- 새 글이 '최소주문+할인액'을 모두 갖춘(더 완전한) 정보면 금액/통화/출처를 통째로 교체해 섞임 방지
                 discount_amount=CASE WHEN {better} THEN excluded.discount_amount
                                      ELSE COALESCE(coupons.discount_amount, excluded.discount_amount) END,
                 currency=CASE WHEN {better} THEN excluded.currency ELSE coupons.currency END,
                 min_spend=CASE WHEN {better} THEN excluded.min_spend ELSE coupons.min_spend END,
                 source_name=CASE WHEN {better} THEN excluded.source_name ELSE coupons.source_name END,
                 source_url=CASE WHEN {better} THEN excluded.source_url ELSE coupons.source_url END,
                 context=CASE WHEN {better} THEN excluded.context ELSE coupons.context END,
                 discount_percent=COALESCE(coupons.discount_percent, excluded.discount_percent),
                 valid_until=COALESCE(coupons.valid_until, excluded.valid_until),
                 event_slug=COALESCE(coupons.event_slug, excluded.event_slug)""".format(
                better="(coupons.min_spend IS NULL AND excluded.min_spend IS NOT NULL AND excluded.discount_amount IS NOT NULL)"),
            (c.platform, c.code, c.discount_amount, c.discount_percent, c.min_spend, c.currency, c.valid_until,
             c.source_name, source_url, c.context, c.event_slug, now, now),
        )
        if cur.rowcount and cur.lastrowid:
            inserted += 1
    return inserted

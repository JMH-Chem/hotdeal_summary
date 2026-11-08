"""핫딜 제목 파싱: `[쇼핑몰] 상품명 (가격/배송비)` 형태에서 메타데이터 추출."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))

ALI_PATTERN = re.compile(r"알리|aliexpress|ali\s*express", re.I)
COUPON_PATTERN = re.compile(r"쿠폰|프로모션\s*코드|할인\s*코드|promo|코드\s*[:：]|전용\s*코드", re.I)

_MALL_RE = re.compile(r"^\s*[\[【(]([^\]】)]{1,30})[\]】)]")
_KRW_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(만\s*)?원")
_USD_RE = re.compile(r"(?:\$|US\s*\$|USD\s*)\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:달러|불)\b")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{3,}")
_FREE_SHIP_RE = re.compile(r"무료|무배|free", re.I)

CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("상품권/포인트", ("상품권", "포인트", "네이버페이", "적립", "캐시", "기프티콘")),
    ("PC/하드웨어", ("ssd", "rtx", "그래픽", "cpu", "라이젠", "모니터", "키보드", "마우스", "램 ", "nvme", "미니 pc", "미니pc", "노트북")),
    ("가전/디지털", ("tv", "에어팟", "갤럭시", "아이폰", "아이패드", "청소기", "이어폰", "헤드폰", "충전기", "보조배터리", "공기청정", "냉장고", "세탁기")),
    ("식품", ("햇반", "라면", "커피", "생수", "고기", "닭", "과자", "우유", "김치", "쌀", "도넛", "음료", "치킨", "피자")),
    ("생활/뷰티", ("샴푸", "세제", "휴지", "물티슈", "치약", "로션", "화장품", "선크림", "바디")),
    ("패션", ("신발", "운동화", "러닝화", "자켓", "패딩", "티셔츠", "바지", "나이키", "아디다스", "뉴발란스")),
    ("게임/SW", ("스팀", "steam", "닌텐도", "스위치", "ps5", "플스", "엑스박스", "게임")),
]


@dataclass
class ParsedTitle:
    mall: str | None
    price: int | None
    currency: str | None
    shipping: str | None


def parse_title(title: str) -> ParsedTitle:
    mall = None
    m = _MALL_RE.match(title)
    if m:
        mall = m.group(1).strip()

    price: int | None = None
    currency: str | None = None
    shipping: str | None = None

    # 1) 마지막 괄호 그룹 우선: "(67,684원/무배)", "(23,218 / 무료배송)", "($372)"
    groups = _PAREN_RE.findall(title)
    for group in reversed(groups):
        parts = [p.strip() for p in re.split(r"[/|]", group)]
        p_val, p_cur = _extract_price(parts[0]) if parts else (None, None)
        if p_val is None:
            continue
        price, currency = p_val, p_cur
        if len(parts) > 1:
            shipping = _normalize_shipping(parts[1])
        break

    # 2) 제목 전체에서 'N원' / '$N'
    if price is None:
        price, currency = _extract_price(title, require_unit=True)

    if shipping is None and re.search(r"무료\s*배송|무배|무료\)", title):
        shipping = "무료배송"
    return ParsedTitle(mall=mall, price=price, currency=currency, shipping=shipping)


def _extract_price(text: str, require_unit: bool = False) -> tuple[int | None, str | None]:
    usd = _USD_RE.search(text)
    krw = _KRW_RE.search(text)
    if krw and (not usd or krw.start() <= usd.start()):
        value = int(krw.group(1).replace(",", ""))
        if krw.group(2):  # "3만원"
            value *= 10000
        return value, "KRW"
    if usd:
        raw = usd.group(1) or usd.group(2)
        return int(round(float(raw.replace(",", "")))), "USD"
    if require_unit:
        return None, None
    num = _NUM_RE.search(text)
    if num:
        return int(num.group(0).replace(",", "")), "KRW"
    return None, None


def _normalize_shipping(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if _FREE_SHIP_RE.search(text):
        return "무료배송"
    return text[:30]


def guess_category(title: str, hint: str | None = None) -> str | None:
    if hint:
        return hint
    low = title.lower()
    for name, words in CATEGORY_KEYWORDS:
        if any(w in low for w in words):
            return name
    return None


def is_aliexpress(*texts: str | None) -> bool:
    return any(t and ALI_PATTERN.search(t) for t in texts)


def looks_like_coupon(*texts: str | None) -> bool:
    return any(t and COUPON_PATTERN.search(t) for t in texts)


def dedupe_key(title: str, mall: str | None, price: int | None) -> str:
    body = _MALL_RE.sub("", title)
    body = _PAREN_RE.sub("", body)
    body = re.sub(r"[^0-9a-z가-힣]", "", body.lower())[:24]
    mall_key = re.sub(r"[^0-9a-z가-힣]", "", (mall or "").lower())
    return f"{mall_key}|{body}|{price or ''}"


def parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_kst(value: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime | None:
    try:
        return datetime.strptime(value.strip(), fmt).replace(tzinfo=KST).astimezone(timezone.utc)
    except ValueError:
        return None


def to_kst_str(iso: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(KST).strftime(fmt)
    except ValueError:
        return iso


def deal_search_query(title: str, max_words: int = 6) -> str:
    """핫딜 제목 → 가격비교 검색어: [쇼핑몰]·(가격/배송)·가격 문구 제거 후 앞 단어만."""
    text = _MALL_RE.sub("", title)
    text = _PAREN_RE.sub(" ", text)
    text = re.sub(r"[\d,]+\s*(?:원|만원)|\$\s*[\d.,]+|무료배송|무배|/\s*\S*배송", " ", text)
    words = [w.strip(",.;:/+-") for w in re.split(r"\s+", text)]
    words = [w for w in words if w and not re.fullmatch(r"[\W_]+", w)]
    return " ".join(words[:max_words])


def format_won(value: int | float | None) -> str:
    if value is None:
        return "-"
    return f"{int(round(value)):,}원"

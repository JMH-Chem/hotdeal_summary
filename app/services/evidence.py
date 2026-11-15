"""AI 조사 응답에서 증거(가져온 페이지·검색 결과·인용)를 뽑고, 후보 가격을 결정적으로 검증·정렬한다.

원칙: AI가 말한 가격은 "주장"이고, Anthropic 서버 도구가 실제로 가져온 페이지 본문이나
검색 인용문에 같은 금액이 있을 때만 '확인됨'으로 본다. 우리가 AI 제공 URL을 직접 요청하지는
않는다(SSRF 방지). 모든 함수는 순수 함수라 응답 dict만으로 테스트할 수 있다.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from ..sources.shopping import aliexpress_product_id
from .customs import estimate_import_tax

# ------------------------------------------------------------------ 금액 추출

_KRW_COMMA = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(?![\d,]|\.\d)")
_KRW_PLAIN = re.compile(r"(?<![\d.,])(\d{4,9})(?![\d,]|\.\d)")
_KRW_SMALL_WON = re.compile(r"(?<![\d.,])(\d{2,3})\s*원")
_KRW_MAN = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)\s*만(?:\s*(\d{1,4})\s*천)?(?:\s*(\d,\d{3}|\d{1,4})\s*원)?")
_USD = re.compile(r"(?:US\s*)?\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
                  r"|(\d+(?:\.\d{1,2})?)\s*(?:달러|USD)", re.I)


def extract_amounts(text: str) -> tuple[set[int], set[float]]:
    """텍스트 안의 금액 후보: (원화 정수 집합, 달러 집합). 단위 없는 숫자는 4자리 이상만 인정."""
    krw: set[int] = set()
    usd: set[float] = set()
    if not text:
        return krw, usd
    for m in _KRW_COMMA.finditer(text):
        krw.add(int(m.group(1).replace(",", "")))
    for m in _KRW_PLAIN.finditer(text):
        krw.add(int(m.group(1)))
    for m in _KRW_SMALL_WON.finditer(text):
        krw.add(int(m.group(1)))
    for m in _KRW_MAN.finditer(text):
        value = float(m.group(1)) * 10000
        if m.group(2):
            value += int(m.group(2)) * 1000
        if m.group(3):
            value += int(m.group(3).replace(",", ""))
        krw.add(int(round(value)))
    for m in _USD.finditer(text):
        raw = m.group(1) or m.group(2)
        usd.add(round(float(raw.replace(",", "")), 2))
    return krw, usd


def price_in_text(text: str, price_krw: int | None, original_amount: float | None = None,
                  original_currency: str | None = None) -> bool:
    if not text or not price_krw or price_krw <= 0:
        return False
    krw, usd = extract_amounts(text)
    if price_krw in krw:
        return True
    if original_currency and original_currency.upper() == "USD" and original_amount and original_amount > 0:
        return round(float(original_amount), 2) in usd
    return False


# ------------------------------------------------------------------ URL 정규화

_TRACKING = re.compile(
    r"^(utm_.*|napm|spm|gclid|fbclid|scm|pvid|aff_.*|af|cv|dp|sk|src|ref|referrer|trackingid|traceid|"
    r"sourcetype|searchid|isaddedcart|rank|clicktype|n_.*|nt_.*|nacn|fromwhere|ndid)$",
    re.I,
)
_RETAILER_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # (키 접두어, 정규식, 대상: path|query 파라미터 이름)
    ("coupang", re.compile(r"coupang\.com$"), r"/v[pm]/products/(\d+)"),
    ("naverstore", re.compile(r"(smartstore|brand)\.naver\.com$"), r"/products/(\d+)"),
    ("navercat", re.compile(r"shopping\.naver\.com$"), r"/catalog/(\d+)"),
    ("11st", re.compile(r"11st\.co\.kr$"), r"/products/(?:pa/)?(\d+)"),
    ("lotteon", re.compile(r"lotteon\.com$"), r"/p/product/([A-Za-z0-9]+)"),
]
_RETAILER_QUERY: list[tuple[str, re.Pattern[str], str]] = [
    ("danawa", re.compile(r"danawa\.com$"), "pcode"),
    ("gmarket", re.compile(r"gmarket\.co\.kr$"), "goodscode"),
    ("auction", re.compile(r"auction\.co\.kr$"), "itemno"),
    ("ssg", re.compile(r"ssg\.com$"), "itemid"),
    ("11st", re.compile(r"11st\.co\.kr$"), "prdno"),
]


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def normalize_url(url: str) -> str:
    """같은 상품 페이지를 같은 키로: 판매처별 상품 ID가 있으면 그것, 아니면 추적 파라미터를 뺀 정규형."""
    url = (url or "").strip()
    if not url:
        return ""
    ali = aliexpress_product_id(url)
    if ali:
        return f"ali:{ali}"
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = _host(url)
    path = unquote(parts.path or "/").rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)]
    for prefix, host_re, path_re in _RETAILER_PATTERNS:
        if host_re.search(host):
            m = re.search(path_re, path)
            if m:
                return f"{prefix}:{m.group(1)}"
    lower_q = {k.lower(): v for k, v in query}
    for prefix, host_re, param in _RETAILER_QUERY:
        if host_re.search(host) and lower_q.get(param):
            return f"{prefix}:{lower_q[param]}"
    kept = sorted((k, v) for k, v in query if not _TRACKING.match(k))
    return f"{host}{path}" + (f"?{urlencode(kept)}" if kept else "")


def is_safe_link(url: str | None) -> bool:
    """화면에 링크로 걸어도 되는지 (javascript:, data: 등 차단)."""
    if not url:
        return False
    try:
        return urlsplit(url.strip()).scheme in ("http", "https")
    except ValueError:
        return False


KNOWN_SHOP_DOMAINS = (
    "coupang.com", "11st.co.kr", "gmarket.co.kr", "auction.co.kr", "ssg.com", "lotteon.com", "naver.com",
    "danawa.com", "enuri.com", "gsshop.com", "cjonstyle.com", "hmall.com", "lotteimall.com", "nsmall.com",
    "hnsmall.com", "kshop.co.kr", "skstoa.com", "shinsegaetvshopping.com", "wshop.co.kr", "gongyoungshop.kr",
    "e-himart.co.kr", "himart.co.kr", "emart.com", "homeplus.co.kr", "kurly.com", "musinsa.com",
    "oliveyoung.co.kr", "29cm.co.kr", "wconcept.co.kr", "interpark.com", "compuzone.co.kr", "aliexpress.com",
    "aliexpress.us", "amazon.com", "amazon.co.jp", "ebay.com", "qoo10.com", "temu.com", "iherb.com",
    "bestbuy.com", "newegg.com", "apple.com", "samsung.com", "lge.co.kr", "ppomppu.co.kr", "quasarzone.com",
    "ruliweb.com", "clien.net", "fmkorea.com", "arca.live", "coolenjoy.net", "dealbada.com", "eomisae.co.kr",
    "kakao.com", "tossshopping.com", "toss.im", "11st.com", "daiso.co.kr", "costco.co.kr", "lotte.com",
)
COMMUNITY_DOMAINS = ("ppomppu.co.kr", "quasarzone.com", "ruliweb.com", "clien.net", "fmkorea.com", "arca.live",
                     "coolenjoy.net", "dealbada.com", "eomisae.co.kr")


def _domain_in(url: str, domains: Iterable[str]) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in domains)


def is_known_shop(url: str) -> bool:
    return _domain_in(url, KNOWN_SHOP_DOMAINS)


_LIST_PAGE = re.compile(r"(search|/w/wholesale|/list|catalog/category|query=|keyword=|q=|/np/search|/bbs/board)", re.I)


def is_list_page(url: str) -> bool:
    return bool(_LIST_PAGE.search(url or ""))


# ------------------------------------------------------------------ 응답 → 증거

_REF_RE = re.compile(r"\[([CS]\d{1,2})\]")


@dataclass
class FetchedDoc:
    url: str
    title: str
    text: str
    retrieved_at: str | None = None


@dataclass
class Cite:
    url: str
    title: str
    cited_text: str
    ref: str | None


@dataclass
class SearchHit:
    url: str
    title: str
    page_age: str | None = None


@dataclass
class Evidence:
    report_text: str = ""
    fetched: dict[str, FetchedDoc] = field(default_factory=dict)
    search_hits: dict[str, SearchHit] = field(default_factory=dict)
    citations: list[Cite] = field(default_factory=list)
    seen_keys: set[str] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)
    fetch_errors: list[tuple[str, str]] = field(default_factory=list)   # (url, error_code)
    search_errors: list[str] = field(default_factory=list)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "queries": self.queries,
            "search_results": len(self.search_hits),
            "fetched_pages": len(self.fetched),
            "citations": len(self.citations),
            "fetch_errors": self.fetch_errors[:30],
            "search_errors": self.search_errors[:10],
        }


_TOOL_BLOCK_TYPES = {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}


def _nested_tool_blocks(value: Any) -> Iterable[dict[str, Any]]:
    """동적 필터링(코드 실행 안에서 부른 검색/페치) 결과가 다른 블록 안에 들어 있어도 찾아낸다."""
    if isinstance(value, dict):
        if value.get("type") in _TOOL_BLOCK_TYPES:
            yield value
            return
        for v in value.values():
            yield from _nested_tool_blocks(v)
    elif isinstance(value, list):
        for v in value:
            yield from _nested_tool_blocks(v)


def _blocks(responses: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for resp in responses:
        for block in resp.get("content") or []:
            if not isinstance(block, dict):
                continue
            yield block
            if block.get("type") not in _TOOL_BLOCK_TYPES and block.get("type") != "text":
                for key, value in block.items():
                    if key != "type":
                        yield from _nested_tool_blocks(value)


def _doc_text(document: dict[str, Any]) -> str:
    source = document.get("source") or {}
    if source.get("type") == "text":
        return str(source.get("data") or "")
    return ""  # PDF(base64) 등은 검증 대상에서 제외


def build_evidence(responses: list[dict[str, Any]], seed_urls: Iterable[str] = ()) -> Evidence:
    """조사 응답들(.to_dict())에서 증거를 모은다. seed_urls: 우리가 프롬프트에 넣은 기준가·핫딜 URL."""
    ev = Evidence()
    for url in seed_urls:
        if url:
            ev.seen_keys.add(normalize_url(url))
    pending_fetch: dict[str, str] = {}
    text_parts: list[str] = []
    current_ref: str | None = None
    for block in _blocks(responses):
        btype = block.get("type")
        if btype == "server_tool_use":
            inp = block.get("input") or {}
            if block.get("name") == "web_search" and inp.get("query"):
                ev.queries.append(str(inp["query"]))
            elif block.get("name") == "web_fetch" and inp.get("url"):
                pending_fetch[str(block.get("id"))] = str(inp["url"])
        elif btype == "web_search_tool_result":
            content = block.get("content")
            if isinstance(content, dict):  # 오류 객체
                ev.search_errors.append(str(content.get("error_code")))
                continue
            for item in content or []:
                if isinstance(item, dict) and item.get("url"):
                    key = normalize_url(item["url"])
                    ev.search_hits[key] = SearchHit(item["url"], str(item.get("title") or ""), item.get("page_age"))
                    ev.seen_keys.add(key)
        elif btype == "web_fetch_tool_result":
            content = block.get("content") or {}
            if content.get("type") == "web_fetch_result":
                url = str(content.get("url") or "")
                doc = content.get("content") or {}
                key = normalize_url(url)
                ev.fetched[key] = FetchedDoc(url, str(doc.get("title") or ""), _doc_text(doc), content.get("retrieved_at"))
                ev.seen_keys.add(key)
            else:
                url = pending_fetch.get(str(block.get("tool_use_id")), "")
                ev.fetch_errors.append((url, str(content.get("error_code"))))
        elif btype == "text":
            text = str(block.get("text") or "")
            text_parts.append(text)
            refs = _REF_RE.findall(text)
            if refs:
                current_ref = refs[-1]
            for cite in block.get("citations") or []:
                if isinstance(cite, dict) and cite.get("url"):
                    ev.citations.append(Cite(cite["url"], str(cite.get("title") or ""),
                                             str(cite.get("cited_text") or ""), current_ref))
                    ev.seen_keys.add(normalize_url(cite["url"]))
    ev.report_text = "".join(text_parts)
    return ev


# ------------------------------------------------------------------ 보고서 줄 파싱 (교차 확인용)

_LINE_RE = re.compile(r"^\s*[-*•]?\s*([^:：\n]{1,20})\s*[:：]\s*(.+?)\s*$")


def parse_report_lines(report: str) -> dict[str, dict[str, str]]:
    """'[C1]' 블록의 '키: 값' 줄을 dict로. Phase B 결과와 URL·가격이 다르면 그 행을 버리는 데 쓴다."""
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in report.splitlines():
        refs = _REF_RE.findall(line)
        if refs:
            current = refs[0]
            out.setdefault(current, {})
            rest = _REF_RE.sub("", line).strip()
            if not rest:
                continue
            line = rest
        if current is None:
            continue
        m = _LINE_RE.match(line)
        if m:
            key = m.group(1).strip()
            out[current].setdefault(key, m.group(2).strip())
    return out


def report_line_price(fields: dict[str, str]) -> int | None:
    for key in ("가격", "판매가", "price"):
        if key in fields:
            krw, _ = extract_amounts(fields[key])
            return max(krw) if krw else None
    return None


def report_line_url(fields: dict[str, str]) -> str | None:
    for key in ("URL", "url", "링크"):
        if key in fields:
            m = re.search(r"https?://\S+", fields[key])
            if m:
                return m.group(0).rstrip(").,>]")
    return None


# ------------------------------------------------------------------ 검증 등급

LEVEL_ORDER = {"system": 0, "page": 1, "citation": 2, "stale": 3, "unverified": 4, "url_unseen": 5}
CONFIRMED_LEVELS = {"system", "page", "citation"}
STALE_DAYS = 3


def page_age_days(page_age: str | None, now: datetime) -> float | None:
    """검색 결과의 page_age('3 days ago', 'April 30, 2025', '2026-11-14' 등) → 경과 일수. 모르면 None."""
    if not page_age:
        return None
    text = page_age.strip()
    m = re.match(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return n * {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[unit]
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return (naive_now - dt).total_seconds() / 86400
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y.%m.%d", "%d %B %Y"):
        try:
            return (naive_now - datetime.strptime(text, fmt)).total_seconds() / 86400
        except ValueError:
            continue
    return None


@dataclass
class Verdict:
    level: str
    note: str
    flags: list[str] = field(default_factory=list)


def verify_candidate(cand: dict[str, Any], ev: Evidence, now: datetime | None = None) -> Verdict:
    now = now or datetime.now(timezone.utc)
    url = str(cand.get("url") or "")
    flags: list[str] = []
    if url and not is_known_shop(url):
        flags.append("unknown_shop")
    if _domain_in(url, COMMUNITY_DOMAINS):
        flags.append("community")
    if cand.get("origin") == "system":
        return Verdict("system", "우리 수집기가 직접 확인한 가격", flags)
    key = normalize_url(url)
    if not key or key not in ev.seen_keys:
        return Verdict("url_unseen", "조사 과정의 검색·페이지·인용 어디에도 없던 링크 (지어낸 링크일 수 있음)", flags)
    price = cand.get("price")
    amount, currency = cand.get("original_amount"), cand.get("original_currency")
    doc = ev.fetched.get(key)
    if doc and price_in_text(doc.text, price, amount, currency):
        if is_list_page(doc.url):
            flags.append("list_page")
            return Verdict("citation", "목록/검색 페이지에서 같은 금액 확인 (다른 상품 가격일 수 있음)", flags)
        return Verdict("page", f"가져온 페이지에서 같은 금액 확인 ({doc.retrieved_at or '시각 미상'})", flags)
    ref = cand.get("ref")
    for cite in ev.citations:
        cite_key = normalize_url(cite.url)
        same_url = cite_key == key
        if not same_url and (ref is None or cite.ref != ref):
            continue  # 같은 링크이거나, 이 후보 블록([C#]) 안에서 붙은 인용만 본다
        if price_in_text(cite.cited_text, price, amount, currency):
            hit = ev.search_hits.get(cite_key)
            age = page_age_days(hit.page_age if hit else None, now)
            if age is not None and age > STALE_DAYS:
                return Verdict("stale", f"검색 인용문에 금액이 있으나 {int(age)}일 전 정보", flags)
            where = "" if same_url else f" (출처: {_host(cite.url)})"
            return Verdict("citation", f"검색 결과 인용문에서 같은 금액 확인{where}: “{cite.cited_text[:80]}”", flags)
    if doc:
        return Verdict("unverified", "페이지는 가져왔지만 같은 금액을 찾지 못함", flags)
    return Verdict("unverified", "AI 보고만 있고 자동 확인 못 함 (차단된 사이트일 수 있음)", flags)


# ------------------------------------------------------------------ 실구매가 · 순위

def compute_landed(cand: dict[str, Any], usd_krw: float, category: str = "general") -> tuple[int, str]:
    price = int(cand.get("price") or 0)
    shipping = int(cand.get("shipping") if cand.get("shipping") is not None else -1)
    ship = max(shipping, 0)
    note = "배송비 미확인" if shipping < 0 else ""
    origin = str(cand.get("origin_country") or "").upper()
    overseas = bool(cand.get("is_overseas")) or (origin not in ("", "KR", "UNKNOWN"))
    if overseas and str(cand.get("tax_included") or "unknown") != "yes" and price > 0:
        est = estimate_import_tax(price, ship, usd_krw, origin="US" if origin == "US" else "CN", category=category)
        if est.taxable and est.total_tax:
            tax = f"관부가세 약 {est.total_tax:,}원 포함(${est.item_usd} > ${est.limit_usd})"
        elif est.taxable:
            tax = "과세 대상이나 세액 1만원 미만"
        else:
            tax = f"면세(${est.item_usd} ≤ ${est.limit_usd})"
        return est.landed_price, ", ".join(x for x in (note, tax) if x)
    return price + ship, note


@dataclass
class Ranking:
    best: dict[str, Any] | None
    confirmed: list[dict[str, Any]]
    cheaper_unconfirmed: list[dict[str, Any]]
    others: list[dict[str, Any]]
    conditional_best: dict[str, Any] | None
    baseline_median: float | None


def rank_candidates(cands: list[dict[str, Any]], usd_krw: float, category: str = "general") -> Ranking:
    """각 후보 dict에 landed_price/tax_note/flags를 채우고, 확인된 최저가와 경고 목록을 만든다."""
    for c in cands:
        c["landed_price"], c["tax_note"] = compute_landed(c, usd_krw, category)
        c.setdefault("flags", [])
    exact_system = [c["landed_price"] for c in cands
                    if c.get("origin") == "system" and c.get("match") == "exact" and c["landed_price"] > 0]
    median = statistics.median(exact_system) if exact_system else None
    for c in cands:
        if median and 0 < c["landed_price"] < 0.5 * median and "suspicious_low" not in c["flags"]:
            c["flags"].append("suspicious_low")

    def usable(c: dict[str, Any]) -> bool:
        return (int(c.get("price") or 0) > 0 and c.get("match") == "exact" and c.get("in_stock") != "no"
                and c.get("verification") in CONFIRMED_LEVELS and "suspicious_low" not in c["flags"])

    key = lambda c: (c["landed_price"], LEVEL_ORDER.get(c.get("verification", ""), 9))  # noqa: E731
    confirmed = sorted((c for c in cands if usable(c)), key=key)
    best = confirmed[0] if confirmed else None
    cheaper = sorted(
        (c for c in cands if not usable(c) and int(c.get("price") or 0) > 0 and c.get("match") in ("exact", "close")
         and (best is None or c["landed_price"] < best["landed_price"])),
        key=key,
    )
    chosen = {id(c) for c in confirmed} | {id(c) for c in cheaper}
    others = sorted((c for c in cands if id(c) not in chosen), key=key)
    cond = [c for c in confirmed if int(c.get("conditional_price") or 0) > 0]
    conditional_best = min(cond, key=lambda c: c["conditional_price"]) if cond else None
    return Ranking(best, confirmed, cheaper, others, conditional_best, median)


def kst_today(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone(timedelta(hours=9))).date().isoformat()

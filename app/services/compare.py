"""여러 쇼핑 소스를 병렬 조회해 총액 기준으로 비교한다. 알리는 관부가세 추정치를 더한 실구매가로 비교."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import httpx

from ..config import Settings, get_settings
from ..http import make_client
from ..sources.shopping import Offer, ProviderResult, search_aliexpress, search_danawa, search_naver
from .customs import estimate_import_tax

CACHE_TTL = 600
_cache: dict[tuple[str, str], tuple[float, "CompareResult"]] = {}


@dataclass
class CompareResult:
    query: str
    offers: list[Offer]
    providers: list[ProviderResult]
    cheapest: Offer | None = None
    cheapest_domestic: Offer | None = None
    cheapest_overseas: Offer | None = None
    cheapest_home: Offer | None = None
    fetched_at: float = field(default_factory=time.time)


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,/()\[\]+-]+", text.lower()) if len(t) >= 2]


def relevance(query: str, title: str) -> float:
    toks = _tokens(query)
    if not toks:
        return 1.0
    hay = re.sub(r"\s+", "", title.lower())
    return sum(1 for t in toks if t in hay) / len(toks)


def landed_price(offer: Offer) -> int:
    return int(offer.extra.get("landed_price", offer.total))


def rank_offers(query: str, offers: list[Offer], settings: Settings, category: str = "general",
                min_relevance: float = 0.5) -> list[Offer]:
    kept = []
    want_used = "중고" in query
    for o in offers:
        if not want_used and "중고" in o.title:
            continue  # 새 상품 기준 비교 (검색어에 '중고'가 있으면 포함)
        o.extra["relevance"] = round(relevance(query, o.title), 2)
        if o.extra["relevance"] < min_relevance:
            continue
        # 다나와 '해외구매' 가격은 판매처가 세금 포함/별도를 다르게 표기하므로 추정치를 더하지 않는다
        if o.is_overseas and o.provider != "danawa":
            est = estimate_import_tax(o.price, o.shipping or 0, settings.usd_krw, category=category)
            o.extra["tax"] = est
            o.extra["landed_price"] = est.landed_price
        else:
            o.extra["landed_price"] = o.total
        kept.append(o)
    kept.sort(key=landed_price)
    return kept


async def compare(query: str, category: str = "general", client: httpx.AsyncClient | None = None,
                  use_cache: bool = True) -> CompareResult:
    query = query.strip()
    key = (query.lower(), category)
    if use_cache and key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    settings = get_settings()
    own = client is None
    client = client or make_client()
    try:
        tasks = [search_naver(client, settings, query), search_danawa(client, settings, query),
                 search_aliexpress(client, settings, query)]
        names = [("naver", "네이버쇼핑"), ("danawa", "다나와"), ("aliexpress", "알리익스프레스")]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if own:
            await client.aclose()
    providers: list[ProviderResult] = []
    offers: list[Offer] = []
    for (pkey, pname), res in zip(names, raw):
        if isinstance(res, BaseException):
            providers.append(ProviderResult(pkey, pname, [], ok=False, message=f"오류: {type(res).__name__} {res}"[:200]))
            continue
        providers.append(res)
        offers.extend(res.offers)
    ranked = rank_offers(query, offers, settings, category)
    result = CompareResult(query=query, offers=ranked, providers=providers)
    result.cheapest = ranked[0] if ranked else None
    result.cheapest_domestic = next((o for o in ranked if not o.is_overseas), None)
    result.cheapest_overseas = next((o for o in ranked if o.is_overseas), None)
    result.cheapest_home = next((o for o in ranked if o.extra.get("home_shopping")), None)
    _cache[key] = (time.time(), result)
    return result

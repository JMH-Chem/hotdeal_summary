"""가격비교용 쇼핑 소스: 네이버 쇼핑 API, 다나와 검색, 알리익스프레스 Affiliate API."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Settings

ALI_GATEWAY = "https://api-sg.aliexpress.com/sync"

# 네이버쇼핑 결과의 mallName으로 TV홈쇼핑/T커머스 판매처를 식별한다.
HOME_SHOPPING_MALLS = re.compile(
    r"GS\s*SHOP|GS샵|GS홈쇼핑|CJ\s*온스타일|CJ\s*ON\s*STYLE|CJ오쇼핑|현대\s*H\s*mall|현대홈쇼핑|Hmall|롯데홈쇼핑|롯데아이몰|"
    r"NS\s*홈쇼핑|NS몰|홈앤쇼핑|공영쇼핑|공영홈쇼핑|KT\s*알파|K쇼핑|SK\s*스토아|신세계라이브쇼핑|신세계쇼핑|쇼핑엔티|W쇼핑|더블유쇼핑",
    re.I,
)


def is_home_shopping(mall: str | None) -> bool:
    return bool(mall and HOME_SHOPPING_MALLS.search(mall))


@dataclass
class Offer:
    provider: str          # naver | danawa | aliexpress
    provider_name: str
    title: str
    price: int             # KRW
    url: str
    mall: str | None = None
    image: str | None = None
    shipping: int | None = None
    is_overseas: bool = False
    product_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.price + (self.shipping or 0)


@dataclass
class ProviderResult:
    provider: str
    provider_name: str
    offers: list[Offer]
    ok: bool = True
    message: str = ""
    search_url: str | None = None  # 키가 없을 때 사용자에게 줄 직접 검색 링크


# ------------------------------------------------------------------ 네이버

_B_TAG = re.compile(r"</?b>")
_HTML_TAG = re.compile(r"<[^>]+>")


def parse_naver(payload: dict[str, Any]) -> list[Offer]:
    offers = []
    for it in payload.get("items", []):
        try:
            price = int(it.get("lprice") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        mall = it.get("mallName") or None
        offers.append(
            Offer(
                provider="naver",
                provider_name="네이버쇼핑",
                title=_B_TAG.sub("", it.get("title", "")),
                price=price,
                url=it.get("link", ""),
                mall=mall,
                image=it.get("image"),
                product_ref=str(it.get("productId") or ""),
                is_overseas=bool(mall and re.search(r"알리|aliexpress|해외", mall, re.I)),
                extra={"home_shopping": is_home_shopping(mall)},
            )
        )
    return offers


async def search_naver(client: httpx.AsyncClient, settings: Settings, query: str, display: int = 30) -> ProviderResult:
    link = f"https://search.shopping.naver.com/search/all?query={quote(query)}"
    if not settings.naver_enabled:
        return ProviderResult("naver", "네이버쇼핑", [], ok=False,
                              message="NAVER_CLIENT_ID/SECRET 미설정", search_url=link)
    resp = await client.get(
        "https://openapi.naver.com/v1/search/shop.json",
        params={"query": query, "display": display, "sort": "sim", "exclude": "used:rental"},
        headers={"X-Naver-Client-Id": settings.naver_client_id, "X-Naver-Client-Secret": settings.naver_client_secret},
    )
    resp.raise_for_status()
    return ProviderResult("naver", "네이버쇼핑", parse_naver(resp.json()), search_url=link)


# ------------------------------------------------------------------ 다나와

_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def parse_danawa(html: str) -> list[Offer]:
    """다나와 검색(Next.js RSC) 응답에 내장된 products 배열을 파싱."""
    chunks = _FLIGHT_RE.findall(html)
    if not chunks:
        return []
    flight = "".join(json.loads('"' + c + '"') for c in chunks)
    idx = flight.find('"products":[')
    if idx < 0:
        return []
    try:
        products, _ = json.JSONDecoder().raw_decode(flight[idx + len('"products":'):])
    except json.JSONDecodeError:
        return []
    offers = []
    for p in products:
        price = p.get("minPriceNumber") or p.get("minPrice") or 0
        try:
            price = int(price)
        except (TypeError, ValueError):
            continue
        if price <= 0 or not p.get("productName"):
            continue
        offers.append(
            Offer(
                provider="danawa",
                provider_name="다나와",
                title=_HTML_TAG.sub("", p["productName"]),
                price=price,
                url=p.get("linkUrl") or f"https://prod.danawa.com/info/?pcode={p.get('productSeq')}",
                mall=f"다나와 최저가({p.get('shopCount') or '?'}개 몰)",
                image=p.get("imageUrl"),
                product_ref=str(p.get("productSeq") or ""),
                extra={"maker": p.get("makerName")},
            )
        )
    return offers


async def search_danawa(client: httpx.AsyncClient, settings: Settings, query: str) -> ProviderResult:
    link = f"https://search.danawa.com/?query={quote(query)}"
    resp = await client.get(link)
    resp.raise_for_status()
    offers = parse_danawa(resp.text)
    return ProviderResult("danawa", "다나와", offers, ok=True,
                          message="" if offers else "결과 없음(구조 변경 가능)", search_url=link)


# ------------------------------------------------------------------ 알리익스프레스

def ali_sign(params: dict[str, str], secret: str) -> str:
    """AliExpress Open Platform(IOP) 비즈니스 API 서명: key+value 정렬 연결 후 HMAC-SHA256 대문자 hex."""
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def ali_build_params(settings: Settings, method: str, biz: dict[str, Any], timestamp_ms: int | None = None) -> dict[str, str]:
    params = {
        "app_key": settings.ali_app_key,
        "method": method,
        "sign_method": "sha256",
        "timestamp": str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)),
        **{k: str(v) for k, v in biz.items() if v not in (None, "")},
    }
    params["sign"] = ali_sign(params, settings.ali_app_secret)
    return params


def _ali_products(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key, body in payload.items():
        if key.endswith("_response") and isinstance(body, dict):
            result = (body.get("resp_result") or {}).get("result") or {}
            products = (result.get("products") or {}).get("product") or []
            return products if isinstance(products, list) else [products]
        if key == "error_response":
            raise RuntimeError(f"AliExpress API 오류: {body.get('msg') or body}")
    return []


def _to_krw(value: Any, currency: str | None, usd_krw: float) -> int | None:
    try:
        num = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if currency and currency.upper() == "USD":
        num *= usd_krw
    return int(round(num))


def parse_ali_products(payload: dict[str, Any], usd_krw: float) -> list[Offer]:
    offers = []
    for p in _ali_products(payload):
        currency = p.get("target_sale_price_currency") or p.get("sale_price_currency") or "KRW"
        price = _to_krw(p.get("target_sale_price") or p.get("sale_price"), currency, usd_krw)
        if not price:
            continue
        original = _to_krw(p.get("target_original_price") or p.get("original_price"), currency, usd_krw)
        offers.append(
            Offer(
                provider="aliexpress",
                provider_name="알리익스프레스",
                title=p.get("product_title", ""),
                price=price,
                url=p.get("promotion_link") or p.get("product_detail_url") or "",
                mall=p.get("shop_name") or "AliExpress",
                image=p.get("product_main_image_url"),
                is_overseas=True,
                product_ref=str(p.get("product_id") or ""),
                extra={
                    "original_price": original,
                    "discount": p.get("discount"),
                    "rating": p.get("evaluate_rate"),
                    "sold": p.get("lastest_volume"),
                },
            )
        )
    return offers


async def ali_call(client: httpx.AsyncClient, settings: Settings, method: str, biz: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(ALI_GATEWAY, data=ali_build_params(settings, method, biz))
    resp.raise_for_status()
    return resp.json()


async def search_aliexpress(client: httpx.AsyncClient, settings: Settings, query: str, page_size: int = 20) -> ProviderResult:
    link = f"https://ko.aliexpress.com/w/wholesale-{quote(query)}.html"
    if not settings.ali_enabled:
        return ProviderResult("aliexpress", "알리익스프레스", [], ok=False,
                              message="ALI_APP_KEY/SECRET 미설정 (Affiliate API 키 필요)", search_url=link)
    payload = await ali_call(client, settings, "aliexpress.affiliate.product.query", {
        "keywords": query,
        "target_currency": "KRW",
        "target_language": "KO",
        "ship_to_country": "KR",
        "page_size": page_size,
        "sort": "SALE_PRICE_ASC",
        "tracking_id": settings.ali_tracking_id,
    })
    return ProviderResult("aliexpress", "알리익스프레스", parse_ali_products(payload, settings.usd_krw), search_url=link)


async def aliexpress_product(client: httpx.AsyncClient, settings: Settings, product_id: str) -> Offer | None:
    if not settings.ali_enabled:
        raise RuntimeError("ALI_APP_KEY/SECRET 미설정")
    payload = await ali_call(client, settings, "aliexpress.affiliate.productdetail.get", {
        "product_ids": product_id,
        "target_currency": "KRW",
        "target_language": "KO",
        "country": "KR",
        "tracking_id": settings.ali_tracking_id,
    })
    offers = parse_ali_products(payload, settings.usd_krw)
    return offers[0] if offers else None


ALI_ITEM_RE = re.compile(r"aliexpress\.[a-z.]+/item/(\d+)\.html", re.I)


def aliexpress_product_id(url_or_id: str) -> str | None:
    text = url_or_id.strip()
    if text.isdigit():
        return text
    m = ALI_ITEM_RE.search(text)
    return m.group(1) if m else None

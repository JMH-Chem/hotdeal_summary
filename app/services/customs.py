"""해외직구 관부가세 간이 추정 (참고용).

- 면세 한도: 물품가격 USD 150 이하(미국발 목록통관 USD 200).
- 과세 시: 과세가격 = 물품가 + 국제배송비, 관세 = 과세가격 × 관세율, 부가세 = (과세가격 + 관세) × 10%.
- 세액 합계 1만원 미만은 징수하지 않음(관세법 징수금액 최저한).
- 실제 세액은 품목분류·합산과세(같은 날 입항)·환율 고시값에 따라 달라진다.
"""

from __future__ import annotations

from dataclasses import dataclass

DUTY_RATES = {
    "general": (0.08, "일반(8%)"),
    "electronics": (0.0, "IT/전자기기(0%, ITA)"),
    "clothing": (0.13, "의류·신발(13%)"),
    "cosmetics": (0.065, "화장품(6.5%)"),
    "food": (0.08, "식품(8%, 품목별 상이)"),
}
VAT_RATE = 0.10
MIN_COLLECT_KRW = 10_000


@dataclass
class TaxEstimate:
    item_usd: float
    limit_usd: int
    taxable: bool
    duty: int
    vat: int
    total_tax: int
    landed_price: int
    category_label: str


def _round10(value: float) -> int:
    return int(value // 10 * 10)


def estimate_import_tax(price_krw: int, shipping_krw: int = 0, usd_krw: float = 1400.0,
                        origin: str = "CN", category: str = "general") -> TaxEstimate:
    rate, label = DUTY_RATES.get(category, DUTY_RATES["general"])
    limit_usd = 200 if origin.upper() == "US" else 150
    item_usd = price_krw / usd_krw if usd_krw else 0.0
    base = price_krw + shipping_krw
    if item_usd <= limit_usd:
        return TaxEstimate(round(item_usd, 2), limit_usd, False, 0, 0, 0, base, label)
    taxable_value = price_krw + shipping_krw
    duty = _round10(taxable_value * rate)
    vat = _round10((taxable_value + duty) * VAT_RATE)
    total = duty + vat
    if total < MIN_COLLECT_KRW:
        return TaxEstimate(round(item_usd, 2), limit_usd, True, 0, 0, 0, base, label)
    return TaxEstimate(round(item_usd, 2), limit_usd, True, duty, vat, total, base + total, label)

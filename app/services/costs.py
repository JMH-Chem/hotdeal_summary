"""Claude API 사용량 → 비용(USD) 추정. 토큰 단가는 2026-11 기준 공개 요금(변경 시 이 표만 고치면 됨)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 모델: (입력, 출력, 캐시 읽기, 캐시 쓰기 5분) — USD / 100만 토큰
MODEL_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-2": (8.0, 40.0, 0.20, 10.0),
    "claude-fable-5-1": (10.0, 50.0, 1.0, 12.5),
}
DEFAULT_MODEL = "claude-opus-5-5"
WEB_SEARCH_USD = 0.01  # 1,000회당 $10 (웹 페치는 토큰 외 추가 요금 없음)


def _tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    return (int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
            int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0))


def token_cost(model: str | None, usage: dict[str, Any]) -> float:
    inp, out, c_read, c_write = _tokens(usage)
    p_in, p_out, p_read, p_write = MODEL_PRICES.get(model or DEFAULT_MODEL, MODEL_PRICES[DEFAULT_MODEL])
    return (inp * p_in + out * p_out + c_read * p_read + c_write * p_write) / 1_000_000


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    cost_usd: float = 0.0
    responses: int = 0

    def add(self, response: dict[str, Any], default_model: str = DEFAULT_MODEL) -> float:
        """응답 1개의 사용량을 더하고 그 응답의 비용을 돌려준다. 이어가기 응답은 각자 자기 몫만 보고한다."""
        usage = response.get("usage") or {}
        iterations = [it for it in (usage.get("iterations") or []) if isinstance(it, dict)]
        priced = [it for it in iterations if any(k in it for k in ("input_tokens", "output_tokens"))]
        if priced:  # 거절 후 대체 모델(fallback)이 이어받으면 시도별 모델 단가로 계산
            cost = sum(token_cost(it.get("model") or response.get("model") or default_model, it) for it in priced)
            sums = [_tokens(it) for it in priced]
            inp, out, c_read, c_write = (sum(x[i] for x in sums) for i in range(4))
        else:
            cost = token_cost(response.get("model") or default_model, usage)
            inp, out, c_read, c_write = _tokens(usage)
        stu = usage.get("server_tool_use") or {}
        searches = int(stu.get("web_search_requests") or 0)
        fetches = int(stu.get("web_fetch_requests") or 0)
        cost += searches * WEB_SEARCH_USD
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += c_read
        self.cache_write += c_write
        self.web_searches += searches
        self.web_fetches += fetches
        self.cost_usd += cost
        self.responses += 1
        return cost

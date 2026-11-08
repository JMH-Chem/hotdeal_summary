"""환경변수 기반 설정. 매 호출마다 읽어서 테스트/런타임에서 쉽게 바꿀 수 있게 한다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    db_path: str
    collect_interval_sec: int
    price_check_interval_sec: int
    enable_scheduler: bool
    user_agent: str
    enabled_sources: tuple[str, ...]  # 비어 있으면 기본값(enabled_by_default) 사용
    naver_client_id: str
    naver_client_secret: str
    ali_app_key: str
    ali_app_secret: str
    ali_tracking_id: str
    telegram_bot_token: str
    telegram_chat_id: str
    discord_webhook_url: str
    usd_krw: float

    @property
    def naver_enabled(self) -> bool:
        return bool(self.naver_client_id and self.naver_client_secret)

    @property
    def ali_enabled(self) -> bool:
        return bool(self.ali_app_key and self.ali_app_secret)


def get_settings() -> Settings:
    sources = tuple(s.strip() for s in _env("ENABLED_SOURCES").split(",") if s.strip())
    return Settings(
        db_path=_env("HOTDEAL_DB", str(DATA_DIR / "hotdeal.db")),
        collect_interval_sec=int(_env("COLLECT_INTERVAL_SEC", "300")),
        price_check_interval_sec=int(_env("PRICE_CHECK_INTERVAL_SEC", "3600")),
        enable_scheduler=_env("ENABLE_SCHEDULER", "1") == "1",
        user_agent=_env("HTTP_USER_AGENT", DEFAULT_UA),
        enabled_sources=sources,
        naver_client_id=_env("NAVER_CLIENT_ID"),
        naver_client_secret=_env("NAVER_CLIENT_SECRET"),
        ali_app_key=_env("ALI_APP_KEY"),
        ali_app_secret=_env("ALI_APP_SECRET"),
        ali_tracking_id=_env("ALI_TRACKING_ID"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
        usd_krw=float(_env("USD_KRW", "1400")),
    )

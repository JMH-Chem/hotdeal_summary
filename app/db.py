"""SQLite 스키마와 헬퍼."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_name TEXT,
  board TEXT,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  mall TEXT,
  price INTEGER,
  currency TEXT,
  shipping TEXT,
  category TEXT,
  author TEXT,
  summary TEXT,
  thumbnail TEXT,
  views INTEGER,
  posted_at TEXT,
  collected_at TEXT NOT NULL,
  dedupe_key TEXT,
  is_aliexpress INTEGER DEFAULT 0,
  is_coupon INTEGER DEFAULT 0,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_deals_posted ON deals(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_dedupe ON deals(dedupe_key);

CREATE TABLE IF NOT EXISTS coupons (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  code TEXT NOT NULL,
  discount_amount INTEGER,
  discount_percent INTEGER,
  min_spend INTEGER,
  currency TEXT,
  valid_until TEXT,
  source_name TEXT,
  source_url TEXT,
  context TEXT,
  event_slug TEXT,
  first_seen TEXT,
  last_seen TEXT,
  UNIQUE(platform, code)
);

CREATE TABLE IF NOT EXISTS tracked_items (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- 'query' | 'aliexpress'
  query TEXT,
  product_ref TEXT,
  url TEXT,
  target_price INTEGER,
  drop_percent REAL,
  last_price INTEGER,
  last_mall TEXT,
  lowest_price INTEGER,
  highest_price INTEGER,
  last_checked TEXT,
  created_at TEXT NOT NULL,
  active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES tracked_items(id) ON DELETE CASCADE,
  price INTEGER NOT NULL,
  mall TEXT,
  provider TEXT,
  checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_item ON price_history(item_id, checked_at);

CREATE TABLE IF NOT EXISTS keyword_alerts (
  id INTEGER PRIMARY KEY,
  keyword TEXT NOT NULL UNIQUE,
  max_price INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  url TEXT,
  created_at TEXT NOT NULL,
  is_read INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY,
  last_run TEXT,
  ok INTEGER,
  fetched INTEGER,
  inserted INTEGER,
  message TEXT
);

-- AI 최저가 탐색: 실행 1회 = 한 상품의 한 날짜 조사
CREATE TABLE IF NOT EXISTS research_runs (
  id INTEGER PRIMARY KEY,
  query TEXT NOT NULL,
  query_key TEXT NOT NULL,         -- 정규화된 질의 (같은 날 중복 실행 방지)
  options TEXT,                    -- JSON
  status TEXT NOT NULL,            -- queued | running | done | error | interrupted
  run_date TEXT NOT NULL,          -- KST 날짜 YYYY-MM-DD
  model TEXT,
  item_id INTEGER REFERENCES tracked_items(id) ON DELETE SET NULL,
  reserved_usd REAL DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  web_searches INTEGER DEFAULT 0,
  web_fetches INTEGER DEFAULT 0,
  best_price INTEGER,              -- 확인된(검증된) 오늘의 최저 실구매가
  best_candidate_id INTEGER,
  summary TEXT,
  report TEXT,                     -- 1단계 조사 보고서 원문 (투명성)
  diagnostics TEXT,                -- JSON: 출처 수, 가져오기 실패, 인용 수 등
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_date ON research_runs(run_date, query_key);
CREATE INDEX IF NOT EXISTS idx_research_item ON research_runs(item_id, run_date);

CREATE TABLE IF NOT EXISTS research_candidates (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  ref TEXT,                        -- C1.. (AI) / S1.. (우리 수집기)
  origin TEXT NOT NULL,            -- ai | system
  seller TEXT,
  channel TEXT,
  title TEXT,
  url TEXT,
  price INTEGER,
  shipping INTEGER,                -- -1 = 미확인
  conditional_price INTEGER,
  conditions TEXT,
  original_currency TEXT,
  original_amount REAL,
  is_overseas INTEGER DEFAULT 0,
  origin_country TEXT,
  landed_price INTEGER,
  tax_note TEXT,
  match TEXT,                      -- exact | close | different
  in_stock TEXT,                   -- yes | no | unknown
  time_limited TEXT,
  evidence TEXT,
  verification TEXT,               -- system | page | citation | stale | unverified | url_unseen
  verify_note TEXT,
  flags TEXT,                      -- 쉼표 구분: suspicious_low, unknown_shop, list_page ...
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON research_candidates(run_id);

CREATE TABLE IF NOT EXISTS research_sources (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  title TEXT,
  kind TEXT                        -- search | fetch | citation
);
CREATE INDEX IF NOT EXISTS idx_sources_run ON research_sources(run_id);
"""

SCHEMA_VERSION = 2


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_settings().db_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def migrate(conn: sqlite3.Connection) -> None:
    """기존 DB를 현재 스키마로 올린다 (CREATE IF NOT EXISTS로 안 되는 컬럼 추가 등)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        _ensure_column(conn, "tracked_items", "ai_daily", "INTEGER DEFAULT 0")
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()

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
"""


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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()

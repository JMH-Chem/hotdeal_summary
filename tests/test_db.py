import sqlite3

from app.db import SCHEMA_VERSION, connect, init_db


def test_migrate_adds_ai_daily_to_old_db(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE tracked_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
             query TEXT, product_ref TEXT, url TEXT, target_price INTEGER, drop_percent REAL, last_price INTEGER,
             last_mall TEXT, lowest_price INTEGER, highest_price INTEGER, last_checked TEXT,
             created_at TEXT NOT NULL, active INTEGER DEFAULT 1);
           INSERT INTO tracked_items (name, kind, created_at) VALUES ('x', 'query', '2026-11-01');"""
    )
    old.commit()
    old.close()

    conn = connect(str(path))
    init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked_items)")}
    assert "ai_daily" in cols
    assert conn.execute("SELECT ai_daily FROM tracked_items").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"research_runs", "research_candidates", "research_sources"} <= tables
    init_db(conn)  # 두 번 실행해도 안전

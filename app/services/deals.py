"""핫딜 조회 (웹 화면·API·AI 조사가 함께 쓰는 쿼리)."""

from __future__ import annotations

import sqlite3

PAGE_SIZE = 40


def query_deals(conn: sqlite3.Connection, q: str = "", source: str = "", only_ali: bool = False,
                only_coupon: bool = False, page: int = 1, dedupe: bool = True, sort: str = "new",
                limit: int = PAGE_SIZE) -> list[sqlite3.Row]:
    where, params = [], []
    if q:
        for tok in q.split():
            where.append("title LIKE ?")
            params.append(f"%{tok}%")
    if source:
        where.append("source_name = ?")
        params.append(source)
    if only_ali:
        where.append("is_aliexpress = 1")
    if only_coupon:
        where.append("is_coupon = 1")
    sql = "SELECT * FROM deals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if dedupe:
        sql = f"SELECT * FROM ({sql}) GROUP BY COALESCE(dedupe_key, id)"
    order = "COALESCE(views, 0) DESC, posted_at DESC" if sort == "views" else "posted_at DESC"
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    params += [limit, (page - 1) * limit]
    return conn.execute(sql, params).fetchall()

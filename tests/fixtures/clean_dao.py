"""干净的夹具：不应命中任何慢查规则。"""
import sqlite3


def list_orders(conn: sqlite3.Connection, limit: int = 20) -> list[tuple]:
    cur = conn.execute(
        "SELECT id, status, created_at FROM orders WHERE status = ? LIMIT ?",
        ("PAID", limit),
    )
    return cur.fetchall()

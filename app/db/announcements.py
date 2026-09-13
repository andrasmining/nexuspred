"""Broadcaster announcements (alpha.98) and the publisher's subscriber directory."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .core import _connect, _now, init


def _schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publisher_area_id INTEGER NOT NULL,
            listing_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            recipients INTEGER NOT NULL DEFAULT 0,
            mailed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ann_pub ON announcements(publisher_area_id, id);
        """
    )


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "listing_key": r["listing_key"], "title": r["title"], "body": r["body"], "recipients": r["recipients"],
            "mailed": r["mailed"], "created_at": r["created_at"]}


def add_announcement(publisher_area_id: int, listing_key: str, title: str, body: str, recipients: int, mailed: int) -> dict[str, Any]:
    init()
    now = _now()
    with _connect() as c:
        cur = c.execute("INSERT INTO announcements(publisher_area_id,listing_key,title,body,recipients,mailed,created_at) VALUES(?,?,?,?,?,?,?)",
                        (publisher_area_id, listing_key, title, body, recipients, mailed, now))
        return {"id": int(cur.lastrowid), "listing_key": listing_key, "title": title, "body": body, "recipients": recipients, "mailed": mailed, "created_at": now}


def list_announcements(publisher_area_id: int, limit: int = 50) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM announcements WHERE publisher_area_id=? ORDER BY id DESC LIMIT ?", (publisher_area_id, max(1, min(int(limit), 500)))).fetchall()
    return [_row(r) for r in rows]


def announcements_today(publisher_area_id: int) -> int:
    init()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM announcements WHERE publisher_area_id=? AND substr(created_at,1,10)=?", (publisher_area_id, day)).fetchone()[0])


def publisher_subscribers(publisher_area_id: int, listing_key: Optional[str] = None) -> list[dict[str, Any]]:
    """Distinct subscriber workspaces (area, owner user, email) of a publisher's
    enabled subscriptions — of one listing, or of all."""
    init()
    q = ("SELECT DISTINCT s.area_id AS area_id, u.id AS user_id, u.email AS email FROM subscriptions s "
         "JOIN areas a ON a.id = s.area_id JOIN users u ON u.id = a.owner_user_id WHERE s.publisher_area_id=? AND s.enabled=1")
    args: list[Any] = [publisher_area_id]
    if listing_key:
        q += " AND s.webhook_id=?"
        args.append(listing_key)
    with _connect() as c:
        rows = c.execute(q + " ORDER BY u.email", args).fetchall()
    return [{"area_id": r["area_id"], "user_id": r["user_id"], "email": r["email"]} for r in rows]

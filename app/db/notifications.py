"""Notification inbox (alpha.97): every alert as a row per workspace, read or
unread, with a deep link — so a user who has push and mail off still misses
nothing, and an admin sees platform events (role requests, backup alarms,
health changes) next to their own trading alerts."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .core import _connect, _now, init

KEEP_DAYS = 30
MAX_PER_AREA = 500


def _schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            read_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notif_area ON notifications(area_id, id);
        CREATE INDEX IF NOT EXISTS idx_notif_unread ON notifications(area_id, read_at);
        """
    )


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "kind": r["kind"], "severity": r["severity"], "title": r["title"], "message": r["message"],
            "url": r["url"], "created_at": r["created_at"], "read_at": r["read_at"], "read": r["read_at"] is not None}


def add_notification(area_id: int, kind: str, severity: str, title: str, message: str = "", url: str = "") -> dict[str, Any]:
    init()
    now = _now()
    with _connect() as c:
        cur = c.execute("INSERT INTO notifications(area_id,kind,severity,title,message,url,created_at) VALUES(?,?,?,?,?,?,?)",
                        (area_id, kind[:60], severity if severity in ("info", "warn", "critical") else "info", title[:160], message[:1000], url[:200], now))
        nid = int(cur.lastrowid)
        # bounded per workspace: the oldest read rows go first
        n = c.execute("SELECT COUNT(*) FROM notifications WHERE area_id=?", (area_id,)).fetchone()[0]
        if n > MAX_PER_AREA:
            c.execute("DELETE FROM notifications WHERE area_id=? AND id IN (SELECT id FROM notifications WHERE area_id=? ORDER BY (read_at IS NULL), id LIMIT ?)",
                      (area_id, area_id, n - MAX_PER_AREA))
    return {"id": nid, "kind": kind, "severity": severity, "title": title, "message": message, "url": url, "created_at": now, "read_at": None, "read": False}


def list_notifications(area_id: int, *, unread_only: bool = False, limit: int = 50, before_id: Optional[int] = None) -> list[dict[str, Any]]:
    init()
    q = "SELECT * FROM notifications WHERE area_id=?"
    args: list[Any] = [area_id]
    if unread_only:
        q += " AND read_at IS NULL"
    if before_id:
        q += " AND id<?"
        args.append(before_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _connect() as c:
        return [_row(r) for r in c.execute(q, args).fetchall()]


def unread_count(area_id: int) -> int:
    init()
    with _connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM notifications WHERE area_id=? AND read_at IS NULL", (area_id,)).fetchone()[0])


def mark_read(area_id: int, ids: Optional[list[int]] = None) -> int:
    """Mark the given ids (or every unread row of the workspace) read."""
    init()
    now = _now()
    with _connect() as c:
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = c.execute(f"UPDATE notifications SET read_at=? WHERE area_id=? AND read_at IS NULL AND id IN ({marks})", (now, area_id, *[int(i) for i in ids]))
        else:
            cur = c.execute("UPDATE notifications SET read_at=? WHERE area_id=? AND read_at IS NULL", (now, area_id))
        return int(cur.rowcount)


def prune_notifications(days: int = KEEP_DAYS) -> int:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as c:
        return int(c.execute("DELETE FROM notifications WHERE created_at<?", (cutoff,)).rowcount)

"""Mail outbox and alert-delivery log (alpha.95).

* **outbox** — every platform e-mail as a row: queued, delivered by the
  worker in :mod:`app.mailer` with backoff, kept (without the body) as the
  admin's mail log. A reset link, an invite or a role notice never vanishes
  silently again: it is ``pending``, ``sent`` or ``failed`` with the error.
* **alert_deliveries** — one row per alert delivery attempt per channel
  (push, e-mail, Discord) so Settings → Alerts can show "last delivered 4 min
  ago" and mark a channel that keeps failing.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .core import _connect, _now, init

OUTBOX_KEEP_DAYS = 30
DELIVERIES_KEEP_DAYS = 14
DEGRADED_AFTER = 3            # consecutive failures that mark an alert channel as degraded


def _schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_addr TEXT NOT NULL,
            subject TEXT NOT NULL,
            html TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            area_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_at);
        CREATE TABLE IF NOT EXISTS alert_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_area ON alert_deliveries(area_id, channel, id);
        """
    )


# ------------------------------------------------------------------ outbox
def _row(r: sqlite3.Row, body: bool = False) -> dict[str, Any]:
    d = {"id": r["id"], "to": r["to_addr"], "subject": r["subject"], "kind": r["kind"], "area_id": r["area_id"],
         "status": r["status"], "attempts": r["attempts"], "next_at": r["next_at"], "last_error": r["last_error"],
         "route": r["route"], "created_at": r["created_at"], "sent_at": r["sent_at"]}
    if body:
        d["html"] = r["html"]
        d["text"] = r["text"]
    return d


def outbox_add(to_addr: str, subject: str, html: str, text: str, kind: str = "", area_id: Optional[int] = None) -> int:
    init()
    now = _now()
    with _connect() as c:
        cur = c.execute("INSERT INTO outbox(to_addr,subject,html,text,kind,area_id,next_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (to_addr, subject, html, text, kind, area_id, now, now))
        return int(cur.lastrowid)


def outbox_due(limit: int = 20) -> list[dict[str, Any]]:
    """Pending rows whose retry time has come, oldest first, with bodies."""
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM outbox WHERE status='pending' AND next_at<=? ORDER BY id LIMIT ?", (_now(), limit)).fetchall()
    return [_row(r, body=True) for r in rows]


def outbox_get(row_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()
    return _row(r, body=True) if r else None


def outbox_sent(row_id: int, route: str) -> None:
    init()
    with _connect() as c:
        c.execute("UPDATE outbox SET status='sent', attempts=attempts+1, sent_at=?, route=?, last_error='' WHERE id=?", (_now(), route, row_id))


def outbox_failed(row_id: int, error: str, retry_in_s: Optional[float]) -> None:
    """Record a failed attempt; ``retry_in_s`` None = give up (status ``failed``)."""
    init()
    with _connect() as c:
        if retry_in_s is None:
            c.execute("UPDATE outbox SET status='failed', attempts=attempts+1, last_error=? WHERE id=?", (error[:500], row_id))
        else:
            nxt = (datetime.now(timezone.utc) + timedelta(seconds=retry_in_s)).isoformat()
            c.execute("UPDATE outbox SET attempts=attempts+1, last_error=?, next_at=? WHERE id=?", (error[:500], nxt, row_id))


def outbox_retry(row_id: int) -> bool:
    """Admin: put a failed row back in the queue (attempt counter kept)."""
    init()
    with _connect() as c:
        cur = c.execute("UPDATE outbox SET status='pending', next_at=? WHERE id=? AND status='failed'", (_now(), row_id))
        return cur.rowcount > 0


def outbox_list(limit: int = 100, status: Optional[str] = None) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        if status:
            rows = c.execute("SELECT * FROM outbox WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def outbox_counts() -> dict[str, int]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status").fetchall()
    out = {"pending": 0, "sent": 0, "failed": 0}
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    return out


def outbox_prune(days: int = OUTBOX_KEEP_DAYS) -> int:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as c:
        cur = c.execute("DELETE FROM outbox WHERE status!='pending' AND created_at<?", (cutoff,))
        n = cur.rowcount
        # bodies of sent mail are not needed after a day: keep the log small
        day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        c.execute("UPDATE outbox SET html='', text='' WHERE status='sent' AND html!='' AND sent_at<?", (day,))
    return int(n)


# ------------------------------------------------------- alert deliveries
def record_delivery(area_id: int, channel: str, ok: bool, title: str = "", error: str = "") -> None:
    init()
    with _connect() as c:
        c.execute("INSERT INTO alert_deliveries(area_id,channel,title,ok,error,created_at) VALUES(?,?,?,?,?,?)",
                  (area_id, channel, title[:120], 1 if ok else 0, error[:300], _now()))


def delivery_status(area_id: int) -> dict[str, dict[str, Any]]:
    """Per channel: last successful delivery, last error, consecutive failures, degraded flag."""
    init()
    out: dict[str, dict[str, Any]] = {}
    with _connect() as c:
        for ch in ("push", "email", "discord"):
            rows = c.execute("SELECT ok, error, created_at, title FROM alert_deliveries WHERE area_id=? AND channel=? ORDER BY id DESC LIMIT 20",
                             (area_id, ch)).fetchall()
            last_ok = next((r["created_at"] for r in rows if r["ok"]), None)
            last_error = next(({"at": r["created_at"], "error": r["error"], "title": r["title"]} for r in rows if not r["ok"]), None)
            streak = 0
            for r in rows:
                if r["ok"]:
                    break
                streak += 1
            out[ch] = {"last_ok": last_ok, "last_error": last_error, "failures": streak,
                       "degraded": streak >= DEGRADED_AFTER, "attempts": len(rows)}
    return out


def recent_deliveries(area_id: int, limit: int = 50) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM alert_deliveries WHERE area_id=? ORDER BY id DESC LIMIT ?", (area_id, limit)).fetchall()
    return [{"id": r["id"], "channel": r["channel"], "title": r["title"], "ok": bool(r["ok"]), "error": r["error"], "at": r["created_at"]} for r in rows]


def prune_deliveries(days: int = DELIVERIES_KEEP_DAYS) -> int:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as c:
        return int(c.execute("DELETE FROM alert_deliveries WHERE created_at<?", (cutoff,)).rowcount)

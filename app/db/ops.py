"""Operations tables (alpha.99): escalations of critical alerts and the
per-workspace settings history."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .core import _connect, _now, init

HISTORY_KEEP = 30


def _schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            stage INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            acked_at TEXT,
            acked_by TEXT NOT NULL DEFAULT '',
            last_step_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_esc_open ON escalations(acked_at, created_at);
        CREATE TABLE IF NOT EXISTS settings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            keys TEXT NOT NULL DEFAULT '',
            snapshot TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sh_area ON settings_history(area_id, id);
        """
    )


# ------------------------------------------------------------------ escalations
def _esc(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "area_id": r["area_id"], "kind": r["kind"], "title": r["title"], "message": r["message"], "url": r["url"],
            "stage": r["stage"], "created_at": r["created_at"], "acked_at": r["acked_at"], "acked_by": r["acked_by"], "last_step_at": r["last_step_at"]}


def add_escalation(area_id: int, kind: str, title: str, message: str, url: str) -> dict[str, Any]:
    init()
    now = _now()
    with _connect() as c:
        cur = c.execute("INSERT INTO escalations(area_id,kind,title,message,url,created_at,last_step_at) VALUES(?,?,?,?,?,?,?)",
                        (area_id, kind[:60], title[:160], message[:1000], url[:200], now, now))
        return get_escalation(int(cur.lastrowid))


def get_escalation(esc_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM escalations WHERE id=?", (esc_id,)).fetchone()
    return _esc(r) if r else None


def open_escalations(area_id: Optional[int] = None) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        if area_id is None:
            rows = c.execute("SELECT * FROM escalations WHERE acked_at IS NULL ORDER BY id").fetchall()
        else:
            rows = c.execute("SELECT * FROM escalations WHERE acked_at IS NULL AND area_id=? ORDER BY id", (area_id,)).fetchall()
    return [_esc(r) for r in rows]


def list_escalations(area_id: int, limit: int = 50) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM escalations WHERE area_id=? ORDER BY id DESC LIMIT ?", (area_id, max(1, min(int(limit), 500)))).fetchall()
    return [_esc(r) for r in rows]


def step_escalation(esc_id: int, stage: int) -> None:
    init()
    with _connect() as c:
        c.execute("UPDATE escalations SET stage=?, last_step_at=? WHERE id=?", (stage, _now(), esc_id))


def ack_escalation(esc_id: int, by: str, area_id: Optional[int] = None) -> bool:
    init()
    with _connect() as c:
        if area_id is None:
            cur = c.execute("UPDATE escalations SET acked_at=?, acked_by=? WHERE id=? AND acked_at IS NULL", (_now(), by[:120], esc_id))
        else:
            cur = c.execute("UPDATE escalations SET acked_at=?, acked_by=? WHERE id=? AND acked_at IS NULL AND area_id=?", (_now(), by[:120], esc_id, area_id))
        return cur.rowcount > 0


def prune_escalations(days: int = 30) -> int:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as c:
        return int(c.execute("DELETE FROM escalations WHERE created_at<? AND acked_at IS NOT NULL", (cutoff,)).rowcount)


# ------------------------------------------------------------------ settings history
def add_settings_version(area_id: int, actor: str, keys: list[str], snapshot: str) -> int:
    init()
    with _connect() as c:
        cur = c.execute("INSERT INTO settings_history(area_id,ts,actor,keys,snapshot) VALUES(?,?,?,?,?)",
                        (area_id, _now(), actor[:120], ",".join(keys)[:1000], snapshot))
        n = c.execute("SELECT COUNT(*) FROM settings_history WHERE area_id=?", (area_id,)).fetchone()[0]
        if n > HISTORY_KEEP:
            c.execute("DELETE FROM settings_history WHERE area_id=? AND id IN (SELECT id FROM settings_history WHERE area_id=? ORDER BY id LIMIT ?)",
                      (area_id, area_id, n - HISTORY_KEEP))
        return int(cur.lastrowid)


def last_settings_snapshot(area_id: int) -> Optional[str]:
    init()
    with _connect() as c:
        r = c.execute("SELECT snapshot FROM settings_history WHERE area_id=? ORDER BY id DESC LIMIT 1", (area_id,)).fetchone()
    return r["snapshot"] if r else None


def list_settings_versions(area_id: int, limit: int = HISTORY_KEEP) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT id, ts, actor, keys FROM settings_history WHERE area_id=? ORDER BY id DESC LIMIT ?", (area_id, limit)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "keys": [k for k in r["keys"].split(",") if k]} for r in rows]


def get_settings_version(area_id: int, version_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM settings_history WHERE area_id=? AND id=?", (area_id, version_id)).fetchone()
    return {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "keys": [k for k in r["keys"].split(",") if k], "snapshot": r["snapshot"]} if r else None

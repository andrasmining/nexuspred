"""Durable manual-command inbox. No dispatcher, polling loop or automatic retry.

The request identity is claimed before any broker mutation. Each write uses a
short independent FULL-synchronous transaction; the legacy history writer's
batch/NORMAL connection cannot weaken the pre-dispatch commit.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from . import core


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    core.init()
    c = sqlite3.connect(str(core.DB_FILE), timeout=2.0)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        with c:
            yield c
    finally:
        c.close()


def _scope(area_id: int, command_id: str) -> None:
    if type(area_id) is not int or area_id <= 0 or not isinstance(command_id, str) or not command_id:
        raise ValueError("Explicit workspace and command id required")


def claim(area_id: int, command_id: str, actor_user_id: int, request_hash: str, request_json: str) -> tuple[bool, dict[str, Any]]:
    _scope(area_id, command_id)
    with _connection() as c:
        c.execute("BEGIN IMMEDIATE")
        now = core._now()
        cur = c.execute(
            "INSERT INTO execution_commands(area_id,command_id,actor_user_id,request_hash,request_json,outcome,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'claimed',?,?) ON CONFLICT(area_id,command_id) DO NOTHING",
            (area_id, command_id, actor_user_id, request_hash, request_json, now, now),
        )
        row = c.execute("SELECT * FROM execution_commands WHERE area_id=? AND command_id=?", (area_id, command_id)).fetchone()
        return cur.rowcount == 1, dict(row)


def dispatch(area_id: int, command_id: str, target: dict[str, Any]) -> None:
    _scope(area_id, command_id)
    with _connection() as c:
        cur = c.execute("UPDATE execution_commands SET outcome='dispatching', target_json=?, updated_at=? "
                        "WHERE area_id=? AND command_id=? AND outcome='claimed'",
                        (json.dumps(target, sort_keys=True, allow_nan=False), core._now(), area_id, command_id))
        if cur.rowcount != 1:
            raise ValueError("Command is no longer claimable for dispatch")


def finish(area_id: int, command_id: str, outcome: str, *, response: dict[str, Any] | None = None, error_code: str = "") -> None:
    _scope(area_id, command_id)
    if outcome not in ("accepted", "rejected", "unknown"):
        raise ValueError("Invalid terminal execution outcome")
    allowed = ("dispatching",) if outcome == "accepted" else ("claimed", "dispatching")
    with _connection() as c:
        cur = c.execute("UPDATE execution_commands SET outcome=?, response_json=?, error_code=?, updated_at=? "
                        "WHERE area_id=? AND command_id=? AND outcome IN (" + ",".join("?" for _ in allowed) + ")",
                        (outcome, json.dumps(response or {}, sort_keys=True, allow_nan=False), error_code,
                         core._now(), area_id, command_id, *allowed))
        if cur.rowcount != 1:
            raise ValueError("Command already has an outcome")


def get(area_id: int, command_id: str) -> dict[str, Any] | None:
    _scope(area_id, command_id)
    core.init()
    with core._connect() as c:
        row = c.execute("SELECT * FROM execution_commands WHERE area_id=? AND command_id=?", (area_id, command_id)).fetchone()
    return dict(row) if row is not None else None


def recover_incomplete() -> int:
    """Startup only, before accepting requests. Never send anything to a broker.

    Even a claimed command is not resumed: this is an operator-reconciliation
    queue, not a retry queue. The existing single-process deployment is required.
    """
    with _connection() as c:
        cur = c.execute("UPDATE execution_commands SET outcome='unknown', error_code='interrupted', updated_at=? "
                        "WHERE outcome IN ('claimed','dispatching')", (core._now(),))
        return cur.rowcount

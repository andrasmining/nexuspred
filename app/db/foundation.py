"""Additive schema for the in-process commercial/execution boundary."""
from __future__ import annotations

import sqlite3


def create_schema(c: sqlite3.Connection) -> None:
    # Execute individual statements: executescript() can commit its caller's
    # transaction. No existing table, identifier or settings value is rewritten.
    c.execute("""CREATE TABLE IF NOT EXISTS execution_commands (
        area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
        command_id TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL,
        request_hash TEXT NOT NULL,
        request_json TEXT NOT NULL,
        target_json TEXT NOT NULL DEFAULT '{}',
        outcome TEXT NOT NULL CHECK(outcome IN ('claimed','dispatching','accepted','rejected','unknown')),
        response_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(area_id, command_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_execution_commands_area_outcome ON execution_commands(area_id, outcome)")
    c.execute("""CREATE TABLE IF NOT EXISTS commercial_entitlements (
        area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
        capability TEXT NOT NULL CHECK(capability='manual_trading'),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(area_id, capability)
    )""")

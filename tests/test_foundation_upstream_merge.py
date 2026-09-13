"""The execution foundation and alpha.97/98 schemas must coexist."""
from __future__ import annotations

import pytest

from app import db
from app.db import announcements, notifications
from app.db import entitlements as grants
from app.db import execution as ledger


@pytest.mark.parametrize(
    "missing",
    [(), ("execution_commands", "commercial_entitlements"), ("notifications", "announcements")],
    ids=["repeat-init", "upgrade-upstream", "upgrade-foundation"],
)
def test_combined_schema_upgrade_preserves_existing_rows(admin, missing):
    area = db.user_primary_area(admin["id"])
    ledger.claim(area, "merge-compat-key", admin["id"], "hash", "{}")
    grants.set_manual_trading(area, False)
    notifications.add_notification(area, "merge-test", "warn", "Retained notification")
    announcements.add_announcement(area, "listing-key", "Retained announcement", "Body", 0, 0)
    scoped_tables = {
        "execution_commands": "area_id",
        "commercial_entitlements": "area_id",
        "notifications": "area_id",
        "announcements": "publisher_area_id",
    }
    with db._connect() as c:
        before = {
            table: [tuple(row) for row in c.execute(
                f'SELECT * FROM "{table}" WHERE "{column}"=? ORDER BY rowid', (area,)
            )]
            for table, column in scoped_tables.items() if table not in missing
        }
        for table in missing:
            c.execute(f'DROP TABLE "{table}"')
    for _ in range(2):
        db.mark_uninitialized()
        db.init()
        with db._connect() as c:
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert set(scoped_tables) <= tables
            columns = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
            assert {"role_note", "role_expires_at"} <= columns
            for table, expected in before.items():
                column = scoped_tables[table]
                actual = [tuple(row) for row in c.execute(
                    f'SELECT * FROM "{table}" WHERE "{column}"=? ORDER BY rowid', (area,)
                )]
                assert actual == expected
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not c.execute("PRAGMA foreign_key_check").fetchall()

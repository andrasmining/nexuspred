"""The execution foundation and alpha.97-99 operations must coexist."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db, platform
from app.db import announcements, notifications
from app.db import entitlements as grants
from app.db import execution as ledger
from app.commercial import entitlements, workspaces
from app.execution.contracts import ManualOrder


@pytest.mark.parametrize(
    "missing",
    [(), ("execution_commands", "commercial_entitlements"), ("notifications", "announcements", "escalations", "settings_history")],
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
        "escalations": "area_id",
        "settings_history": "area_id",
    }
    with db._connect() as c:
        c.execute("INSERT INTO escalations(area_id,title,created_at,last_step_at) VALUES(?,?,?,?)",
                  (area, "Retained escalation", "2026-09-13T12:00:00+00:00", "2026-09-13T12:00:00+00:00"))
        c.execute("INSERT INTO settings_history(area_id,ts,snapshot) VALUES(?,?,?)",
                  (area, "2026-09-13T12:00:00+00:00", "{}"))
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


def test_upstream_platform_module_and_commercial_package_do_not_shadow_each_other(admin):
    assert Path(platform.__file__).name == "platform.py"
    assert callable(platform.broadcast) and callable(platform.public_config)
    area = db.user_primary_area(admin["id"])
    platform.save_config({"canary_minutes": 9})
    grants.set_manual_trading(area, False)
    platform.reset()
    assert platform.get_config()["canary_minutes"] == 9
    assert not entitlements.for_workspace(area).manual_trading
    actor = workspaces.Actor(workspaces.WorkspaceId(area), admin["id"])
    command = ManualOrder.from_payload(actor, {"lid": "test-login", "spec": "TEST", "symbol": "MNQZ6", "action": "buy", "qty": 1})
    assert command.actor is actor

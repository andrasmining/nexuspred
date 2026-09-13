"""Regression coverage for alpha.100 maintenance safety fixes."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import backups, db, mailer
from app import main as main_app


def test_pending_restore_staging_failure_keeps_marker_and_live_database(admin, monkeypatch):
    entry = backups._make_snapshot_sync("rollback-source")
    db.create_user("later@example.com", "password123")
    assert db.user_count() == 2
    backups.schedule_restore(entry["name"])

    real_copy = backups.shutil.copy2

    def fail_restore_copy(src, dst, *args, **kwargs):
        if Path(dst).name.endswith(".db.restoring"):
            raise OSError("simulated staging failure")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backups.shutil, "copy2", fail_restore_copy)
    db.mark_uninitialized()
    with pytest.raises(OSError, match="staging failure"):
        backups.apply_pending_restore()

    marker = Path(backups.config.DATA_DIR) / backups.RESTORE_FILE
    assert marker.exists() and marker.read_text(encoding="utf-8").strip() == entry["name"]
    db.reset_caches()
    db.init()
    assert db.user_count() == 2


def test_pending_restore_missing_snapshot_keeps_retry_marker(admin):
    marker = Path(backups.config.DATA_DIR) / backups.RESTORE_FILE
    marker.write_text("fluxbridge-missing.db", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="snapshot is gone"):
        backups.apply_pending_restore()
    assert marker.exists()


def test_pre_rollback_copy_contains_committed_wal_state(admin):
    entry = backups._make_snapshot_sync("rollback-source")
    backups.schedule_restore(entry["name"])
    db.disconnect()

    live = Path(db.DB_FILE)
    conn = sqlite3.connect(str(live))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE rollback_probe(value TEXT NOT NULL)")
        conn.execute("INSERT INTO rollback_probe(value) VALUES('committed-in-wal')")
        conn.commit()
        assert Path(str(live) + "-wal").exists()

        assert backups.apply_pending_restore() == entry["name"]
    finally:
        conn.close()

    pre = live.with_suffix(".db.pre-rollback")
    assert pre.exists()
    c = sqlite3.connect(str(pre))
    try:
        assert c.execute("SELECT value FROM rollback_probe").fetchone()[0] == "committed-in-wal"
    finally:
        c.close()


def test_mail_backup_permanent_failure_never_becomes_offsite_success(admin):
    mailer.save_config({"provider": "smtp", "host": "mail.example", "from_addr": "noreply@example.com"})
    backups.save_config({"offsite": "mail"})

    import asyncio
    entry = asyncio.run(backups.run("manual"))
    assert entry["offsite"] == "" and entry["offsite_pending"] == "mail:1 pending"
    row_id = entry["offsite_mail_ids"][0]
    db.outbox_failed(row_id, "SMTP permanently unavailable", None)

    failures = backups.reconcile_mail_offsite()
    assert len(failures) == 1 and failures[0][0] == entry["name"]
    stored = next(r for r in backups.list_backups() if r["name"] == entry["name"])
    assert stored["offsite"] == "" and stored["offsite_pending"] == ""
    assert "SMTP permanently unavailable" in stored["offsite_error"]


async def test_startup_refuses_to_open_database_after_restore_failure(monkeypatch):
    opened = False

    def fail_restore():
        raise RuntimeError("restore failed")

    def init_database():
        nonlocal opened
        opened = True

    monkeypatch.setattr(main_app.backups, "apply_pending_restore", fail_restore)
    monkeypatch.setattr(main_app.db, "init", init_database)
    with pytest.raises(RuntimeError, match="restore failed"):
        await main_app._startup()
    assert opened is False

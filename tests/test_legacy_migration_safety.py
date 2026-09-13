"""A failed migration must not create a fresh installation over real users."""
from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest

from app.db import core
from tests.test_alpha86 import _legacy_db


@pytest.fixture
def legacy_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUSPRED_DATA_DIR", raising=False)
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(core, "DB_FILE", tmp_path / "data" / "fluxbridge.db")
    monkeypatch.setattr(core, "LEGACY_DB_FILE", tmp_path / "app" / "data" / "fluxbridge.db")
    _legacy_db(core.LEGACY_DB_FILE, users=1)
    return core


def test_migration_error_aborts_startup_instead_of_opening_setup(monkeypatch):
    monkeypatch.setattr(core, "_initialized", False)

    def failed():
        raise OSError("database migration failed")

    def must_not_open():
        pytest.fail("startup opened a fresh database after losing the migration")

    monkeypatch.setattr(core, "migrate_legacy_db", failed)
    monkeypatch.setattr(core, "_connect", must_not_open)
    with pytest.raises(OSError, match="migration failed"):
        core.init()


def test_partial_destination_is_never_published(legacy_paths, monkeypatch):
    original_connect = sqlite3.connect

    def broken_copy(source, destination):
        Path(destination).write_bytes(b"partial database")
        raise OSError("disk full during migration")

    monkeypatch.setattr(shutil, "copy2", broken_copy)

    class BrokenBackup(sqlite3.Connection):
        def backup(self, target, **kwargs):
            # A destination can already contain bytes when a disk/write fails.
            target.execute("CREATE TABLE incomplete (id INTEGER)")
            target.commit()
            raise OSError("disk full during migration")

    def connect(*args, **kwargs):
        if kwargs.get("uri"):
            kwargs["factory"] = BrokenBackup
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(OSError, match="disk full"):
        core.migrate_legacy_db()
    assert not core.DB_FILE.exists()
    assert core.LEGACY_DB_FILE.exists()
    assert not list(core.DATA_DIR.glob(".fluxbridge-migrate-*"))


def test_wal_users_are_in_the_published_database(legacy_paths):
    writer = sqlite3.connect(core.LEGACY_DB_FILE)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO users(email) VALUES ('wal@example.com')")
        writer.commit()
        assert core.migrate_legacy_db()
        # Only the published file is required; it is a complete SQLite snapshot.
        migrated = sqlite3.connect(core.DB_FILE)
        try:
            assert migrated.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
            assert migrated.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            migrated.close()
    finally:
        writer.close()

"""Additive migration, durable identity and commercial-policy boundaries."""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3

import pytest

from app import config, context, db, signals
from app.db import execution as ledger
from app.db import entitlements as grants
from app.execution.contracts import AccountTarget, ExecutionError, Outcome, RiskEffect
from app.commercial import commercial_policy, entitlements
from tests.test_execution_foundation import command


def snapshot_legacy():
    with db._connect() as c:
        tables = c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                           "AND name NOT IN ('execution_commands','commercial_entitlements') ORDER BY name").fetchall()
        return [(r["name"], r["sql"], [tuple(x) for x in c.execute(f'SELECT * FROM "{r["name"]}" ORDER BY rowid')]) for r in tables]


def test_additive_migration_preserves_all_legacy_tables_and_settings(admin):
    config.save_settings({"trading_enabled": True, "symbol_map": {"MNQ1!": "MNQZ6"}})
    with context.use_area(1):
        signals._map_for(False)["wh:MNQ"] = {"root": "MNQ", "contract": "MNQZ6", "accounts": {"D1": {"qty": 2}}}
        signals.persist_active(force=True)
    # Model an existing installation with the exact legacy schema, no new tables.
    with db._connect() as c:
        c.execute("DROP TABLE execution_commands")
        c.execute("DROP TABLE commercial_entitlements")
    before = snapshot_legacy()
    db.mark_uninitialized()
    db.init()
    assert snapshot_legacy() == before
    assert grants.manual_trading(1) is True
    assert json.loads(db.meta_get("active_trades:1"))["wh:MNQ"]["accounts"]["D1"]["qty"] == 2
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM execution_commands").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM commercial_entitlements").fetchone()[0] == 0
    db.mark_uninitialized()
    db.init()
    assert snapshot_legacy() == before


def test_store_is_full_synchronous_and_not_joined_to_legacy_batch(admin):
    with ledger._connection() as c:
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    cmd = command(admin)
    fresh, _ = ledger.claim(1, cmd.command_id, admin["id"], cmd.fingerprint(), cmd.intent_json())
    assert fresh
    with sqlite3.connect(str(db.DB_FILE)) as outside:
        assert outside.execute("SELECT outcome FROM execution_commands WHERE area_id=? AND command_id=?",
                               (1, cmd.command_id)).fetchone()[0] == "claimed"


def test_concurrent_claim_is_unique_and_terminal_records_are_immutable(admin):
    cmd = command(admin)
    def claim():
        return ledger.claim(1, cmd.command_id, admin["id"], cmd.fingerprint(), cmd.intent_json())[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: claim(), range(4)))
    assert outcomes.count(True) == 1
    with pytest.raises(ValueError):
        ledger.finish(1, cmd.command_id, "accepted")  # no dispatch acknowledgement yet
    ledger.dispatch(1, cmd.command_id, {"spec": "D1"})
    ledger.finish(1, cmd.command_id, "accepted", response={"order_id": 10, "status": "submitted"})
    for operation in (lambda: ledger.dispatch(1, cmd.command_id, {}),
                      lambda: ledger.finish(1, cmd.command_id, "unknown")):
        with pytest.raises(ValueError):
            operation()
    assert ledger.get(1, cmd.command_id)["outcome"] == "accepted"


def test_store_mutations_are_scoped_to_workspace(admin):
    peer = db.create_user("peer@example.com", "password123")
    aid = db.user_primary_area(peer["id"])
    cmd = command(admin)
    ledger.claim(1, cmd.command_id, admin["id"], cmd.fingerprint(), cmd.intent_json())
    assert ledger.get(aid, cmd.command_id) is None
    with pytest.raises(ValueError):
        ledger.dispatch(aid, cmd.command_id, {})
    with pytest.raises(ValueError):
        ledger.finish(aid, cmd.command_id, "rejected")
    assert ledger.get(1, cmd.command_id)["outcome"] == "claimed"
    grants.set_manual_trading(aid, False)
    assert grants.manual_trading(1) and not grants.manual_trading(aid)


@pytest.mark.parametrize("bad", [None, 0, -1, True, "1"])
def test_missing_workspace_is_never_defaulted_in_new_stores(admin, bad):
    with pytest.raises(ValueError):
        ledger.get(bad, "x")
    with pytest.raises(ValueError):
        grants.manual_trading(bad)


def test_missing_workspace_is_not_grandfathered(admin):
    with pytest.raises(ValueError):
        grants.manual_trading(99999)
    with pytest.raises(sqlite3.IntegrityError):
        grants.set_manual_trading(99999, True)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.claim(99999, "x", admin["id"], "hash", "{}")


def test_restart_marks_incomplete_unknown_without_resubmitting_or_changing_terminal_results(admin):
    for name in ("before-dispatch", "after-dispatch", "accepted", "rejected"):
        ledger.claim(1, name, admin["id"], "hash", "{}")
    for name in ("after-dispatch", "accepted"):
        ledger.dispatch(1, name, {"spec": "D1"})
    ledger.finish(1, "accepted", "accepted", response={"order_id": 1})
    ledger.finish(1, "rejected", "rejected")
    assert ledger.recover_incomplete() == 2
    assert ledger.recover_incomplete() == 0
    for name in ("before-dispatch", "after-dispatch"):
        row = ledger.get(1, name)
        assert row["outcome"] == "unknown" and row["error_code"] == "interrupted"
    assert ledger.get(1, "accepted")["outcome"] == "accepted"
    assert ledger.get(1, "rejected")["outcome"] == "rejected"


def test_workspace_deletion_cascades_new_data_without_changing_legacy_deleter(admin):
    user = db.create_user("delete@example.com", "password123")
    aid = db.user_primary_area(user["id"])
    ledger.claim(aid, "deleted-workspace", user["id"], "hash", "{}")
    grants.set_manual_trading(aid, False)
    db.delete_user(user["id"])
    assert ledger.get(aid, "deleted-workspace") is None
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM commercial_entitlements WHERE area_id=?", (aid,)).fetchone()[0] == 0
    assert db.get_user(admin["id"]) is not None


@pytest.mark.parametrize("effect", list(RiskEffect))
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("verified", [True, False])
def test_commercial_policy_never_confuses_a_label_with_reduction(effect, active, verified):
    expected = active or effect in (RiskEffect.CLOSE_RISK, RiskEffect.EMERGENCY) or (
        verified and effect in (RiskEffect.REDUCE_RISK, RiskEffect.PROTECT_RISK))
    assert commercial_policy.permits(effect, entitlements.Entitlements(active), reduction_verified=verified) is expected


def test_invalid_policy_or_boolean_is_not_an_implicit_grant():
    assert not commercial_policy.permits("emergency", entitlements.Entitlements(False))
    for invalid in ("false", 1, None):
        with pytest.raises(ValueError):
            entitlements.Entitlements(invalid)


@pytest.mark.parametrize("field", ["spec", "lid"])
def test_selector_storage_is_bounded(field):
    body = {"spec": "D1", "lid": "L1", field: "x" * 129}
    with pytest.raises(ExecutionError):
        AccountTarget.from_payload(body)


def test_upstream_backup_keeps_command_identity_and_entitlements(admin, tmp_path):
    from app import backups
    ledger.claim(1, "backup-key", admin["id"], "hash", "{}")
    ledger.dispatch(1, "backup-key", {"spec": "DEMO11"})
    ledger.finish(1, "backup-key", "unknown", error_code="broker_unknown")
    grants.set_manual_trading(1, False)
    path = tmp_path / "snapshot.db"
    backups.write_snapshot(str(path))
    with sqlite3.connect(str(path)) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT outcome FROM execution_commands WHERE area_id=? AND command_id=?", (1, "backup-key")).fetchone()[0] == "unknown"
        assert c.execute("SELECT enabled FROM commercial_entitlements WHERE area_id=? AND capability=?", (1, "manual_trading")).fetchone()[0] == 0

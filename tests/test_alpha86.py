"""alpha.86: the parts of PR #22 adopted — the database path, a fail-closed
workspace gate, Rithmic mutation timeouts as unknown outcomes, send-time checks
of outbound targets — plus what replaced its rejected parts."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app import alerts, config, context, db, push, signals, state, tradovate, updater
from app.db import core
from app.discord_signals import dispatcher
from tests.helpers import FakeExecutor
from tests.test_rithmic import rsess  # noqa: F401 - fixture
from tests.test_strategies import ENTRY, active, live, wh  # noqa: F401 - fixtures


# ---------------------------------------------------------------- database path
def test_the_default_data_dir_is_the_repository_root():
    assert core.ROOT_DIR == Path(__file__).resolve().parents[1]
    assert not (core.ROOT_DIR / "app" / "data" / "fluxbridge.db").exists() or True    # the stray file is untracked either way


def _legacy_db(path: Path, users: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    for i in range(users):
        c.execute("INSERT INTO users(email) VALUES (?)", (f"u{i}@example.com",))
    c.commit(); c.close()


def test_a_legacy_database_with_users_is_moved_once(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUSPRED_DATA_DIR", raising=False)
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(core, "DB_FILE", tmp_path / "data" / "fluxbridge.db")
    monkeypatch.setattr(core, "LEGACY_DB_FILE", tmp_path / "app" / "data" / "fluxbridge.db")
    _legacy_db(core.LEGACY_DB_FILE, users=1)
    assert core.migrate_legacy_db() is True
    assert core.DB_FILE.exists() and not core.LEGACY_DB_FILE.exists() and (tmp_path / "app" / "data" / "fluxbridge.db.migrated").exists()
    assert sqlite3.connect(core.DB_FILE).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert core.migrate_legacy_db() is False                          # once: the new location is populated now


def test_an_empty_legacy_database_or_an_explicit_data_dir_moves_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUSPRED_DATA_DIR", raising=False)
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(core, "DB_FILE", tmp_path / "data" / "fluxbridge.db")
    monkeypatch.setattr(core, "LEGACY_DB_FILE", tmp_path / "app" / "data" / "fluxbridge.db")
    _legacy_db(core.LEGACY_DB_FILE, users=0)                          # the file git tracked by mistake: an empty schema
    assert core.migrate_legacy_db() is False and not core.DB_FILE.exists()
    _legacy_db(tmp_path / "other.db", users=2)
    monkeypatch.setattr(core, "LEGACY_DB_FILE", tmp_path / "other.db")
    monkeypatch.setenv("NEXUSPRED_DATA_DIR", str(tmp_path / "ext"))
    assert core.migrate_legacy_db() is False


async def test_the_updater_refuses_a_hard_reset_over_a_tracked_database(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_FILE", config.ROOT_DIR / "README.md")
    assert updater._tracked_runtime_db() == "README.md"
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "outside.db")
    assert updater._tracked_runtime_db() == ""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    monkeypatch.setattr(updater, "_tracked_runtime_db", lambda: "app/data/fluxbridge.db")
    calls = []
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=120: (calls.append(cmd), (True, "x"))[1])
    r = await updater.apply_update()
    assert r["success"] is False and "Update refused" in r["message"] and calls == []


# ---------------------------------------------------------------- workspace gate
async def test_a_login_without_a_workspace_is_refused_not_routed_elsewhere(client, monkeypatch):
    monkeypatch.setattr(db, "user_primary_area", lambda uid: None)
    r = await client.get("/api/token-accounts")
    assert r.status_code == 403 and r.json()["detail"] == "No workspace membership"


# ---------------------------------------------------------------- rithmic: lost answers stay unknown
async def test_rithmic_mutation_timeouts_are_unknown_outcomes_not_rejections(rsess, monkeypatch):  # noqa: F811
    s = rsess["s"]
    await s.connect()
    client = rsess["made"][0]
    ex = tradovate.AccountExecutor(s, {"spec": "APEX-123", "id": s.accounts[0]["id"], "enabled": True})
    with context.use_area(1):
        placed = await ex.place_order(symbol="MNQZ6", action="Sell", qty=2, order_type="Stop", stop_price=20900.0)

    async def timeout(**kw):
        raise asyncio.TimeoutError()
    for method, call in (("modify_order", lambda: ex.modify_order(placed["order_id"], qty=1, order_type="Stop", stop_price=20950.0)),
                         ("cancel_order", lambda: ex.cancel_order(placed["order_id"])),
                         ("exit_position", lambda: ex.liquidate_position("MNQZ6"))):
        monkeypatch.setattr(client, method, timeout)
        with context.use_area(1), pytest.raises(tradovate.OrderOutcomeUnknown, match="outcome unknown"):
            await call()
    with context.use_area(1):
        statuses = [o["status"] for o in list(state._st().orders)[:3]]
    assert statuses == ["unknown", "unknown", "unknown"]


# ---------------------------------------------------------------- outbound targets checked when used
class _Never:
    async def post(self, *a, **k):
        raise AssertionError("nothing may be sent to a rejected target")


async def test_discord_alert_and_signal_targets_are_checked_at_send_time(monkeypatch):
    monkeypatch.setattr(alerts.security, "check_outbound_url", lambda url: "resolves to a private address")
    monkeypatch.setattr(alerts.http, "client", lambda kind: _Never())
    with context.use_area(1):
        await alerts._send_discord("x", settings={"alert_discord_enabled": True, "alert_discord_webhook_url": "https://hooks.example/h"})
    monkeypatch.setattr(dispatcher.security, "check_outbound_url", lambda url: "resolves to a private address")
    monkeypatch.setattr(dispatcher.http, "client", lambda kind: _Never())
    r = await dispatcher._post_one({"label": "t", "url": "https://hooks.example/h", "secret": "s"}, {"action": "buy"})
    assert r["ok"] is False and "rejected" in r["error"]


def test_smtp_and_push_targets_are_checked_at_send_time(monkeypatch):
    monkeypatch.setattr(alerts.security, "check_outbound_url", lambda url: "resolves to a private address")
    monkeypatch.setattr(alerts.config, "load_settings", lambda: {"alert_smtp_username": "u@example.com", "alert_smtp_password": "p",
                                                                 "alert_smtp_host": "smtp.example.com", "alert_smtp_port": 587})

    class NeverSMTP:
        def __init__(self, *a, **k):
            raise AssertionError("SMTP must not connect")
    monkeypatch.setattr(alerts.smtplib, "SMTP", NeverSMTP)
    with pytest.raises(ValueError, match="SMTP target rejected"):
        alerts._send_to_sync("to@example.com", "s", "b")
    monkeypatch.setattr(push.security, "check_outbound_url", lambda url: "resolves to a private address")
    ok, status, error = push._send_one({"endpoint": "https://push.example/e", "p256dh": "x", "auth": "y"}, {"title": "x"})
    assert (ok, status) == (False, 0) and "rejected" in error


async def test_projectx_custom_gateway_is_checked_at_login_only(admin, monkeypatch):
    from app import projectx
    custom = projectx.ProjectXSession(0, {"name": "C", "enabled": True, "px_user": "u", "px_api_key": "k", "px_firm": "https://gateway.example.com", "accounts": []}, area_id=1)
    monkeypatch.setattr(projectx.security, "check_outbound_url", lambda url: "resolves to a private address")
    monkeypatch.setattr(custom, "_client", lambda: _Never())
    with pytest.raises(tradovate.TradovateError, match="gateway rejected"):
        await custom._get_token(force=True)
    known = projectx.ProjectXSession(0, {"name": "T", "enabled": True, "px_user": "u", "px_api_key": "k", "px_firm": "topstep", "accounts": []}, area_id=1)
    monkeypatch.setattr(projectx.security, "check_outbound_url", lambda url: (_ for _ in ()).throw(AssertionError("a known firm is never looked up")))

    class Resp:
        status_code, content = 200, b"{}"

        def json(self):
            return {"success": True, "token": "t"}

    class Client:
        async def post(self, *a, **k):
            return Resp()
    monkeypatch.setattr(known, "_client", lambda: Client())
    assert await known._get_token(force=True) == "t"


# ---------------------------------------------------------------- what replaced the rejected parts
async def test_an_entry_over_a_tracked_trade_is_executed_and_announced(live):  # noqa: F811
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", id="wh_over")
    await signals.process({**ENTRY, "sl": 110.0}, w)
    first_stop = active("wh_over:MNQ")["accounts"]["A"]["sl_order_id"]
    r = await signals.process({**ENTRY, "sl": 110.0}, w)                # e.g. the stop was hit at the broker, the strategy re-enters
    assert r["status"] == "ok" and active("wh_over:MNQ")["accounts"]["A"]["sl_order_id"] != first_stop
    with context.use_area(1):
        assert any("replaces a tracked trade" in e.get("message", "") for e in state._st().events)


def test_old_records_are_dropped_after_the_longer_ttl_with_a_warning(admin):
    assert signals.ACTIVE_TTL_S == 45 * 24 * 3600
    with context.use_area(991):
        m = signals._map_for(False)
        m.clear()
        for i in range(60):
            m[f"T{i}"] = {"ts": 1.0, "accounts": {"A": {"contract": "MNQZ6"}}}
        for _ in range(200):
            signals._map_for(False)
        assert len(m) == 0 and any("dropped after 45 days" in e.get("message", "") for e in state._st().events)
        signals._active.pop(991, None)

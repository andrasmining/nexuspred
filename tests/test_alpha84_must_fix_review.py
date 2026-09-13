from __future__ import annotations

from pathlib import Path

import pytest

from app import alerts, context, db, push, signals, updater
from app.discord_signals import dispatcher
from tests.helpers import FakeExecutor


def test_db_package_still_points_at_repository_root():
    from app.db import core
    assert core.ROOT_DIR == Path(__file__).resolve().parents[1]


def test_updater_detects_a_tracked_runtime_database(monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", updater.config.ROOT_DIR / "README.md")
    assert updater._tracked_runtime_db() == "README.md"


async def test_authenticated_user_without_membership_is_denied(client, monkeypatch):
    monkeypatch.setattr(db, "user_primary_area", lambda _uid: None)
    response = await client.get("/api/token-accounts")
    assert response.status_code == 403
    assert response.json()["detail"] == "No workspace membership"


async def test_existing_live_trade_blocks_tracking_overwrite():
    ex = FakeExecutor("A", positions=[{"symbol": "MNQZ6", "netPos": 1}])
    active = {"wh:MNQ": {"accounts": {"A": {"contract": "MNQZ6"}}, "ts": 1}}
    result = await signals._guard_existing_trade(active, "wh:MNQ", [ex], "MNQ", "")
    assert result and result["reason"] == "active_trade_exists"
    assert "wh:MNQ" in active


async def test_stale_trade_is_cleared_only_after_broker_confirms_flat():
    ex = FakeExecutor("A")
    active = {"wh:MNQ": {"accounts": {"A": {"contract": "MNQZ6"}}, "ts": 1}}
    result = await signals._guard_existing_trade(active, "wh:MNQ", [ex], "MNQ", "")
    assert result is None
    assert "wh:MNQ" not in active


async def test_unresolved_trade_blocks_entry_fail_closed():
    active = {"wh:MNQ": {"accounts": {"deleted-account": {"contract": "MNQZ6"}}, "ts": 1}}
    result = await signals._guard_existing_trade(active, "wh:MNQ", [], "MNQ", "")
    assert result and result["reason"] == "active_trade_unresolved"
    assert "wh:MNQ" in active


async def test_working_order_keeps_trade_live_even_when_position_is_flat():
    ex = FakeExecutor("A", working=[{"id": 7, "symbol": "MNQZ6"}])
    active = {"wh:MNQ": {"accounts": {"A": {"contract": "MNQZ6"}}, "ts": 1}}
    result = await signals._guard_existing_trade(active, "wh:MNQ", [ex], "MNQ", "")
    assert result and result["reason"] == "active_trade_exists"


def test_old_tracking_records_are_not_evicted_by_time():
    with context.use_area(991):
        m = signals._map_for(False)
        m.clear()
        for i in range(60):
            m[str(i)] = {"ts": 1, "accounts": {"A": {"contract": "MNQZ6"}}}
        for _ in range(250):
            assert signals._map_for(False) is m
        assert len(m) == 60
        signals._active.pop(991, None)


async def test_discord_signal_runtime_guard_runs_before_secret_send(monkeypatch):
    monkeypatch.setattr(dispatcher.security, "check_outbound_url", lambda _url: "internal address")

    class NeverClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("request must not be sent")

    monkeypatch.setattr(dispatcher.http, "client", lambda _kind: NeverClient())
    result = await dispatcher._post_one(
        {"label": "custom", "url": "https://example.invalid/hook", "secret": "secret"}, {"action": "buy"}
    )
    assert result["ok"] is False
    assert "rejected" in result["error"]


async def test_alert_discord_runtime_guard_blocks_request(monkeypatch):
    monkeypatch.setattr(alerts.security, "check_outbound_url", lambda _url: "internal address")

    class NeverClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("request must not be sent")

    monkeypatch.setattr(alerts.http, "client", lambda _kind: NeverClient())
    await alerts._send_discord(
        "x", settings={"alert_discord_enabled": True, "alert_discord_webhook_url": "https://example.invalid/hook"}
    )


def test_smtp_runtime_guard_runs_before_connect(monkeypatch):
    monkeypatch.setattr(alerts.security, "check_outbound_url", lambda _url: "internal address")
    monkeypatch.setattr(alerts.config, "load_settings", lambda: {
        "alert_smtp_username": "u@example.com", "alert_smtp_password": "p",
        "alert_smtp_host": "smtp.example.com", "alert_smtp_port": 587,
    })

    class NeverSMTP:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SMTP must not connect")

    monkeypatch.setattr(alerts.smtplib, "SMTP", NeverSMTP)
    with pytest.raises(ValueError, match="SMTP target rejected"):
        alerts._send_to_sync("to@example.com", "subject", "body")


def test_push_runtime_guard_runs_before_webpush(monkeypatch):
    monkeypatch.setattr(push.security, "check_outbound_url", lambda _url: "internal address")
    ok, status, error = push._send_one(
        {"endpoint": "https://example.invalid/push", "p256dh": "x", "auth": "y"}, {"title": "x"}
    )
    assert (ok, status) == (False, 0)
    assert "rejected" in error

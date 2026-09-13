"""alpha.97: notification inbox, severities / quiet hours / digest, role-specific
alerts, workspace readiness + onboarding, what's new, release mail, mail preferences."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app import alerts, config, context, db, mailer, readiness, releases, state
from app import events as bus
from tests.test_alpha93 import _client, trio  # noqa: F401 - the three-role fixture
from tests.test_alpha95 import sent  # noqa: F401 - platform mailer with a faked transport


@pytest.fixture
def channels(monkeypatch, admin):
    """Every channel enabled, transports faked; returns the three call lists."""
    discord, email, pushes = [], [], []

    async def fake_discord(message, **kw):
        if alerts._allowed("discord", config.load_settings()):            # the real sender gates the same way
            discord.append(message)

    async def fake_email(subject, body):
        if alerts._allowed("email", config.load_settings()):
            email.append(subject)

    async def fake_push(title, body, url="/"):
        pushes.append(title)
        return {"sent": 1, "failed": 0, "total": 1}
    monkeypatch.setattr(alerts, "_send_discord", fake_discord)
    monkeypatch.setattr(alerts, "_send_email", fake_email)
    monkeypatch.setattr(alerts.push, "available", lambda: True)
    monkeypatch.setattr(alerts.push, "send_current_area", fake_push)
    config.save_settings({"alert_push_enabled": True})
    return discord, email, pushes


async def _settle():
    """Let scheduled inbox writes reach the single writer thread, then wait for it."""
    await asyncio.sleep(0.01)
    await asyncio.to_thread(alerts.drain_inbox)


# ================================================================ inbox
async def test_every_alert_lands_in_the_inbox(channels, area):
    await alerts.connection_lost("L1", "demo", "boom")
    await alerts.trade_opened("A", "MNQZ6", "long", 2, 21000)
    await alerts.risk_triggered("A", "loss", "daily loss limit hit", -500.0, [])
    await _settle()
    rows = db.list_notifications(area)
    assert [r["kind"] for r in rows] == ["risk.triggered", "position.opened", "connection.lost"]
    assert rows[0]["severity"] == "critical" and rows[2]["severity"] == "warn" and rows[1]["severity"] == "info"
    assert rows[2]["title"] == "Connection lost: L1" and "**" not in rows[2]["message"] and rows[2]["url"] == "/#/settings/accounts"
    assert db.unread_count(area) == 3
    assert db.mark_read(area, [rows[0]["id"]]) == 1 and db.unread_count(area) == 2
    assert db.mark_read(area) == 2 and db.unread_count(area) == 0


async def test_inbox_api_is_per_workspace_and_marks_read(trio):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    db.add_notification(ua, "test", "info", "hello", "m", "/#/")
    db.add_notification(ua, "test", "warn", "world", "m", "/#/")
    async with _client(us["id"]) as c:
        r = (await c.get("/api/notifications")).json()
        assert r["unread"] == 2 and [x["title"] for x in r["rows"]] == ["world", "hello"]
        assert (await c.get("/api/notifications?unread=1&limit=1")).json()["rows"][0]["title"] == "world"
        r = await c.post("/api/notifications/read", json={"ids": [r["rows"][0]["id"]]})
        assert r.json() == {"marked": 1, "unread": 1}
        assert (await c.post("/api/notifications/read", json={})).json()["unread"] == 0
        assert (await c.post("/api/notifications/read", json={"ids": "x"})).status_code == 400
    async with _client(bc["id"]) as c:
        assert (await c.get("/api/notifications/count")).json()["unread"] == 0


def test_inbox_is_bounded_and_pruned(admin, area):
    for i in range(db.notifications.MAX_PER_AREA + 20):
        db.add_notification(area, "t", "info", f"n{i}")
    rows = db.list_notifications(area, limit=200)
    assert len(rows) == 200 and db.unread_count(area) == db.notifications.MAX_PER_AREA
    with db.core._connect() as c:
        c.execute("UPDATE notifications SET created_at='2000-01-01'")
    assert db.prune_notifications() == db.notifications.MAX_PER_AREA


# ================================================================ severities, quiet hours, digest
async def test_channel_thresholds_filter_by_severity(channels, area):
    discord, email, pushes = channels
    config.save_settings({"alert_min_severity_push": "critical", "alert_min_severity_discord": "warn", "alert_min_severity_email": "info"})
    await alerts.trade_opened("A", "MNQZ6", "long", 1)                       # info: nothing but the inbox
    await alerts.connection_lost("L1", "demo", "x")                          # warn: discord + email
    await alerts.risk_triggered("A", "loss", "limit", -1.0, [])              # critical: everything
    await _settle()
    assert discord == [m for m in discord if "Opened" not in m] and len(discord) == 2
    assert len(email) == 2 and pushes == ["Risk guard: A"]
    assert len(db.list_notifications(area)) == 3


def test_quiet_hours_wrap_midnight():
    s = {"alert_quiet_from": "22:00", "alert_quiet_to": "07:00", "journal_timezone": "UTC"}
    assert alerts.quiet_now(s, datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc)) is True
    assert alerts.quiet_now(s, datetime(2026, 9, 14, 6, 59, tzinfo=timezone.utc)) is True
    assert alerts.quiet_now(s, datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)) is False
    assert alerts.quiet_now({"alert_quiet_from": "09:00", "alert_quiet_to": "17:00", "journal_timezone": "Europe/Zurich"}, datetime(2026, 9, 13, 8, 30, tzinfo=timezone.utc)) is True   # 10:30 local
    assert alerts.quiet_now({"alert_quiet_from": "", "alert_quiet_to": "07:00"}) is False
    assert alerts.quiet_now({"alert_quiet_from": "07:00", "alert_quiet_to": "07:00"}) is False


async def test_quiet_hours_hold_everything_but_critical(channels, area, monkeypatch):
    discord, email, pushes = channels
    monkeypatch.setattr(alerts, "quiet_now", lambda s, now=None: True)
    await alerts.connection_lost("L1", "demo", "x")
    await alerts.risk_triggered("A", "loss", "limit", -1.0, [])
    await _settle()
    assert len(discord) == 1 and "Risk guard" in discord[0] and pushes == ["Risk guard: A"]
    assert db.unread_count(area) == 2                                         # the inbox has both


async def test_quiet_hours_setting_validates_and_allows_empty(client):
    assert (await client.post("/api/settings", json={"alert_quiet_from": "25:00"})).status_code == 400
    r = await client.post("/api/settings", json={"alert_quiet_from": "22:00", "alert_quiet_to": ""})
    assert r.status_code == 200 and config.load_settings()["alert_quiet_from"] == "22:00" and config.load_settings()["alert_quiet_to"] == ""
    assert (await client.post("/api/settings", json={"alert_min_severity_push": "loud"})).status_code == 400


async def test_digest_bundles_trade_alerts(channels, area):
    discord, email, pushes = channels
    config.save_settings({"alert_digest_trades": True, "alert_digest_minutes": 15})
    await alerts.trade_opened("A", "MNQZ6", "long", 1)
    await alerts.trade_closed("A", "MNQZ6", "long", 1, 42.0, "3m")
    await alerts.connection_lost("L1", "demo", "x")                          # not a trade alert: goes out at once
    await _settle()
    assert len(discord) == 1 and "Connection lost" in discord[0]
    assert db.unread_count(area) == 3                                         # inbox rows are immediate
    assert await alerts.flush_digest(area) is False                           # not due yet
    assert await alerts.flush_digest(area, force=True) is True
    assert len(discord) == 2 and "2 trade updates" in discord[1] and "Opened" in discord[1] and "Closed" in discord[1]
    assert pushes[-1] == "2 trade updates"
    assert await alerts.flush_digest(area, force=True) is False               # buffer empty
    # a critical alert never waits in the digest
    await alerts.execution_problem("Position without stop", "MNQZ6 on A")
    await _settle()
    assert "Position without stop" in discord[-1]


# ================================================================ role-specific alerts
async def test_publisher_hears_about_subscribers_and_payments(trio, channels, monkeypatch):
    admin, bc, us = trio
    pa = db.user_primary_area(bc["id"])
    with context.use_area(pa):
        wh = config.new_webhook(name="Signal")
        wh["sharing"] = {"enabled": True, "visibility": "all"}
        config.save_settings({"webhooks": [wh]})
    async with _client(us["id"]) as c:
        r = await c.post(f"/api/marketplace/{pa}/{wh['id']}/subscribe", json={"accounts": [], "enabled": True})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert (await c.delete(f"/api/subscriptions/{sid}")).status_code == 200
    await _settle()
    rows = db.list_notifications(pa)
    assert [r["kind"] for r in rows] == ["subscriber.left", "subscriber.joined"]
    assert "us@example.com" in rows[1]["message"] and rows[1]["url"] == "/#/webhooks"
    assert db.list_notifications(db.user_primary_area(us["id"])) == []       # the subscriber's own inbox is untouched
    # the publisher's switch can silence the channels but not the inbox
    with context.use_area(pa):
        config.save_settings({"alert_on_subscribers": False}, area_id=pa)
    await alerts.publisher_event(pa, "payment.failed", "Payment past_due", "x", severity="warn")
    await _settle()
    assert db.list_notifications(pa)[0]["kind"] == "payment.failed"


async def test_fanout_pause_tells_the_publisher(trio):
    admin, bc, us = trio
    from app import signals
    pa = db.user_primary_area(bc["id"])
    ua = db.user_primary_area(us["id"])
    sub = db.upsert_subscription(ua, pa, "wh1", [], user_id=us["id"])
    webhook = {"name": "Signal", "subscription": {"id": sub["id"], "webhook_id": "wh1", "publisher_area_id": pa}, "controls": {"pause_after_errors": 2}}
    with context.use_area(ua):
        assert signals._note_subscription_outcome(webhook, RuntimeError("no fill")) is None
        coro = signals._note_subscription_outcome(webhook, RuntimeError("no fill again"))
        assert coro is not None
        await coro
    await _settle()
    rows = db.list_notifications(pa)
    assert rows and rows[0]["kind"] == "fanout.paused" and "us@example.com" in rows[0]["message"]
    assert db.get_subscription(sub["id"], ua)["enabled"] is False


async def test_security_events_reach_the_user(trio, monkeypatch):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    async with _client(admin["id"]) as c:
        assert (await c.post(f"/api/users/{us['id']}/2fa/reset")).status_code == 200
    await _settle()
    assert db.list_notifications(ua)[0]["kind"] == "mfa.reset"
    # a sign-in from another address than last time
    from app.routers import auth as auth_router

    class _Req:
        headers = {"x-forwarded-for": "203.0.113.9"}
        client = type("C", (), {"host": "203.0.113.9"})()
        url = type("U", (), {"scheme": "https"})()
    db.record_login(us["id"], "198.51.100.1")
    u = db.get_user(us["id"])
    monkeypatch.setattr(auth_router, "client_ip", lambda r: "203.0.113.9")
    auth_router._signed_in(_Req(), u, "password")
    await _settle()
    row = db.list_notifications(ua)[0]
    assert row["kind"] == "login.new_ip" and "203.0.113.9" in row["message"] and "198.51.100.1" in row["message"]


async def test_admins_get_platform_events_in_their_inbox(trio, sent):
    admin, bc, us = trio
    aa = db.user_primary_area(admin["id"])
    n = await alerts.notify_admins("notice", {"title": "Backup: snapshot failed", "message": "disk full", "button": "Open", "url": "https://b/"},
                                   inbox=("backup.alarm", "critical", "Backup: snapshot failed", "/#/settings/backups"))
    await _settle()
    assert n == 1
    rows = db.list_notifications(aa)
    assert rows[0]["kind"] == "backup.alarm" and rows[0]["severity"] == "critical" and rows[0]["message"] == "disk full"
    assert db.list_notifications(db.user_primary_area(bc["id"])) == []
    # a health transition: once per change, recovery without mail
    readiness._prev_status = "ok"
    await readiness._notify_transition({"status": "degraded", "checks": {"disk": {"status": "degraded", "detail": "90 MB free"}}})
    await readiness._notify_transition({"status": "degraded", "checks": {"disk": {"status": "degraded", "detail": "90 MB free"}}})
    await readiness._notify_transition({"status": "ok", "checks": {}})
    await _settle()
    kinds = [r["kind"] for r in db.list_notifications(aa)]
    assert kinds[:2] == ["health.recovered", "health.degraded"]
    assert len([r for r in db.outbox_list() if r["kind"] == "notice"]) == 2       # backup + degraded, not the recovery


async def test_update_available_is_announced_once(trio, sent, monkeypatch):
    admin, bc, us = trio
    from app import updater

    async def fake_check():
        return {"current_version": "5.0.0-alpha.97", "latest_version": "5.0.0-alpha.98", "update_available": True}
    monkeypatch.setattr(updater, "check_for_update", fake_check)
    readiness._update_checked_day = ""
    await readiness._daily_update_check()
    readiness._update_checked_day = ""
    await readiness._daily_update_check()
    await _settle()
    rows = [r for r in db.list_notifications(db.user_primary_area(admin["id"])) if r["kind"] == "update.available"]
    assert len(rows) == 1 and "alpha.98" in rows[0]["title"]


# ================================================================ readiness + onboarding
async def test_workspace_readiness_per_role(trio):
    admin, bc, us = trio
    async with _client(us["id"]) as c:
        r = (await c.get("/api/workspace/readiness")).json()
        keys = [x["key"] for x in r["checks"]["checks"]]
        assert keys == ["login", "accounts", "trading", "risk", "rollover", "alerts", "2fa"]
        assert r["checks"]["ok"] < r["checks"]["total"] and r["onboarding"]["role"] == "user" and r["onboarding"]["complete"] is False
        assert [s["key"] for s in r["onboarding"]["steps"]] == ["login", "risk", "alerts", "signal"]
        assert (await c.post("/api/workspace/onboarding/dismiss")).json()["dismissed"] is True
        assert (await c.get("/api/workspace/readiness")).json()["onboarding"]["dismissed"] is True
    async with _client(bc["id"]) as c:
        r = (await c.get("/api/workspace/readiness")).json()
        keys = [x["key"] for x in r["checks"]["checks"]]
        assert "listing" in keys and "subscribers" in keys and "backup" not in keys
        assert [s["key"] for s in r["onboarding"]["steps"]][:3] == ["login", "record", "listing"]
    async with _client(admin["id"]) as c:
        r = (await c.get("/api/workspace/readiness")).json()
        keys = [x["key"] for x in r["checks"]["checks"]]
        assert keys[-3:] == ["backup", "mailer", "disk"]
        st = {x["key"]: x for x in r["checks"]["checks"]}
        assert st["mailer"]["status"] == "warn" and st["backup"]["status"] == "warn"
        assert [s["key"] for s in r["onboarding"]["steps"]] == ["mailer", "backup", "heartbeat", "status", "login"]


async def test_readiness_reflects_the_workspace(admin, area, monkeypatch):
    config.save_settings({"trading_enabled": True, "token_accounts": [{"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                                                                        "accounts": [{"spec": "DEMO1", "id": 1, "enabled": True, "risk": {"daily_loss_limit": 500}}]}]})
    state.set_session_status("L1", connected=True, broker="tradovate")
    db.record_delivery(area, "push", True, "t")
    u = db.get_user(admin["id"])
    r = readiness.workspace_checks(area, u)
    st = {x["key"]: x for x in r["checks"]}
    assert st["login"]["status"] == "ok" and st["accounts"]["status"] == "ok" and st["trading"]["status"] == "ok"
    assert st["risk"]["status"] == "ok" and st["alerts"]["status"] == "ok" and st["2fa"]["status"] == "warn"
    ob = readiness.onboarding(area, u, r)
    assert {s["key"]: s["done"] for s in ob["steps"]}["login"] is True


# ================================================================ what's new, release mail, prefs
def test_changelog_sections_and_role_filter():
    secs = releases.sections()
    assert "5.0.0-alpha.96" in secs and any("readyz" in b for b in secs["5.0.0-alpha.96"])
    admin_notes = releases.notes_for("admin", "5.0.0-alpha.96")
    user_notes = releases.notes_for("user", "5.0.0-alpha.96")
    assert admin_notes["all"] == len(admin_notes["bullets"]) and len(user_notes["bullets"]) < admin_notes["all"]
    assert releases.notes_for("user", "0.0.0")["bullets"] == []


async def test_whats_new_shown_once_per_version(trio):
    admin, bc, us = trio
    async with _client(us["id"]) as c:
        r = (await c.get("/api/whatsnew")).json()
        assert r["version"] == config.get_version() and r["seen"] is False and r["first_login"] is True
        assert (await c.post("/api/whatsnew/seen")).json()["seen"] is True
        r = (await c.get("/api/whatsnew")).json()
        assert r["seen"] is True and r["first_login"] is False
    releases.mark_seen(us["id"], "5.0.0-alpha.1")                                       # an older version seen → notes show
    async with _client(us["id"]) as c:
        r = (await c.get("/api/whatsnew")).json()
        assert r["seen"] is False and r["first_login"] is False


def test_release_mail_goes_out_once_to_those_who_want_it(trio, sent):
    admin, bc, us = trio
    releases.save_prefs(bc["id"], {"updates": False})
    n = releases.mail_release()
    assert n == 2                                                                        # admin + user, not the broadcaster
    rows = [r for r in db.outbox_list() if r["kind"] == "release"]
    assert sorted(r["to"] for r in rows) == ["admin@example.com", "us@example.com"]
    assert config.get_version() in rows[0]["subject"]
    assert releases.mail_release() == 0                                                   # once per version


async def test_mail_prefs_api_and_unsubscribe_link(trio, anon_client):
    admin, bc, us = trio
    async with _client(us["id"]) as c:
        assert (await c.get("/api/me/mail-prefs")).json() == releases.PREF_DEFAULTS
        r = await c.put("/api/me/mail-prefs", json={"weekly": False, "bogus": True})
        assert r.json()["weekly"] is False and "bogus" not in r.json()
    url = releases.unsubscribe_url(us["id"], "updates")
    token = url.split("t=", 1)[1]
    r = await anon_client.get(f"/unsubscribe?t={token}")
    assert r.status_code == 200 and "Unsubscribed" in r.text
    assert releases.prefs(us["id"])["updates"] is False
    r = await anon_client.get("/unsubscribe?t=garbage")
    assert r.status_code == 200 and "not valid" in r.text
    assert releases.apply_unsubscribe(token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")) is None   # a bad signature
    r = await anon_client.get(f"/unsubscribe?t={token}", headers={"accept-language": "de"})
    assert "Abgemeldet" in r.text


def test_release_mail_needs_the_platform_mailer(trio):
    assert not mailer.configured()
    assert releases.mail_release() == 0 and db.outbox_list() == []


async def test_events_reach_the_inbox_through_the_bus(channels, area):
    bus.emit("agent.lost", name="vps-1", last_ip="1.2.3.4")
    await _settle()
    await _settle()
    rows = db.list_notifications(area)
    assert rows and rows[0]["kind"] == "agent.lost" and rows[0]["severity"] == "warn"

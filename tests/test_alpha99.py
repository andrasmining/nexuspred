"""alpha.99: escalation with acknowledgement, Telegram, latency watchdog, canary, token
pre-warning, update rollback, settings history, user workspace file, assisted support,
quotas, monthly roles report, admin broadcast."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, backups, broadcaster, canary, config, context, db, escalation, platform, readiness, telegram, updater, web
from app import events as bus
from tests.test_alpha93 import _client, trio  # noqa: F401 - the three-role fixture
from tests.test_alpha95 import sent  # noqa: F401 - platform mailer with a faked transport
from tests.test_alpha97 import channels  # noqa: F401 - every alert channel faked


async def _settle():
    await asyncio.sleep(0.05)


# ================================================================ escalation
async def test_critical_alert_opens_an_escalation_and_steps_until_acknowledged(channels, area, sent, monkeypatch):
    discord, email, pushes = channels
    config.save_settings({"alert_escalation": True, "alert_email_enabled": True, "alert_email_to": "me@x", "alert_smtp_username": "u", "alert_smtp_password": "p"})
    sms = []
    monkeypatch.setattr(escalation, "_sms", lambda to, text: sms.append((to, text)) or asyncio.sleep(0))
    tg = []
    monkeypatch.setattr(telegram, "send_area", lambda a, text: _fake_tg(tg, text))
    await alerts.risk_triggered("A", "loss", "limit", -1.0, [])
    await _settle()
    opened = db.open_escalations(area)
    assert len(opened) == 1 and opened[0]["title"] == "Risk guard: A" and opened[0]["stage"] == 0
    row = db.list_notifications(area)[0]
    assert "Acknowledge:" in row["message"] and "/ack?t=" in row["message"]
    await alerts.trade_opened("A", "MNQZ6", "long", 1)                                    # info: no escalation
    assert len(db.open_escalations(area)) == 1
    esc = opened[0]
    created = datetime.fromisoformat(esc["created_at"]).timestamp()
    assert await escalation.tick(created + 60) == 1 and db.get_escalation(esc["id"])["stage"] == 0          # too early
    await escalation.tick(created + escalation.STAGE_EMAIL_S + 1)
    assert db.get_escalation(esc["id"])["stage"] == 1
    assert any("unacknowledged" in s for s in email) and any(m["kind"] == "notice" and "Risk guard" in m["subject"] for m in db.outbox_list())
    config.save_settings({"alert_sms_to": "+41790000000"})
    platform.save_config({"twilio_sid": "AC1", "twilio_token": "t", "twilio_from": "+41000"})
    await escalation.tick(created + escalation.STAGE_SMS_S + 1)
    assert db.get_escalation(esc["id"])["stage"] == 2 and sms and sms[0][0] == "+41790000000" and "/ack?t=" in sms[0][1]
    assert escalation.acknowledge(esc["id"], "tester") is not None
    assert db.open_escalations(area) == [] and escalation.acknowledge(esc["id"], "again") is None


async def _fake_tg(sink, text):
    sink.append(text)
    return False


async def test_ack_link_and_api(trio, anon_client):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    esc = escalation.open_escalation(ua, "execution.problem", "Position without stop", "MNQ", "/#/")
    tok = escalation.ack_url(esc["id"]).split("t=", 1)[1]
    assert escalation.parse_token(tok) == esc["id"] and escalation.parse_token(tok[:-2] + "zz") is None
    async with _client(bc["id"]) as c:
        assert (await c.post(f"/api/escalations/{esc['id']}/ack")).status_code == 404          # another workspace
    r = await anon_client.get(f"/ack?t={tok}")
    assert r.status_code == 200 and "Acknowledged" in r.text and "Position without stop" in r.text
    r = await anon_client.get(f"/ack?t={tok}")
    assert "Already acknowledged" in r.text
    assert "not valid" in (await anon_client.get("/ack?t=nope")).text
    esc2 = escalation.open_escalation(ua, "risk.triggered", "Risk", "x", "/#/")
    async with _client(us["id"]) as c:
        r = (await c.get("/api/escalations")).json()
        assert [e["id"] for e in r["open"]] == [esc2["id"]]
        assert (await c.post(f"/api/escalations/{esc2['id']}/ack")).json()["acked_by"] == "us@example.com"


# ================================================================ telegram
async def test_telegram_linking_and_channel(trio, monkeypatch):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    sent_msgs = []

    class _R:
        status_code, text = 200, ""

    class _Client:
        async def post(self, url, json=None, timeout=None):
            sent_msgs.append((url.rsplit("/", 1)[1], json))
            return _R()

        async def get(self, url, params=None, timeout=None):
            class R2(_R):
                def json(self):
                    return {"result": [{"update_id": 7, "message": {"chat": {"id": 4242, "first_name": "Tobi", "username": "tobi"}, "text": f"/start {code}"}}]}
            return R2()
    monkeypatch.setattr(telegram.http, "client", lambda *a, **k: _Client())
    async with _client(us["id"]) as c:
        assert (await c.post("/api/telegram/link-code")).status_code == 400                     # bot not configured
    platform.save_config({"telegram_bot_token": "123:abc", "telegram_bot_name": "fb_bot"})
    assert platform.public_config()["telegram_bot_token"] == "********" and json.loads(db.meta_get(platform.META_KEY))["telegram_bot_token"] != "123:abc"
    async with _client(us["id"]) as c:
        r = (await c.post("/api/telegram/link-code")).json()
        code = r["code"]
        assert r["bot_name"] == "fb_bot" and (await c.get("/api/telegram")).json()["linked"] is False
    assert await telegram.poll_once() == 1
    assert telegram.chat_for(ua) == "4242" and telegram.status(ua)["chat_name"] == "Tobi @tobi"
    assert sent_msgs[-1][0] == "sendMessage" and "Linked" in sent_msgs[-1][1]["text"]
    assert telegram._consume_code(code) is None                                                 # single use
    # the channel: gated by its switch and threshold
    with context.use_area(ua):
        config.save_settings({"alert_telegram_enabled": True, "alert_min_severity_telegram": "warn"})
        await alerts.trade_opened("A", "MNQZ6", "long", 1)
        await _settle()
        assert not any(m[0] == "sendMessage" and "Opened" in m[1]["text"] for m in sent_msgs)   # info < warn
        await alerts.connection_lost("L1", "demo", "x")
        await _settle(); await _settle()
        assert any("Connection lost" in m[1]["text"] for m in sent_msgs if m[0] == "sendMessage")
        assert db.delivery_status(ua)["telegram"]["last_ok"] is not None
    async with _client(us["id"]) as c:
        assert (await c.post("/api/telegram/test")).json()["sent"] is True
        assert (await c.delete("/api/telegram/link")).json()["linked"] is False
    assert telegram.chat_for(ua) == ""


# ================================================================ latency watchdog + canary + token warning
async def test_latency_watchdog_notifies_once_per_state(trio, sent):
    admin, bc, us = trio
    platform.save_config({"latency_p95_warn_ms": 100})
    for _ in range(4):
        bus.emit("signal.done", webhook="w", status="ok", seconds=0.5)
    await _settle()
    await readiness._latency_watch()
    await readiness._latency_watch()
    await _settle()
    rows = [r for r in db.list_notifications(db.user_primary_area(admin["id"])) if r["kind"].startswith("latency")]
    assert [r["kind"] for r in rows] == ["latency.slow"] and "p95 500.0 ms" in rows[0]["title"]
    readiness._latency.clear()
    readiness._lag_samples.clear()
    await readiness._latency_watch()
    await _settle()
    rows = [r for r in db.list_notifications(db.user_primary_area(admin["id"])) if r["kind"].startswith("latency")]
    assert [r["kind"] for r in rows] == ["latency.ok", "latency.slow"]


async def test_canary_runs_the_signal_path_in_the_simulator(trio, sent):
    admin, bc, us = trio
    platform.save_config({"canary_enabled": True, "canary_max_ms": 5000})
    res = await canary.run_once()
    assert res["ok"] is True and res["ms"] is not None and res["slow"] is False, res
    st = canary.status()
    assert st["runs"] == 1 and st["state"] == "ok" and st["ok_share"] == 1.0
    platform.save_config({"canary_max_ms": 100})
    async with _client(admin["id"]) as c:
        for _ in range(3):
            await c.post("/api/platform/canary/run")
        chk = (await c.get("/api/platform/health")).json()["checks"]
        assert "canary" in chk
        assert (await c.get("/api/platform/config")).json()["canary"]["runs"] == 4
    rows = [x for x in db.list_notifications(db.user_primary_area(admin["id"])) if x["kind"].startswith("canary")]
    assert not rows or rows[0]["kind"] == "canary.alarm"


async def test_token_expiry_warning_once_per_expiry(channels, area):
    from app import health
    discord, email, pushes = channels

    class _Sess:
        area_id = area
        name = "L1"
        _token_expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    health._warn_expiring(_Sess(), "renew failed: 401")
    health._warn_expiring(_Sess(), "renew failed: 401")
    await _settle(); await _settle()
    assert pushes == ["Token expiring: L1"] and any("Token expiring" in m for m in discord)
    row = db.list_notifications(area)[0]
    assert row["kind"] == "token.expiring" and " min" in row["message"] and "401" in row["message"]
    far = _Sess(); far._token_expires = datetime.now(timezone.utc) + timedelta(hours=5)
    health._warn_expiring(far, "x")
    await _settle()
    assert len(pushes) == 1


# ================================================================ rollback + settings history + workspace file
async def test_pre_update_point_and_pending_restore(admin, monkeypatch, tmp_path):
    entry = await backups.run("pre-update")
    db.meta_set(updater.PRE_UPDATE_KEY, json.dumps({"sha": "a" * 40, "version": "5.0.0-alpha.98", "backup": entry["name"], "at": entry["created_at"]}))
    assert updater.rollback_point()["backup"] == entry["name"]
    backups.schedule_restore(entry["name"])
    with pytest.raises(ValueError):
        backups.schedule_restore("fluxbridge-nope.db")
    db.create_user("later@example.com", "password123")                                       # after the snapshot
    assert db.user_count() == 2
    # a restart: the pending marker replaces the live file before it is opened
    db.mark_uninitialized()
    name = backups.apply_pending_restore()
    assert name == entry["name"] and backups.apply_pending_restore() is None
    db.reset_caches()
    db.init()
    assert db.user_count() == 1 and (backups.Path(db.DB_FILE).with_suffix(".db.pre-rollback")).exists()


async def test_rollback_api_refuses_on_a_managed_host(client, monkeypatch):
    monkeypatch.setenv("RENDER", "1")
    db.meta_set(updater.PRE_UPDATE_KEY, json.dumps({"sha": "b" * 40, "version": "x", "backup": "", "at": ""}))
    r = await client.post("/api/update/rollback", json={"restore_db": False})
    assert r.status_code == 200 and r.json()["success"] is False and "Managed host" in r.json()["message"]
    assert (await client.get("/api/update/rollback")).json()["point"]["version"] == "x"
    db.meta_set(updater.PRE_UPDATE_KEY, "")
    r = await client.post("/api/update/rollback", json={})
    assert r.json()["success"] is False and "No update" in r.json()["message"]


async def test_settings_history_records_and_restores(client, admin, area):
    config.save_settings({"default_qty": 3})
    config.save_settings({"risk_state": {"x": 1}})                                           # machine state: no version
    config.save_settings({"default_qty": 5, "allowed_symbols": ["MNQ"]})
    r = (await client.get("/api/settings/history")).json()
    versions = r["versions"]
    assert sorted(versions[0]["keys"]) == ["allowed_symbols", "default_qty"] and versions[1]["keys"] == ["initial"] and versions[0]["actor"] == "system"
    old_id = versions[1]["id"]
    r = await client.post(f"/api/settings/history/{old_id}/restore")
    assert r.status_code == 200 and config.load_settings()["default_qty"] == 3 and config.load_settings()["allowed_symbols"] == config.DEFAULT_SETTINGS["allowed_symbols"]
    r = (await client.get("/api/settings/history")).json()
    assert r["versions"][0]["actor"] == "admin@example.com" and "default_qty" in r["versions"][0]["keys"]
    assert (await client.post("/api/settings/history/999/restore")).status_code == 404
    # secrets stay encrypted inside the stored snapshot
    config.save_settings({"alert_smtp_password": "hunter2"})
    snap = db.get_settings_version(area, (await client.get("/api/settings/history")).json()["versions"][0]["id"])["snapshot"]
    assert "hunter2" not in snap


async def test_user_exports_and_imports_their_workspace_without_sharing(trio):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    with context.use_area(ua):
        wh = config.new_webhook(name="Mine")
        wh["sharing"] = {"enabled": False, "title": "secret draft"}
        config.save_settings({"webhooks": [wh]})
    async with _client(us["id"]) as c:
        r = await c.get("/api/settings/export")
        assert r.status_code == 200
        doc = r.json()
        assert doc["settings"]["webhooks"][0]["name"] == "Mine" and "sharing" not in doc["settings"]["webhooks"][0]
        r = await c.post("/api/settings/import", json={**doc, "settings": {**doc["settings"], "default_qty": 4}})
        assert r.status_code == 200, r.text
    with context.use_area(ua):
        assert config.load_settings()["default_qty"] == 4
    async with _client(bc["id"]) as c:
        doc = (await c.get("/api/settings/export")).json()
        assert isinstance(doc["settings"], dict)


# ================================================================ assisted support
async def test_support_grant_allows_writes_and_notes(trio):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    async with _client(admin["id"]) as c:
        r = await c.post(f"/api/users/{us['id']}/support")
        cookie = r.cookies.get(web.SUPPORT_COOKIE)
        c.headers["cookie"] += f"; {web.SUPPORT_COOKIE}={cookie}"
        assert (await c.post("/api/webhooks", json={"name": "nope"})).status_code == 403
        assert (await c.post("/api/support/note", json={"message": "Checked your routing — looks fine."})).status_code == 200
    await _settle()
    assert db.list_notifications(ua)[0]["kind"] == "support.note" and "admin@example.com" in db.list_notifications(ua)[0]["title"]
    async with _client(us["id"]) as c:
        r = await c.post("/api/me/support-grant", json={"hours": 24})
        assert r.json()["grant"]["by"] == "us@example.com" and (await c.get("/api/me")).json()["support_grant"]
    async with _client(admin["id"]) as c:
        c.headers["cookie"] += f"; {web.SUPPORT_COOKIE}={cookie}"
        assert (await c.get("/api/me")).json()["support"]["email"] == "us@example.com"
        r = await c.post("/api/webhooks", json={"name": "fixed by support"})
        assert r.status_code == 200, r.text
    with context.use_area(ua):
        assert [w["name"] for w in config.load_settings()["webhooks"]] == ["fixed by support"]
    acts = [a for a in db.list_audit(10) if a["action"] == "support_write"]
    assert acts and acts[0]["actor_email"] == "admin@example.com"
    hist = db.list_settings_versions(ua)
    assert hist[0]["actor"] == "admin@example.com (support)"
    async with _client(us["id"]) as c:
        assert (await c.post("/api/me/support-grant", json={"hours": 0})).json()["grant"] is None
    async with _client(admin["id"]) as c:
        c.headers["cookie"] += f"; {web.SUPPORT_COOKIE}={cookie}"
        assert (await c.post("/api/webhooks", json={"name": "nope"})).status_code == 403
        assert (await c.post("/api/support/note", json={"message": ""})).status_code == 400
    async with _client(admin["id"]) as c:
        assert (await c.post("/api/support/note", json={"message": "x"})).status_code == 400          # not in a support view


# ================================================================ quotas
async def test_quotas_per_role_and_override(trio):
    admin, bc, us = trio
    assert web.quota_for(us) == {"webhooks": 5, "groups": 3, "agents": 2} and web.quota_for(admin)["webhooks"] is None
    async with _client(us["id"]) as c:
        for i in range(5):
            assert (await c.post("/api/webhooks", json={"name": f"w{i}"})).status_code == 200
        r = await c.post("/api/webhooks", json={"name": "w6"})
        assert r.status_code == 403 and "5 of 5 webhooks" in r.text
        me = (await c.get("/api/me")).json()
        assert me["quotas"]["webhooks"] == {"used": 5, "max": 5} and me["quotas"]["agents"] == {"used": 0, "max": 2}
        for i in range(2):
            assert (await c.post("/api/agents/pairing-code", json={})).status_code == 200
    async with _client(admin["id"]) as c:
        assert (await c.put(f"/api/users/{us['id']}/quota", json={"webhooks": "x"})).status_code == 400
        r = await c.put(f"/api/users/{us['id']}/quota", json={"webhooks": 7, "groups": None})
        assert r.json()["quota"] == {"webhooks": 7, "groups": None, "agents": 2}
    async with _client(us["id"]) as c:
        assert (await c.post("/api/webhooks", json={"name": "w6"})).status_code == 200
        for i in range(4):
            assert (await c.post("/api/copy/groups", json={"name": f"g{i}"})).status_code == 200      # unlimited now
    async with _client(admin["id"]) as c:
        r = await c.put(f"/api/users/{us['id']}/quota", json={})                                       # back to defaults
        assert r.json()["quota"]["webhooks"] == 5
    assert db.list_audit(3)[0]["action"] == "quota_set"


# ================================================================ roles report + broadcast
async def test_monthly_roles_report_once(trio, sent):
    admin, bc, us = trio
    db.request_role(us["id"], "broadcaster", {})
    first = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    assert await broadcaster.roles_report(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)) == 0
    assert await broadcaster.roles_report(first) == 3 and await broadcaster.roles_report(first) == 0
    await _settle()
    row = [r for r in db.list_notifications(db.user_primary_area(admin["id"])) if r["kind"] == "roles.report"][0]
    assert "2026-10" in row["title"] and "1 broadcaster" in row["message"] and "bc@example.com" in row["message"] and "us@example.com" in row["message"]


async def test_broadcast_reaches_roles_with_banner(trio, sent):
    admin, bc, us = trio
    async with _client(bc["id"]) as c:
        assert (await c.post("/api/broadcast", json={"title": "x", "body": "y"})).status_code == 403
    async with _client(admin["id"]) as c:
        assert (await c.post("/api/broadcast", json={"title": "", "body": "y"})).status_code == 400
        r = await c.post("/api/broadcast", json={"title": "Maintenance", "body": "Sunday 03:00 UTC", "roles": ["user"], "mail": True, "banner_hours": 2})
        assert r.status_code == 200 and r.json()["recipients"] == 1 and r.json()["mailed"] == 1 and r.json()["banner_id"]
    await _settle()
    assert db.list_notifications(db.user_primary_area(us["id"]))[0]["kind"] == "broadcast"
    assert db.list_notifications(db.user_primary_area(bc["id"])) == []
    async with _client(us["id"]) as c:
        me = (await c.get("/api/me")).json()
        assert me["banner"]["title"] == "Maintenance"
    async with _client(bc["id"]) as c:
        assert (await c.get("/api/me")).json()["banner"] is None
    async with _client(admin["id"]) as c:
        assert (await c.delete("/api/broadcast/banner")).json()["cleared"] is True
    async with _client(us["id"]) as c:
        assert (await c.get("/api/me")).json()["banner"] is None
    assert [m for m in db.outbox_list() if m["kind"] == "notice"][0]["to"] == "us@example.com"


def test_platform_config_validation(admin):
    with pytest.raises(ValueError):
        platform.save_config({"twilio_from": "0790000000"})
    with pytest.raises(ValueError):
        platform.save_config({"canary_minutes": 0})
    cfg = platform.save_config({"latency_p95_warn_ms": 250, "canary_enabled": True})
    assert cfg["latency_p95_warn_ms"] == 250 and platform.public_config()["canary_enabled"] is True and platform.public_config()["sms_configured"] is False
    assert time.time() > 0

"""alpha.95: platform mailer, templates, outbox with backoff, alert delivery log."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import alerts, config, db, mailer
from tests.test_alpha93 import _client, trio  # noqa: F401 - the three-role fixture


@pytest.fixture
def sent(monkeypatch):
    """The platform sender configured (SMTP) with the transport faked: every
    delivery lands in the list instead of on the wire."""
    out: list[tuple[str, str, str]] = []
    mailer.save_config({"provider": "smtp", "host": "mail.example", "from_addr": "noreply@example.com", "from_name": "Fluxbridge"})

    def fake_smtp(cfg, to_addr, subject, html_body, text_body, attachment=""):
        out.append((to_addr, subject, text_body))
    monkeypatch.setattr(mailer, "_smtp_send", fake_smtp)
    return out


# ================================================================ config
def test_config_is_validated_encrypted_and_masked(admin):
    with pytest.raises(ValueError):
        mailer.save_config({"provider": "fax"})
    with pytest.raises(ValueError):
        mailer.save_config({"provider": "smtp"})                                  # no host
    with pytest.raises(ValueError):
        mailer.save_config({"provider": "resend", "from_addr": "x@y"})            # no key
    with pytest.raises(ValueError):
        mailer.save_config({"provider": "smtp", "host": "h", "from_addr": "not an address"})
    cfg = mailer.save_config({"provider": "smtp", "host": "mail.example", "port": "465", "username": "u", "password": "pw", "from_addr": "noreply@example.com"})
    assert cfg["port"] == 465 and mailer.configured()
    stored = json.loads(db.meta_get(mailer.META_KEY))
    assert stored["password"] != "pw" and stored["password"]                       # encrypted at rest
    pub = mailer.public_config()
    assert pub["password"] == "********" and pub["configured"] is True and pub["counts"] == {"pending": 0, "sent": 0, "failed": 0}
    mailer.save_config({"password": "********"})                                  # the mask never overwrites the secret
    assert mailer.get_config()["password"] == "pw"


def test_environment_pins_the_config(admin, monkeypatch):
    monkeypatch.setenv("NEXUSPRED_MAIL_PROVIDER", "postmark")
    monkeypatch.setenv("NEXUSPRED_MAIL_API_KEY", "tok")
    monkeypatch.setenv("NEXUSPRED_MAIL_FROM", "noreply@example.com")
    assert mailer.configured() and mailer.get_config()["provider"] == "postmark"
    assert mailer.env_locked() == ["api_key", "from_addr", "provider"]
    mailer.save_config({"provider": "off"})                                       # stored, but the environment wins
    assert mailer.get_config()["provider"] == "postmark"


# ================================================================ templates
def test_templates_render_in_the_recipients_language():
    subj, html, text = mailer.render("invite", "de", {"inviter": "admin@example.com", "url": "https://b/register?code=1", "role_note": " als Broadcaster", "expiry": ""})
    assert subj == "Einladung zu Fluxbridge" and "Konto erstellen" in html and "https://b/register?code=1" in text
    assert "als Broadcaster" in text and "<html" in html and "Fluxbridge" in html
    subj, html, text = mailer.render("password_reset", "xx", {"who": "An administrator", "url": "https://b/reset?token=t"})
    assert subj.startswith("Reset your") and "Set new password: https://b/reset?token=t" in text   # unknown language → English
    subj, html, text = mailer.render("notice", "en", {"title": "Hi <b>", "message": "m & n", "button": "Go", "url": "https://b/", "unsubscribe_url": "https://b/u"})
    assert "Hi &lt;b&gt;" in html and "m &amp; n" in html and "Unsubscribe" in html and "https://b/u" in text
    subj, html, text = mailer.render("role_changed", "en", {**mailer.role_ctx("broadcaster", "en", "admin@example.com")})
    assert "publish webhooks" in text and "Open Fluxbridge" not in text          # no url → no button


def test_recipient_language_follows_the_workspace(admin):
    area = db.user_primary_area(admin["id"])
    assert mailer.lang_for_area(area) == "en"
    config.save_settings({"ui_language": "de"}, area_id=area)
    assert mailer.lang_for_area(area) == "de" and mailer.lang_for_user(admin["id"]) == "de"
    assert mailer.lang_for_area(None) == "en"


# ================================================================ outbox
async def test_outbox_delivers_and_records_the_route(admin, sent):
    rid = mailer.send_template("u@example.com", "test", {"route": "smtp", "url": "https://b/"}, area_id=1)
    row = db.outbox_get(rid)
    assert row["status"] == "pending" and row["kind"] == "test"
    assert await mailer.deliver_pending() == {"sent": 1, "failed": 0, "retry": 0}
    row = db.outbox_get(rid)
    assert row["status"] == "sent" and row["route"] == "smtp:mail.example" and row["attempts"] == 1
    assert sent == [("u@example.com", "Fluxbridge: test e-mail", row["text"])]
    assert db.outbox_counts()["sent"] == 1 and await mailer.deliver_pending() == {"sent": 0, "failed": 0, "retry": 0}


async def test_failures_back_off_then_give_up_and_flag_the_address(admin, monkeypatch):
    mailer.save_config({"provider": "smtp", "host": "mail.example", "from_addr": "noreply@example.com"})

    def boom(*a, **k):
        raise ConnectionRefusedError("no route to host")
    monkeypatch.setattr(mailer, "_smtp_send", boom)
    rid = mailer.enqueue("dead@example.com", "s", "<p>h</p>", "t", kind="invite")
    assert await mailer.deliver_pending() == {"sent": 0, "failed": 0, "retry": 1}
    row = db.outbox_get(rid)
    assert row["status"] == "pending" and row["attempts"] == 1 and "no route" in row["last_error"] and row["next_at"] > row["created_at"]
    assert await mailer.deliver_pending() == {"sent": 0, "failed": 0, "retry": 0}       # not due yet
    for i in range(2, mailer.MAX_ATTEMPTS + 1):
        with db.core._connect() as c:                                                   # pretend the backoff elapsed
            c.execute("UPDATE outbox SET next_at=?, attempts=? WHERE id=?", ("2000-01-01T00:00:00+00:00", i - 1, rid))
        await mailer.deliver_pending()
    row = db.outbox_get(rid)
    assert row["status"] == "failed" and row["attempts"] == mailer.MAX_ATTEMPTS
    assert mailer.address_failures("dead@example.com") == 1 and not mailer.address_blocked("dead@example.com")
    for _ in range(mailer.BLOCK_AFTER - 1):
        mailer._note_outcome("dead@example.com", False)
    assert mailer.address_blocked("Dead@Example.com")                                 # case-insensitive
    # a delivery that succeeds clears the flag; a retry from the admin re-queues the row
    monkeypatch.setattr(mailer, "_smtp_send", lambda *a, **k: None)
    assert db.outbox_retry(rid) and (await mailer.send_now(rid))["status"] == "sent"
    assert not mailer.address_blocked("dead@example.com")


async def test_fallback_to_the_workspace_smtp_when_the_platform_sender_is_off(admin, monkeypatch):
    calls = []
    assert not mailer.configured() and not mailer.can_send(1)                        # nothing configured anywhere
    rid = mailer.enqueue("u@example.com", "s", "", "t", kind="notice", area_id=1)
    assert await mailer.deliver_pending() == {"sent": 0, "failed": 0, "retry": 1}     # no route → retried, not lost
    assert "no workspace SMTP" in db.outbox_get(rid)["last_error"]
    config.save_settings({"alert_smtp_username": "me@gmail.com", "alert_smtp_password": "app-pw"}, area_id=1)
    assert mailer.can_send(1)
    monkeypatch.setattr(mailer, "_workspace_send", lambda area_id, to, subj, html, text, att="": calls.append((area_id, to)))
    with db.core._connect() as c:
        c.execute("UPDATE outbox SET next_at='2000-01-01' WHERE id=?", (rid,))
    assert await mailer.deliver_pending() == {"sent": 1, "failed": 0, "retry": 0}
    assert db.outbox_get(rid)["route"] == "workspace-smtp" and calls == [(1, "u@example.com")]
    # a row without a workspace has no fallback
    rid2 = mailer.enqueue("u@example.com", "s", "", "t")
    await mailer.deliver_pending()
    assert "no platform mailer" in db.outbox_get(rid2)["last_error"]


async def test_api_providers_post_the_right_payload(admin, monkeypatch):
    posts = []

    class _Resp:
        def __init__(self, code):
            self.status_code, self.text = code, "err"

    class _Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            posts.append((url, json, headers))
            return _Resp(500 if "fail" in str(json.get("to") or json.get("To")) else 200)
    monkeypatch.setattr(mailer.http, "client", lambda *a, **k: _Client())
    mailer.save_config({"provider": "resend", "api_key": "re_1", "from_addr": "noreply@example.com", "reply_to": "help@example.com"})
    rid = mailer.enqueue("u@example.com", "Subj", "<p>h</p>", "t")
    await mailer.deliver_pending()
    url, payload, headers = posts[-1]
    assert url == mailer.RESEND_API and headers["Authorization"] == "Bearer re_1"
    assert payload == {"from": "Fluxbridge <noreply@example.com>", "to": ["u@example.com"], "subject": "Subj", "html": "<p>h</p>", "text": "t", "reply_to": "help@example.com"}
    assert db.outbox_get(rid)["route"] == "resend"
    mailer.save_config({"provider": "postmark", "api_key": "pm_1"})
    rid = mailer.enqueue("fail@example.com", "Subj", "", "t")
    await mailer.deliver_pending()
    url, payload, headers = posts[-1]
    assert url == mailer.POSTMARK_API and headers["X-Postmark-Server-Token"] == "pm_1" and payload["To"] == "fail@example.com"
    assert "postmark answered 500" in db.outbox_get(rid)["last_error"]


async def test_outbox_loop_wakes_on_enqueue(admin, sent, monkeypatch):
    monkeypatch.setattr(mailer, "LOOP_TICK_S", 5.0)
    task = asyncio.create_task(mailer.outbox_loop())
    await asyncio.sleep(0.05)
    mailer.enqueue("u@example.com", "s", "", "t")
    for _ in range(50):
        await asyncio.sleep(0.02)
        if sent:
            break
    task.cancel()
    assert sent and sent[0][0] == "u@example.com"


def test_prune_keeps_pending_rows(admin):
    a = mailer.enqueue("a@example.com", "s", "<p>x</p>", "t")
    b = mailer.enqueue("b@example.com", "s", "<p>x</p>", "t")
    db.outbox_sent(b, "smtp")
    with db.core._connect() as c:
        c.execute("UPDATE outbox SET created_at='2000-01-01', sent_at='2000-01-01'")
    assert db.outbox_prune() == 1
    assert db.outbox_get(a)["status"] == "pending" and db.outbox_get(b) is None


# ================================================================ call sites
async def test_invite_reset_and_role_notices_go_through_the_outbox(trio, sent, monkeypatch):
    admin, bc, us = trio
    config.save_settings({"ui_language": "de"}, area_id=db.user_primary_area(us["id"]))
    async with _client(admin["id"]) as c:
        r = await c.post("/api/users/invite", json={"role": "broadcaster", "email": "new@example.com", "send_email": True})
        assert r.json()["emailed"] is True and r.json()["smtp_configured"] is True
        r = await c.post(f"/api/users/{us['id']}/reset")
        assert r.json()["emailed"] is True and r.json()["url"] == ""
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster"})
        assert r.status_code == 200
    rows = {r["kind"]: r for r in db.outbox_list()}
    assert rows["invite"]["to"] == "new@example.com" and "invited" in rows["invite"]["subject"]         # admin's workspace: English
    assert rows["password_reset"]["subject"] == "Fluxbridge-Passwort zurücksetzen"                        # the user's workspace: German
    assert rows["role_changed"]["subject"] == "Deine Fluxbridge-Rolle ist jetzt Broadcaster"
    await mailer.deliver_pending()
    assert sorted(t[0] for t in sent) == ["new@example.com", "us@example.com", "us@example.com"]
    # the Broadcaster request reaches every admin, in their language, and nobody else
    u2 = db.create_user("us2@example.com", "password123")
    async with _client(u2["id"]) as c:
        assert (await c.post("/api/me/role-request", json={})).status_code == 200
    reqs = [r for r in db.outbox_list() if r["kind"] == "role_request"]
    assert [r["to"] for r in reqs] == ["admin@example.com"]


async def test_no_route_means_no_promise(trio, monkeypatch):
    admin, bc, us = trio
    async with _client(admin["id"]) as c:
        r = await c.post("/api/users/invite", json={"email": "new@example.com", "send_email": True})
        assert r.json()["emailed"] is False and r.json()["smtp_configured"] is False
        r = await c.post(f"/api/users/{us['id']}/reset")
        assert r.json()["emailed"] is False and "/reset?token=" in r.json()["url"]
    assert db.outbox_list() == []


async def test_mail_blocked_shows_in_me(trio):
    admin, bc, us = trio
    for _ in range(mailer.BLOCK_AFTER):
        mailer._note_outcome("us@example.com", False)
    async with _client(us["id"]) as c:
        assert (await c.get("/api/me")).json()["mail_blocked"] is True
    async with _client(admin["id"]) as c:
        assert (await c.get("/api/me")).json()["mail_blocked"] is False


# ================================================================ admin API
async def test_mail_api_is_admin_only_and_round_trips(trio, sent):
    admin, bc, us = trio
    async with _client(bc["id"]) as c:
        assert (await c.get("/api/mail/config")).status_code == 403
        assert (await c.get("/api/mail/log")).status_code == 403
    async with _client(admin["id"]) as c:
        cfg = (await c.get("/api/mail/config")).json()
        assert cfg["provider"] == "smtp" and cfg["configured"] is True
        r = await c.put("/api/mail/config", json={"from_name": "Bridge", "password": "secret"})
        assert r.status_code == 200 and r.json()["password"] == "********" and r.json()["from_name"] == "Bridge"
        assert (await c.put("/api/mail/config", json={"provider": "nope"})).status_code == 400
        r = await c.post("/api/mail/test")
        assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["route"] == "smtp:mail.example", r.text
        assert sent[-1][0] == "admin@example.com" and "test e-mail" in sent[-1][1]
        log = (await c.get("/api/mail/log")).json()
        assert log["counts"]["sent"] == 1 and log["rows"][0]["kind"] == "test" and "html" not in log["rows"][0]
        assert (await c.post("/api/mail/retry/999")).status_code == 404
    assert db.list_audit(3)[0]["action"] == "mail_config"


async def test_test_mail_needs_a_route(client):
    r = await client.post("/api/mail/test")
    assert r.status_code == 400 and "No mail route" in r.text


# ================================================================ delivery log
class _FakeClient:
    code = 200

    async def post(self, url, json=None, timeout=None):
        class R:
            status_code = _FakeClient.code
            text = "boom" if _FakeClient.code >= 400 else ""
        if "raise" in url:
            raise OSError("dns")
        return R()


async def test_alert_deliveries_are_logged_per_channel(admin, area, monkeypatch):
    monkeypatch.setattr(alerts.http, "client", lambda *a, **k: _FakeClient())
    config.save_settings({"alert_discord_enabled": True, "alert_discord_webhook_url": "https://discord/hook"})
    await alerts._send_discord("first")
    await asyncio.sleep(0.05)
    st = db.delivery_status(area)
    assert st["discord"]["last_ok"] and st["discord"]["failures"] == 0 and st["email"]["attempts"] == 0
    _FakeClient.code = 500
    for _ in range(3):
        await alerts._send_discord("again")
    await asyncio.sleep(0.05)
    st = db.delivery_status(area)["discord"]
    assert st["failures"] == 3 and st["degraded"] is True and "500" in st["last_error"]["error"]
    _FakeClient.code = 200
    await alerts._send_discord("ok")
    await asyncio.sleep(0.05)
    assert db.delivery_status(area)["discord"]["degraded"] is False
    # e-mail: an incomplete channel attempts nothing (and logs nothing); a failing one is logged
    await alerts._send_email("s", "b")
    config.save_settings({"alert_email_enabled": True, "alert_email_to": "me@x", "alert_smtp_username": "u", "alert_smtp_password": "p"})
    monkeypatch.setattr(alerts, "_send_email_sync", lambda s, b: (_ for _ in ()).throw(OSError("smtp down")))
    await alerts._send_email("s", "b")
    await asyncio.sleep(0.05)
    assert db.delivery_status(area)["email"]["attempts"] == 1 and "smtp down" in db.delivery_status(area)["email"]["last_error"]["error"]
    recent = db.recent_deliveries(area)
    assert recent[0]["channel"] == "email" and recent[0]["ok"] is False
    assert db.prune_deliveries(days=0) >= 6


async def test_deliveries_api_is_per_workspace(trio):
    admin, bc, us = trio
    db.record_delivery(db.user_primary_area(us["id"]), "push", True, "hello")
    async with _client(us["id"]) as c:
        r = (await c.get("/api/alerts/deliveries")).json()
        assert r["channels"]["push"]["last_ok"] and r["recent"][0]["title"] == "hello"
    async with _client(bc["id"]) as c:
        r = (await c.get("/api/alerts/deliveries")).json()
        assert r["recent"] == [] and r["channels"]["push"]["last_ok"] is None


async def test_push_outcome_is_logged(admin, area, monkeypatch):
    monkeypatch.setattr(alerts.push, "available", lambda: True)

    async def fake_send(title, body, url="/"):
        return {"sent": 0, "failed": 2, "total": 2}
    monkeypatch.setattr(alerts.push, "send_current_area", fake_send)
    await alerts._send_push("t", "m")
    await asyncio.sleep(0.05)
    st = db.delivery_status(area)["push"]
    assert st["failures"] == 1 and "rejected" in st["last_error"]["error"]

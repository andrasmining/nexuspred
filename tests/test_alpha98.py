"""alpha.98: announcements to subscribers, application with data, trial Broadcasters,
cockpit, tiers, weekly report."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, broadcaster, config, context, db, mailer, releases
from app import copy as cp
from tests.test_alpha93 import _client, trio  # noqa: F401 - the three-role fixture
from tests.test_alpha95 import sent  # noqa: F401 - platform mailer with a faked transport


@pytest.fixture
def listing(trio):
    """The broadcaster publishes a webhook; the user subscribes to it."""
    admin, bc, us = trio
    pa = db.user_primary_area(bc["id"])
    with context.use_area(pa):
        wh = config.new_webhook(name="Signal")
        wh["sharing"] = {"enabled": True, "visibility": "all", "title": "NQ Breakout", "published_at": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}
        config.save_settings({"webhooks": [wh]})
    sub = db.upsert_subscription(db.user_primary_area(us["id"]), pa, wh["id"], [], user_id=us["id"])
    return admin, bc, us, pa, wh, sub


async def _settle():
    """Let scheduled inbox writes reach the single writer thread, then wait for it."""
    await asyncio.sleep(0.01)
    await asyncio.to_thread(alerts.drain_inbox)


# ================================================================ announcements
async def test_announcement_reaches_subscribers_inbox_and_mail(listing, sent):
    admin, bc, us, pa, wh, sub = listing
    async with _client(bc["id"]) as c:
        r = await c.get("/api/announcements")
        assert r.json()["left_today"] == 3 and r.json()["listings"][0]["key"] == wh["id"]
        r = await c.post("/api/announcements", json={"listing_key": wh["id"], "title": "No trading today", "body": "FOMC at 20:00 — no entries."})
        assert r.status_code == 200, r.text
        assert r.json()["recipients"] == 1 and r.json()["mailed"] == 1 and r.json()["left_today"] == 2
    await _settle()
    rows = db.list_notifications(db.user_primary_area(us["id"]))
    assert rows[0]["kind"] == "announcement" and "No trading today" in rows[0]["title"] and "bc@example.com" in rows[0]["title"]
    mail = [m for m in db.outbox_list() if m["kind"] == "announcement"]
    assert len(mail) == 1 and mail[0]["to"] == "us@example.com" and mail[0]["subject"] == "bc@example.com: No trading today"
    assert db.list_notifications(db.user_primary_area(admin["id"])) == []          # not a subscriber
    assert db.list_audit(3)[0]["action"] == "announce"


async def test_announcement_respects_prefs_cap_and_validation(listing, sent):
    admin, bc, us, pa, wh, sub = listing
    releases.save_prefs(us["id"], {"announcements": False})
    async with _client(bc["id"]) as c:
        assert (await c.post("/api/announcements", json={"title": "", "body": "x"})).status_code == 400
        assert (await c.post("/api/announcements", json={"title": "x", "body": "y", "listing_key": "nope"})).status_code == 400
        for i in range(3):
            r = await c.post("/api/announcements", json={"title": f"n{i}", "body": "all"})
            assert r.status_code == 200 and r.json()["mailed"] == 0                 # inbox yes, mail no
        r = await c.post("/api/announcements", json={"title": "n4", "body": "all"})
        assert r.status_code == 400 and "3 announcements" in r.text
    async with _client(us["id"]) as c:
        assert (await c.post("/api/announcements", json={"title": "x", "body": "y"})).status_code == 403
        assert (await c.get("/api/broadcaster/cockpit")).status_code == 403
    assert db.unread_count(db.user_primary_area(us["id"])) == 3


def test_publisher_subscribers_are_distinct_and_enabled_only(listing):
    admin, bc, us, pa, wh, sub = listing
    assert [r["email"] for r in db.publisher_subscribers(pa)] == ["us@example.com"]
    db.update_subscription(sub["id"], sub["area_id"], enabled=False)
    assert db.publisher_subscribers(pa) == []


# ================================================================ tiers
def test_tiers_follow_the_facts():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    pub = lambda days: (now - timedelta(days=days)).isoformat()  # noqa: E731
    rec = lambda trades, verified=True, hist=120: {"trades": trades, "verified": verified, "first_trade_at": (now - timedelta(days=hist)).isoformat()}  # noqa: E731
    assert broadcaster.tier_of(None, 0, None, now=now) == ""
    assert broadcaster.tier_of(rec(5), 0, pub(10), now=now) == ""                    # ten days but neither trades nor subscribers
    assert broadcaster.tier_of(rec(5), 1, pub(10), now=now) == "bronze"
    assert broadcaster.tier_of(rec(40), 0, pub(10), now=now) == "bronze"              # not 30 days published yet
    assert broadcaster.tier_of(rec(40), 0, pub(45), now=now) == "silver"
    assert broadcaster.tier_of(rec(40, verified=False), 0, pub(45), now=now) == "bronze"   # unverified never passes silver
    assert broadcaster.tier_of(rec(40), 0, pub(45), error_rate=0.1, now=now) == "bronze"
    assert broadcaster.tier_of(rec(150), 5, pub(100), error_rate=0.01, now=now) == "gold"
    assert broadcaster.tier_of(rec(150), 4, pub(100), now=now) == "silver"            # four subscribers
    assert broadcaster.tier_of(rec(150, hist=30), 5, pub(100), now=now) == "silver"   # a month of history only


async def test_marketplace_carries_the_tier(listing):
    admin, bc, us, pa, wh, sub = listing
    async with _client(us["id"]) as c:
        items = (await c.get("/api/marketplace")).json()
    it = [i for i in items if i["webhook_id"] == wh["id"]][0]
    assert it["tier"] == "bronze"                                                      # 40 days published, one subscriber, no trades


# ================================================================ cockpit
async def test_cockpit_sums_the_business(listing):
    admin, bc, us, pa, wh, sub = listing
    db.upsert_payment(sub["area_id"], pa, wh["id"], status="active", price_cents=2900, currency="chf")
    async with _client(bc["id"]) as c:
        r = (await c.get("/api/broadcaster/cockpit")).json()
    assert r["totals"] == {"subscribers": 1, "active": 1, "pending": 0, "paused": 0, "unpaid": 0, "revenue": {"chf": 2900}, "listings": 1}
    li = r["listings"][0]
    assert li["key"] == wh["id"] and li["title"] == "NQ Breakout" and li["tier"] == "bronze" and li["revenue"] == {"chf": 2900}
    assert len(r["growth"]) == 8 and r["growth"][-1]["new"] == 1 and r["announce_left_today"] == 3
    db.update_subscription(sub["id"], sub["area_id"], enabled=False)
    r = broadcaster.cockpit(pa)
    assert r["totals"]["paused"] == 1 and r["totals"]["active"] == 0


# ================================================================ application + trial
async def test_request_carries_the_application_and_admin_reviews_it(trio, sent):
    admin, bc, us = trio
    async with _client(us["id"]) as c:
        assert (await c.post("/api/me/role-request", json={"link": "ftp://x"})).status_code == 400
        r = await c.post("/api/me/role-request", json={"strategy": "NQ ORB", "instruments": "MNQ", "experience": "4 years", "link": "https://x.example"})
        assert r.status_code == 200
    async with _client(bc["id"]) as c:
        assert (await c.get(f"/api/users/{us['id']}/application")).status_code == 403
    async with _client(admin["id"]) as c:
        a = (await c.get(f"/api/users/{us['id']}/application")).json()
        assert a["note"] == {"strategy": "NQ ORB", "instruments": "MNQ", "experience": "4 years", "link": "https://x.example"}
        assert a["record"]["trades"] == 0 and a["role_request"] == "broadcaster" and a["subscriptions"] == 0
        assert (await c.get("/api/users/999/application")).status_code == 404
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster", "days": 30})
        assert r.status_code == 200 and r.json()["expires_at"][:10] == (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    u = db.get_user(us["id"])
    assert u["role"] == "broadcaster" and u["role_expires_at"] and u["role_note"]["strategy"] == "NQ ORB" and u["role_request"] == ""


async def test_trial_extend_make_permanent_and_validation(trio):
    admin, bc, us = trio
    async with _client(admin["id"]) as c:
        assert (await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster", "days": 400})).status_code == 400
        assert (await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster", "days": "x"})).status_code == 400
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster", "days": 30})
        first = r.json()["expires_at"]
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster", "days": 90})           # same role: extend
        assert r.json()["expires_at"] > first
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster", "days": 0})            # permanent
        assert r.json()["expires_at"] == "" and db.get_user(us["id"])["role_expires_at"] == ""
        r = await c.post(f"/api/users/{bc['id']}/role", json={"role": "user"})                              # demotion clears any expiry
        assert r.json()["expires_at"] == ""
    assert [a["action"] for a in db.list_audit(6)].count("role_set") >= 4


async def test_trial_warns_then_expires(trio, sent, monkeypatch):
    admin, bc, us = trio
    ua = db.user_primary_area(us["id"])
    db.set_role(us["id"], "broadcaster", expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
    with context.use_area(ua):
        wh = config.new_webhook(name="S")
        wh["sharing"] = {"enabled": True, "visibility": "all"}
        config.save_settings({"webhooks": [wh]})
    out = await broadcaster.trial_check()
    assert out == {"warned": 1, "expired": 0}
    assert await broadcaster.trial_check() == {"warned": 0, "expired": 0}                                  # once per expiry
    await _settle()
    rows = db.list_notifications(db.user_primary_area(admin["id"]))
    assert rows[0]["kind"] == "trial.ending" and "us@example.com" in rows[0]["title"]
    assert any(m["kind"] == "notice" and "Trial ending" in m["subject"] for m in db.outbox_list())
    # …and when it lapses: demoted, unpublished, user told
    db.set_role(us["id"], "broadcaster", expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    out = await broadcaster.trial_check()
    assert out == {"warned": 0, "expired": 1}
    u = db.get_user(us["id"])
    assert u["role"] == "user" and u["role_expires_at"] == ""
    with context.use_area(ua):
        assert config.load_settings()["webhooks"][0]["sharing"]["enabled"] is False
    assert any(m["kind"] == "role_changed" and m["to"] == "us@example.com" for m in db.outbox_list())
    assert db.expiring_roles() == []


# ================================================================ weekly report
def test_weekly_report_per_role(listing):
    admin, bc, us, pa, wh, sub = listing
    now = datetime.now(timezone.utc)

    def trade(pid, days, entry, exit_, gross, fees):
        return {"pair_id": pid, "source": "csv", "account_id": 1, "account_spec": "A", "account_name": "A", "environment": "demo", "contract_id": 0,
                "symbol": "MNQZ6", "root": "MNQ", "side": "long", "qty": 1, "entry_price": entry, "exit_price": exit_,
                "entry_ts": (now - timedelta(days=days, hours=1)).isoformat(), "exit_ts": (now - timedelta(days=days)).isoformat(),
                "entry_fill_id": pid * 2, "exit_fill_id": pid * 2 + 1, "points": exit_ - entry, "value_per_point": 2.0, "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees}
    db.upsert_journal_trade(pa, trade(1, 2, 100.0, 150.0, 100.0, 2.0))
    db.upsert_journal_trade(pa, trade(2, 1, 150.0, 130.0, -40.0, 2.0))
    rep = broadcaster.weekly_report(pa, db.get_user(bc["id"]))
    assert rep["role"] == "broadcaster" and rep["stats"]["trades"] == 2
    assert rep["lines"][0].startswith("Net +$56.00 from 2 trades, hit rate 50 %")
    assert any("Best session" in ln and "worst" in ln for ln in rep["lines"])
    assert any("Subscribers: 1" in ln for ln in rep["lines"]) and "cockpit" in rep
    rep_u = broadcaster.weekly_report(db.user_primary_area(us["id"]), db.get_user(us["id"]))
    assert rep_u["lines"][0] == "No closed trades in the last seven days." and "cockpit" not in rep_u
    rep_a = broadcaster.weekly_report(db.user_primary_area(admin["id"]), db.get_user(admin["id"]))
    assert any(ln.startswith("Platform: 3 users, 1 broadcasters") for ln in rep_a["lines"])
    config.save_settings({"ui_language": "de"}, area_id=pa)
    assert broadcaster.weekly_report(pa, db.get_user(bc["id"]))["lines"][0].startswith("Netto +$56.00 aus 2 Trades")


async def test_weekly_mail_goes_out_monday_morning_once(trio, sent, monkeypatch):
    admin, bc, us = trio
    monday_early = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)          # 06:00 Zurich — too early
    assert await broadcaster.mail_weekly_reports(monday_early) == 0
    monday = datetime(2026, 9, 14, 6, 30, tzinfo=timezone.utc)                # 08:30 Zurich
    releases.save_prefs(bc["id"], {"weekly": False})
    assert await broadcaster.mail_weekly_reports(monday) == 2                  # admin + user
    assert await broadcaster.mail_weekly_reports(monday) == 0                  # once per week
    tuesday = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
    assert await broadcaster.mail_weekly_reports(tuesday) == 0
    rows = [m for m in db.outbox_list() if m["kind"] == "weekly"]
    assert sorted(m["to"] for m in rows) == ["admin@example.com", "us@example.com"] and "2026-W38" in rows[0]["subject"]
    await mailer.deliver_pending()
    assert len([s for s in sent if "weekly" in s[1].lower() or "Wochenreport" in s[1]]) == 2


async def test_weekly_mail_needs_the_platform_mailer(trio):
    assert not mailer.configured()
    assert await broadcaster.mail_weekly_reports(datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)) == 0


def test_copy_group_listing_in_cockpit(trio):
    admin, bc, us = trio
    pa = db.user_primary_area(bc["id"])
    with context.use_area(pa):
        g = cp.new_group("Lead")
        g["sharing"] = {"enabled": True, "title": "Alpha Leader", "published_at": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}
        cp.save_groups([g])
    db.upsert_subscription(db.user_primary_area(us["id"]), pa, f"copy:{g['id']}", [], user_id=us["id"])
    r = broadcaster.cockpit(pa)
    assert r["listings"][0]["kind"] == "copy" and r["listings"][0]["title"] == "Alpha Leader" and r["listings"][0]["tier"] == "bronze"
    assert r["listing_keys"] == [{"key": f"copy:{g['id']}", "title": "Alpha Leader"}]

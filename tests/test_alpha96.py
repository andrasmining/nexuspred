"""alpha.96: automatic verified backups, off-site copy, deep health, status page, heartbeat, incidents."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import backups, db, mailer, metrics, readiness, state
from app import events as bus
from tests.test_alpha93 import _client, trio  # noqa: F401 - the three-role fixture


# ================================================================ backups
def test_snapshot_is_consistent_verified_and_indexed(admin):
    db.create_user("u@example.com", "password123")
    db.meta_set("session_secret", "never-leaves")
    entry = backups._make_snapshot_sync("test")
    p = backups.backup_dir() / entry["name"]
    assert p.exists() and entry["verified"] is True and entry["integrity"] == "ok" and entry["mismatch"] == []
    assert entry["size"] > 0 and len(entry["sha256"]) == 64 and "daily" in entry["roles"]
    c = sqlite3.connect(str(p))
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key='session_secret'").fetchone()[0] == 0     # the secret stays home
    c.close()
    assert backups.list_backups()[0]["name"] == entry["name"] and backups.last_verified()["name"] == entry["name"]
    st = backups.status()
    assert st["count"] == 1 and st["stale"] is False and st["age_hours"] is not None and st["age_hours"] < 1


def test_probe_flags_a_corrupt_or_wrong_copy(admin, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all")
    r = backups.probe(bad)
    assert r["verified"] is False and r["integrity"].startswith("error")
    other = tmp_path / "other.db"
    c = sqlite3.connect(str(other))
    c.executescript("CREATE TABLE users(id); CREATE TABLE areas(id); INSERT INTO users VALUES(1),(2),(3),(4),(5),(6),(7),(8),(9),(10);")
    c.commit(); c.close()
    r = backups.probe(other)
    assert r["verified"] is False and "users" in r["mismatch"]                      # ten users in the copy, one live


def test_rotation_keeps_daily_weekly_monthly_and_pinned(admin):
    rows = []
    for i in range(30):
        d = datetime(2026, 8, 1, tzinfo=timezone.utc).replace(day=1 + i)
        rows.append({"name": f"fluxbridge-{d:%Y%m%d}-000000.db", "created_at": d.isoformat(), "roles": backups._roles_for(d)})
    rows[0]["pinned"] = True                                                        # Aug 1 (monthly + pinned)
    backups.save_config({"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 3})
    kept, drop = backups.rotate(rows)
    names = {r["name"] for r in kept}
    assert len(drop) + len(kept) == 30
    assert all(f"fluxbridge-202608{d}-000000.db" in names for d in ("24", "25", "26", "27", "28", "29", "30"))   # 7 daily
    sundays = [r["name"] for r in rows if "weekly" in r["roles"]]
    assert all(n in names for n in sundays[-4:])                                    # 4 weekly
    assert "fluxbridge-20260801-000000.db" in names                                  # monthly / pinned
    assert "fluxbridge-20260812-000000.db" not in names                              # a plain weekday drops


def test_config_validation_and_secret_at_rest(admin):
    with pytest.raises(ValueError):
        backups.save_config({"hour_utc": 25})
    with pytest.raises(ValueError):
        backups.save_config({"offsite": "s3"})                                       # nothing configured
    with pytest.raises(ValueError):
        backups.save_config({"s3_endpoint": "http://plain"})
    cfg = backups.save_config({"offsite": "s3", "s3_endpoint": "https://acc.r2.cloudflarestorage.com", "s3_bucket": "b", "s3_access_key": "AK", "s3_secret_key": "SK", "s3_prefix": "fb"})
    assert cfg["s3_prefix"] == "fb/" and json.loads(db.meta_get(backups.META_KEY))["s3_secret_key"] != "SK"
    assert backups.public_config()["s3_secret_key"] == "********"
    backups.save_config({"s3_secret_key": "********"})
    assert backups.get_config()["s3_secret_key"] == "SK"


def test_encrypt_round_trip_needs_the_same_key(admin, tmp_path):
    src = tmp_path / "a.db"; src.write_bytes(b"sqlite bytes " * 100)
    enc = tmp_path / "a.db.enc"; out = tmp_path / "b.db"
    backups.encrypt_file(src, enc)
    assert enc.read_bytes() != src.read_bytes()
    backups.decrypt_file(enc, out)
    assert out.read_bytes() == src.read_bytes()
    assert backups._cli(["decrypt", str(enc), str(tmp_path / "c.db")]) == 0 and (tmp_path / "c.db").read_bytes() == src.read_bytes()
    assert backups._cli([]) == 2


def test_sigv4_signature_is_deterministic():
    cfg = {"s3_region": "auto", "s3_access_key": "AKIAEXAMPLE", "s3_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    h1 = backups._sigv4_headers(cfg, "PUT", "/bucket/fluxbridge/x.enc", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "acc.r2.cloudflarestorage.com", now)
    h2 = backups._sigv4_headers(cfg, "PUT", "/bucket/fluxbridge/x.enc", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "acc.r2.cloudflarestorage.com", now)
    assert h1 == h2 and h1["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20260913/auto/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=")
    assert h1["x-amz-date"] == "20260913T120000Z"


async def test_s3_push_uses_the_configured_bucket(admin, monkeypatch):
    puts = []

    class _R:
        status_code, text = 200, ""

    class _Client:
        async def put(self, url, content=None, headers=None, timeout=None):
            puts.append((url, len(content), headers))
            return _R()
    monkeypatch.setattr(backups.http, "client", lambda *a, **k: _Client())
    monkeypatch.setattr(backups.security, "check_outbound_url", lambda url: None)
    backups.save_config({"offsite": "s3", "s3_endpoint": "https://s3.example.com", "s3_bucket": "bk", "s3_access_key": "AK", "s3_secret_key": "SK"})
    entry = await backups.run("manual")
    assert entry["offsite"].startswith("s3:https://s3.example.com/bk/fluxbridge/fluxbridge-") and entry["offsite"].endswith(".db.enc")
    url, n, headers = puts[-1]
    assert n > 0 and headers["x-amz-content-sha256"] and "Authorization" in headers
    assert not (backups.backup_dir() / (entry["name"] + ".enc")).exists()            # the encrypted temp file is gone
    assert (await backups.test_offsite())["ok"] is True and puts[-1][0].endswith(".txt")


async def test_mail_offsite_attaches_the_encrypted_snapshot(admin, monkeypatch):
    sent = []
    mailer.save_config({"provider": "smtp", "host": "mail.example", "from_addr": "noreply@example.com"})
    monkeypatch.setattr(mailer, "_smtp_send", lambda cfg, to, subj, html, text, att="": sent.append((to, subj, att)))
    backups.save_config({"offsite": "mail"})
    entry = await backups.run("manual")
    assert entry["offsite"] == "mail:1 admin(s)"
    row = db.outbox_list()[0]
    assert row["kind"] == "backup" and row["attachment"].endswith(".db.enc") and Path(row["attachment"]).exists()
    await mailer.deliver_pending()
    assert sent and sent[0][0] == "admin@example.com" and sent[0][2].endswith(".db.enc")
    assert not Path(row["attachment"]).exists()                                      # released after the send
    # too big for mail → error + alarm, snapshot still there
    backups.save_config({"mail_max_mb": 1})
    monkeypatch.setattr(backups, "encrypt_file", lambda src, dst: dst.write_bytes(b"x" * (2 << 20)))
    entry = await backups.run("manual")
    assert "exceeds the mail limit" in entry["offsite_error"]


async def test_alarm_once_per_day_and_stale_detection(admin, monkeypatch):
    mailer.save_config({"provider": "smtp", "host": "mail.example", "from_addr": "noreply@example.com"})
    monkeypatch.setattr(backups, "write_snapshot", lambda path: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    with pytest.raises(sqlite3.OperationalError):
        await backups.run("manual")
    with pytest.raises(sqlite3.OperationalError):
        await backups.run("manual")
    notices = [r for r in db.outbox_list() if r["kind"] == "notice"]
    assert len(notices) == 1 and "snapshot failed" in notices[0]["subject"]           # one mail per day
    assert any("disk full" in e["message"] for e in state.recent_events())
    assert backups.status()["stale"] is True


def test_schedule_due_once_per_day(admin, monkeypatch):
    backups.save_config({"hour_utc": 21, "minute_utc": 15})
    assert backups.due(datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)) is False
    assert backups.due(datetime(2026, 9, 13, 21, 16, tzinfo=timezone.utc)) is True
    db.meta_set("backups:last_day", "2026-09-13")
    assert backups.due(datetime(2026, 9, 13, 23, 0, tzinfo=timezone.utc)) is False
    assert backups.due(datetime(2026, 9, 14, 21, 20, tzinfo=timezone.utc)) is True
    backups.save_config({"enabled": False})
    assert backups.due(datetime(2026, 9, 14, 21, 20, tzinfo=timezone.utc)) is False
    nxt = backups.next_run_at(datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc))
    assert nxt.isoformat().startswith("2026-09-14T21:15")


async def test_backup_api_round_trip(trio):
    admin, bc, us = trio
    async with _client(bc["id"]) as c:
        assert (await c.get("/api/backups")).status_code == 403
        assert (await c.post("/api/backups/run")).status_code == 403
    async with _client(admin["id"]) as c:
        r = await c.post("/api/backups/run")
        assert r.status_code == 200 and r.json()["backup"]["verified"] is True
        name = r.json()["backup"]["name"]
        r = await c.get("/api/backups")
        assert r.json()["status"]["count"] == 1 and r.json()["backups"][0]["name"] == name
        r = await c.get(f"/api/backups/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.sqlite3") and len(r.content) > 1000
        assert (await c.get("/api/backups/../../etc/passwd")).status_code in (404, 400)
        assert (await c.get("/api/backups/fluxbridge-nope.db")).status_code == 404
        assert (await c.put("/api/backups/config", json={"keep_daily": 0})).status_code == 400
        assert (await c.put("/api/backups/config", json={"keep_daily": 3})).json()["config"]["keep_daily"] == 3
        r = await c.delete(f"/api/backups/{name}")
        assert r.status_code == 200 and r.json()["status"]["count"] == 0
    assert [a["action"] for a in db.list_audit(5)] == ["backup_delete", "backup_config", "backup_download", "backup_run"]


async def test_updater_download_still_works(client):
    r = await client.get("/api/update/backup")
    assert r.status_code in (200, 409)                                              # 409: the key lives in the DB in tests


# ================================================================ readiness
async def test_deep_check_reports_every_subsystem(admin):
    res = await readiness.check()
    assert res["status"] in ("ok", "degraded") and set(res["checks"]) >= {"database", "disk", "backup", "brokers", "history", "event_loop", "mail", "streams", "latency"}
    assert res["checks"]["database"]["status"] == "ok" and res["checks"]["brokers"]["detail"] == "no broker sessions configured"
    assert res["checks"]["backup"]["detail"].startswith("first backup at")
    backups.save_config({"enabled": False})
    res = await readiness.check()
    assert res["checks"]["backup"]["status"] == "degraded" and res["status"] == "degraded"
    assert readiness.last_result() is res


async def test_brokers_and_lag_and_latency_feed_the_check(admin, area):
    state.set_session_status("L1", connected=True, broker="tradovate")
    state.set_session_status("P1", connected=False, broker="projectx")
    readiness._lag_samples.append((0.0, 250.0))
    bus.emit("signal.done", webhook="w", status="ok", seconds=0.12)
    bus.emit("signal.done", webhook="w", status="ok", seconds=0.30)
    await asyncio.sleep(0.02)
    res = await readiness.check()
    assert res["checks"]["brokers"]["status"] == "degraded" and res["checks"]["brokers"]["brokers"] == {"tradovate": {"total": 1, "connected": 1}, "projectx": {"total": 1, "connected": 0}}
    assert res["checks"]["event_loop"]["status"] == "degraded"
    lw = readiness.latency_window()
    assert lw["count"] == 2 and lw["p50_ms"] == 120.0 and lw["p95_ms"] == 300.0
    pub = readiness.public_summary()
    assert pub["brokers"] == {"tradovate": "ok", "projectx": "down"} and pub["latency"]["count"] == 2 and "checks" not in pub


async def test_readyz_needs_the_metrics_token(anon_client, monkeypatch):
    r = await anon_client.get("/readyz")
    assert r.status_code == 401 and "NEXUSPRED_METRICS_TOKEN" in r.text
    monkeypatch.setenv("NEXUSPRED_METRICS_TOKEN", "tok123")
    assert metrics.enabled()
    assert (await anon_client.get("/readyz")).status_code == 401
    r = await anon_client.get("/readyz?token=tok123")
    assert r.status_code == 200 and r.json()["status"] in ("ok", "degraded") and "checks" in r.json()
    r = await anon_client.get("/readyz", headers={"Authorization": "Bearer tok123"})
    assert r.status_code == 200


async def test_readyz_is_503_when_down(anon_client, monkeypatch):
    monkeypatch.setenv("NEXUSPRED_METRICS_TOKEN", "tok123")
    readiness._lag_samples.append((0.0, 5000.0))
    r = await anon_client.get("/readyz?token=tok123")
    assert r.status_code == 503 and r.json()["checks"]["event_loop"]["status"] == "down"


async def test_status_page_is_public_and_shows_no_tenant_data(anon_client, admin, area):
    state.set_session_status("TGI-APEX-secret-login", connected=True, broker="tradovate")
    await readiness.check()
    r = await anon_client.get("/status")
    assert r.status_code == 200 and "System status" in r.text and "Tradovate" in r.text and "connected" in r.text
    assert "TGI-APEX" not in r.text and "admin@example.com" not in r.text
    r = await anon_client.get("/status", headers={"accept-language": "de"})
    assert "Systemstatus" in r.text and "Alle Systeme laufen" in r.text
    r = await anon_client.get("/api/public/status")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["brokers"] == {"tradovate": "ok"}


async def test_incidents_crud_and_public_view(trio, anon_client):
    admin, bc, us = trio
    async with _client(bc["id"]) as c:
        assert (await c.post("/api/incidents", json={"title": "x"})).status_code == 403
    async with _client(admin["id"]) as c:
        assert (await c.post("/api/incidents", json={"title": ""})).status_code == 400
        assert (await c.post("/api/incidents", json={"title": "x", "status": "burning"})).status_code == 400
        r = await c.post("/api/incidents", json={"title": "Tradovate demo logins failing", "status": "investigating", "body": "Since 09:10 UTC."})
        assert r.status_code == 200
        iid = r.json()["id"]
        r = await c.put(f"/api/incidents/{iid}", json={"title": "Tradovate demo logins failing", "status": "monitoring", "body": "Fix deployed."})
        assert r.json()["status"] == "monitoring" and len(r.json()["updates"]) == 2
        assert (await c.put("/api/incidents/inc_nope", json={"title": "x"})).status_code == 404
    r = await anon_client.get("/status")
    assert "Tradovate demo logins failing" in r.text and "Fix deployed." in r.text and "monitoring" in r.text
    assert anon_client and (await anon_client.get("/api/public/status")).json()["incidents"][0]["updates"][0]["body"] == "Since 09:10 UTC."
    async with _client(admin["id"]) as c:
        assert (await c.delete(f"/api/incidents/{iid}")).status_code == 200
        assert (await c.delete(f"/api/incidents/{iid}")).status_code == 404
    assert "No incidents" in (await anon_client.get("/status")).text


async def test_heartbeat_pings_with_the_health_result(trio, monkeypatch):
    admin, bc, us = trio
    calls = []

    class _R:
        status_code = 200

    class _Client:
        async def get(self, url, timeout=None, params=None):
            calls.append((url, params))
            return _R()
    monkeypatch.setattr(readiness.http, "client", lambda *a, **k: _Client())
    monkeypatch.setattr(readiness.security, "check_outbound_url", lambda url: None)
    async with _client(admin["id"]) as c:
        assert (await c.put("/api/platform/heartbeat", json={"url": "http://plain", "interval": 60})).status_code == 400
        r = await c.put("/api/platform/heartbeat", json={"url": "https://hc-ping.com/abc", "interval": 5})
        assert r.status_code == 200 and r.json()["interval"] == 30 and r.json()["pinged"] is True and r.json()["ok"] is True
        assert calls[-1] == ("https://hc-ping.com/abc", None)
        r = await c.put("/api/platform/heartbeat", json={"url": "https://uptime.example/push/x", "interval": 120})
        assert calls[-1] == ("https://uptime.example/push/x", {"status": "ok"}) or calls[-1][1]["status"] in ("ok", "degraded")
    readiness._lag_samples.append((0.0, 9000.0))                                     # down → healthchecks gets /fail
    readiness.save_heartbeat("https://hc-ping.com/abc", 60)
    await readiness.heartbeat_once()
    assert calls[-1][0] == "https://hc-ping.com/abc/fail"
    readiness.save_heartbeat("", 60)
    assert await readiness.heartbeat_once() is False and readiness.heartbeat_status()["url"] == ""
    async with _client(bc["id"]) as c:
        assert (await c.get("/api/platform/health")).status_code == 403

"""Deep health (alpha.96): what ``/healthz`` never said.

``/healthz`` answers "ok" while the process is up. :func:`check` looks at what
actually has to work: the database writes, the disk has room, a verified
backup is recent, at least one broker session per broker is connected, the
history writer keeps up, the event loop is not stalling, the outbox is not
piling up failures. The result is ``ok`` / ``degraded`` / ``down`` with one
line per check, served at ``/readyz`` (metrics token or admin session) and
summarised on the public status page.

Also here: the rolling signal-latency window (p50 / p95 of the last hour,
fed by the event bus), the event-loop lag sampler, the platform heartbeat
(an outbound ping to healthchecks.io / Uptime Kuma that stops when the bridge
is down — the failure it cannot report itself) and the admin-kept incident
list shown on the status page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config, db, http, security, state
from . import events as bus

log = logging.getLogger("nexuspred.readiness")

DISK_WARN_MB = 500
DISK_DOWN_MB = 100
LAG_WARN_MS = 100.0
LAG_DOWN_MS = 1000.0
HISTORY_WARN = 1000
LATENCY_WINDOW_S = 3600
HEARTBEAT_KEY = "platform:heartbeat"
INCIDENTS_KEY = "incidents"
STARTED_AT = time.time()

_lag_samples: deque[tuple[float, float]] = deque(maxlen=120)     # (ts, lag_ms), one per second
_latency: deque[tuple[float, float]] = deque(maxlen=20000)       # (ts, seconds)
_heartbeat_state: dict[str, Any] = {"last_at": None, "ok": None, "error": ""}
_last_result: Optional[dict[str, Any]] = None


def reset() -> None:
    global _last_result, _latency_state
    _latency_state = "ok"
    _lag_samples.clear()
    _latency.clear()
    _heartbeat_state.update({"last_at": None, "ok": None, "error": ""})
    _last_result = None


# ------------------------------------------------------------- samplers
def _on_signal_done(data: dict[str, Any]) -> None:
    secs = data.get("seconds")
    if isinstance(secs, (int, float)) and secs >= 0:
        _latency.append((time.time(), float(secs)))


bus.subscribe("signal.done", _on_signal_done)


def latency_window(now: Optional[float] = None) -> dict[str, Any]:
    """p50 / p95 / max / count of the signal→done latency over the last hour."""
    now = now or time.time()
    cutoff = now - LATENCY_WINDOW_S
    vals = sorted(s for ts, s in _latency if ts >= cutoff)
    if not vals:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}

    def pct(p: float) -> float:
        return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]
    return {"count": len(vals), "p50_ms": round(pct(0.5) * 1000, 1), "p95_ms": round(pct(0.95) * 1000, 1), "max_ms": round(vals[-1] * 1000, 1)}


async def lag_loop() -> None:
    """Sleep one second at a time and record how late the wake-up was."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(1.0)
        lag_ms = max(0.0, (time.perf_counter() - t0 - 1.0) * 1000)
        _lag_samples.append((time.time(), lag_ms))


def loop_lag() -> dict[str, Any]:
    if not _lag_samples:
        return {"avg_ms": 0.0, "max_ms": 0.0, "samples": 0}
    vals = [v for _, v in _lag_samples]
    return {"avg_ms": round(sum(vals) / len(vals), 1), "max_ms": round(max(vals), 1), "samples": len(vals)}


# ------------------------------------------------------------- checks
def _brokers() -> dict[str, dict[str, int]]:
    """Per broker across every workspace: sessions total / connected."""
    out: dict[str, dict[str, int]] = {}
    for aid in list(state._areas.keys()):
        for s in state.session_statuses_for(aid):
            b = str(s.get("broker") or "tradovate")
            d = out.setdefault(b, {"total": 0, "connected": 0})
            d["total"] += 1
            if s.get("connected"):
                d["connected"] += 1
    return out


def _streams() -> int:
    return sum(len(st.subscribers) for st in list(state._areas.values()))


async def check() -> dict[str, Any]:
    """Run every check; the worst one decides the overall status."""
    global _last_result
    checks: dict[str, dict[str, Any]] = {}

    def put(name: str, status: str, detail: str, **extra: Any) -> None:
        checks[name] = {"status": status, "detail": detail, **extra}

    # database: a real write
    try:
        await asyncio.to_thread(db.meta_set, "readyz:ping", datetime.now(timezone.utc).isoformat())
        put("database", "ok", "writable")
    except Exception as exc:  # noqa: BLE001
        put("database", "down", f"write failed: {exc}")
    # disk
    from . import backups
    free = backups.free_bytes()
    if free is None:
        put("disk", "degraded", "free space unknown")
    else:
        mb = free // (1 << 20)
        put("disk", "down" if mb < DISK_DOWN_MB else "degraded" if mb < DISK_WARN_MB else "ok", f"{mb} MB free", free_mb=mb)
    # backup
    bst = backups.status()
    if not bst["enabled"]:
        put("backup", "degraded", "automatic backups are off", age_hours=bst["age_hours"])
    elif bst["age_hours"] is None:
        put("backup", "degraded" if db.meta_get("backups:last_day") else "ok", "no verified backup yet" if db.meta_get("backups:last_day") else f"first backup at {bst['next_at'][11:16]} UTC", age_hours=None)
    else:
        put("backup", "degraded" if bst["stale"] else "ok", f"last verified {bst['age_hours']} h ago", age_hours=bst["age_hours"])
    # brokers
    brokers = _brokers()
    if not brokers:
        put("brokers", "ok", "no broker sessions configured", brokers={})
    else:
        worst = "ok"
        parts = []
        for b, d in sorted(brokers.items()):
            s = "ok" if d["connected"] == d["total"] else "degraded" if d["connected"] else "down"
            worst = max(worst, s, key=lambda x: ("ok", "degraded", "down").index(x))
            parts.append(f"{b} {d['connected']}/{d['total']}")
        put("brokers", "degraded" if worst == "down" and len(brokers) > 1 else worst, ", ".join(parts), brokers=brokers)
    # history writer backlog
    from . import history
    backlog = history.backlog()
    put("history", "degraded" if backlog > HISTORY_WARN else "ok", f"{backlog} rows queued", backlog=backlog)
    # event loop
    lag = loop_lag()
    put("event_loop", "down" if lag["max_ms"] > LAG_DOWN_MS else "degraded" if lag["max_ms"] > LAG_WARN_MS else "ok",
        f"lag avg {lag['avg_ms']} ms, max {lag['max_ms']} ms (last {lag['samples']} s)", **lag)
    # outbox
    counts = db.outbox_counts()
    put("mail", "degraded" if counts["failed"] else "ok", f"{counts['pending']} pending, {counts['failed']} failed", **counts)
    # live streams
    put("streams", "ok", f"{_streams()} live dashboard stream(s)", streams=_streams())
    # latency
    lw = latency_window()
    put("latency", "ok", f"p95 {lw['p95_ms']} ms over {lw['count']} signal(s)" if lw["count"] else "no signals in the last hour", **lw)
    # canary (alpha.99)
    from . import canary
    cs = canary.status()
    if cs["enabled"]:
        last = cs["last"] or {}
        put("canary", "down" if last and not last.get("ok") else "degraded" if last.get("slow") else "ok",
            f"{last.get('detail')} in {last.get('ms')} ms" if last else "no run yet", **{k: v for k, v in cs.items() if k != "last"})
    order = ("ok", "degraded", "down")
    overall = max((c["status"] for c in checks.values()), key=order.index)
    _last_result = {"status": overall, "version": config.get_version(), "uptime_s": int(time.time() - STARTED_AT),
                    "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks}
    return _last_result


def last_result() -> Optional[dict[str, Any]]:
    return _last_result


# ------------------------------------------------------------- heartbeat
def heartbeat_config() -> dict[str, Any]:
    raw = db.meta_get(HEARTBEAT_KEY)
    cfg = {"url": "", "interval": 60}
    if raw:
        try:
            cfg.update({k: v for k, v in json.loads(raw).items() if k in cfg})
        except ValueError:
            pass
    return cfg


def save_heartbeat(url: str, interval: int) -> dict[str, Any]:
    url = str(url or "").strip()
    if url:
        if not url.startswith("https://"):
            raise ValueError("the heartbeat URL must start with https://")
        problem = security.check_outbound_url(url)
        if problem:
            raise ValueError(f"heartbeat URL rejected: {problem}")
    interval = max(30, min(3600, int(interval or 60)))
    db.meta_set(HEARTBEAT_KEY, json.dumps({"url": url, "interval": interval}))
    return {"url": url, "interval": interval}


def heartbeat_status() -> dict[str, Any]:
    return {**heartbeat_config(), **_heartbeat_state}


async def heartbeat_once() -> bool:
    cfg = heartbeat_config()
    if not cfg["url"]:
        return False
    url = cfg["url"]
    # What the name resolves to *now*, not at save time: a short-TTL record can
    # point an accepted host at the internal network after the fact.
    problem = await asyncio.to_thread(security.check_outbound_url, url)
    if problem:
        _heartbeat_state.update({"last_at": datetime.now(timezone.utc).isoformat(), "ok": False,
                                 "error": f"heartbeat URL rejected: {problem}"})
        log.warning("platform heartbeat not sent: %s", problem)
        return False
    res = await check()
    if res["status"] == "down" and "hc-ping.com" in url:
        url = url.rstrip("/") + "/fail"                      # healthchecks.io: an explicit failure beats silence
    try:
        r = await http.client("outbound").get(url, timeout=10.0, params={"status": res["status"]} if "hc-ping.com" not in url else None)
        ok = r.status_code < 400
        _heartbeat_state.update({"last_at": datetime.now(timezone.utc).isoformat(), "ok": ok, "error": "" if ok else f"HTTP {r.status_code}"})
        return ok
    except Exception as exc:  # noqa: BLE001
        _heartbeat_state.update({"last_at": datetime.now(timezone.utc).isoformat(), "ok": False, "error": str(exc)[:200]})
        return False


async def heartbeat_loop() -> None:
    while True:
        cfg = heartbeat_config()
        try:
            if cfg["url"]:
                await heartbeat_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("platform heartbeat failed: %s", exc)
        await asyncio.sleep(cfg["interval"])


# ------------------------------------------------------------- incidents
INCIDENT_STATES = ("investigating", "identified", "monitoring", "resolved")


def incidents(include_resolved_days: int = 7) -> list[dict[str, Any]]:
    raw = db.meta_get(INCIDENTS_KEY)
    rows: list[dict[str, Any]] = []
    if raw:
        try:
            rows = [r for r in json.loads(raw) if isinstance(r, dict)]
        except ValueError:
            rows = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=include_resolved_days)).isoformat()
    return sorted([r for r in rows if r.get("status") != "resolved" or r.get("updated_at", "") >= cutoff],
                  key=lambda r: r.get("updated_at", ""), reverse=True)


def _all_incidents() -> list[dict[str, Any]]:
    raw = db.meta_get(INCIDENTS_KEY)
    try:
        return [r for r in json.loads(raw) if isinstance(r, dict)] if raw else []
    except ValueError:
        return []


def save_incident(data: dict[str, Any], author: str, incident_id: Optional[str] = None) -> dict[str, Any]:
    rows = _all_incidents()
    title = str(data.get("title") or "").strip()[:140]
    body = str(data.get("body") or "").strip()[:2000]
    status = str(data.get("status") or "investigating")
    if not title:
        raise ValueError("title is required")
    if status not in INCIDENT_STATES:
        raise ValueError(f"status must be one of {', '.join(INCIDENT_STATES)}")
    now = datetime.now(timezone.utc).isoformat()
    if incident_id:
        for r in rows:
            if r["id"] == incident_id:
                r.update({"title": title, "status": status, "updated_at": now})
                if body:
                    r.setdefault("updates", []).append({"at": now, "status": status, "body": body, "by": author})
                break
        else:
            raise KeyError(incident_id)
        inc = next(r for r in rows if r["id"] == incident_id)
    else:
        inc = {"id": "inc_" + secrets.token_urlsafe(6), "title": title, "status": status, "created_at": now, "updated_at": now,
               "updates": [{"at": now, "status": status, "body": body or title, "by": author}]}
        rows.append(inc)
    # keep the last 50 resolved
    resolved = [r for r in rows if r["status"] == "resolved"]
    if len(resolved) > 50:
        drop = {r["id"] for r in sorted(resolved, key=lambda r: r["updated_at"])[:len(resolved) - 50]}
        rows = [r for r in rows if r["id"] not in drop]
    db.meta_set(INCIDENTS_KEY, json.dumps(rows))
    return inc


def delete_incident(incident_id: str) -> bool:
    rows = _all_incidents()
    keep = [r for r in rows if r["id"] != incident_id]
    if len(keep) == len(rows):
        return False
    db.meta_set(INCIDENTS_KEY, json.dumps(keep))
    return True


# ------------------------------------------------------------- public summary
def public_summary() -> dict[str, Any]:
    """What the status page shows: no tenant data, no hostnames."""
    res = _last_result or {"status": "ok", "checks": {}}
    brokers = (res.get("checks", {}).get("brokers") or {}).get("brokers") or {}
    bro = {b: ("ok" if d["connected"] == d["total"] else "degraded" if d["connected"] else "down") for b, d in brokers.items()}
    lw = latency_window()
    return {"status": res.get("status", "ok"), "version": config.get_version(), "uptime_s": int(time.time() - STARTED_AT),
            "checked_at": res.get("checked_at"), "brokers": bro, "latency": lw,
            "incidents": [{"id": i["id"], "title": i["title"], "status": i["status"], "created_at": i["created_at"], "updated_at": i["updated_at"],
                           "updates": [{"at": u["at"], "status": u["status"], "body": u["body"]} for u in i.get("updates", [])]} for i in incidents()]}


_prev_status: Optional[str] = None
_update_checked_day = ""


async def _notify_transition(res: dict[str, Any]) -> None:
    """alpha.97: admins hear when the overall state changes (once per change)."""
    global _prev_status
    st = res["status"]
    if _prev_status is None:
        _prev_status = st
        return
    if st == _prev_status:
        return
    prev, _prev_status = _prev_status, st
    from . import alerts
    bad = [f"{k}: {c['detail']}" for k, c in res["checks"].items() if c["status"] != "ok"]
    if st == "ok":
        await alerts.notify_admins("notice", {"title": "Bridge healthy again", "message": f"All checks pass again (was {prev}).", "button": "Open Platform", "url": (config.PUBLIC_URL or "") + "/#/settings/platform"},
                                   inbox=("health.recovered", "info", "Bridge healthy again", "/#/settings/platform"), mail=False)
    else:
        await alerts.notify_admins("notice", {"title": f"Bridge {st}", "message": "; ".join(bad)[:800] or st, "button": "Open Platform", "url": (config.PUBLIC_URL or "") + "/#/settings/platform"},
                                   inbox=("health.degraded", "critical" if st == "down" else "warn", f"Bridge {st}: " + (bad[0] if bad else st), "/#/settings/platform"),
                                   mail=(st == "down" or prev == "ok"))


async def _daily_update_check() -> None:
    """alpha.97: once a day, tell the admins when a newer version is on GitHub (once per version)."""
    global _update_checked_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _update_checked_day == today:
        return
    _update_checked_day = today
    from . import alerts, updater
    try:
        res = await updater.check_for_update()
    except Exception as exc:  # noqa: BLE001
        log.info("update check skipped: %s", exc)
        return
    latest = str(res.get("latest_version") or "")
    if not res.get("update_available") or not latest or db.meta_get("update_notified") == latest:
        return
    db.meta_set("update_notified", latest)
    await alerts.notify_admins("notice", {"title": f"Update available: {latest}", "message": f"Fluxbridge {latest} is available (you run {res.get('current_version')}). Apply it under Settings → Updates.",
                                          "button": "Open Updates", "url": (config.PUBLIC_URL or "") + "/#/settings/updates"},
                               inbox=("update.available", "info", f"Update available: {latest}", "/#/settings/updates"))


_latency_state = "ok"


async def _latency_watch() -> None:
    """alpha.99: p95 signal latency or event-loop lag over the platform thresholds → admins, once per state change."""
    global _latency_state
    from . import alerts, platform
    cfg = platform.get_config()
    lw = latency_window()
    lag = loop_lag()
    bad = []
    if lw["count"] >= 3 and lw["p95_ms"] is not None and lw["p95_ms"] > cfg["latency_p95_warn_ms"]:
        bad.append(f"signal latency p95 {lw['p95_ms']} ms over {lw['count']} signals (threshold {cfg['latency_p95_warn_ms']} ms)")
    if lag["max_ms"] > cfg["loop_lag_warn_ms"]:
        bad.append(f"event-loop lag max {lag['max_ms']} ms (threshold {cfg['loop_lag_warn_ms']} ms)")
    st = "slow" if bad else "ok"
    if st == _latency_state:
        return
    _latency_state = st
    if st == "slow":
        await alerts.notify_admins("notice", {"title": "Latency over threshold", "message": "; ".join(bad), "button": "Open Platform", "url": (config.PUBLIC_URL or "") + "/#/settings/platform"},
                                   inbox=("latency.slow", "warn", "Latency over threshold: " + bad[0], "/#/settings/platform"))
    else:
        await alerts.notify_admins("notice", {"title": "Latency back to normal", "message": f"p95 {lw['p95_ms']} ms, loop lag max {lag['max_ms']} ms."},
                                   inbox=("latency.ok", "info", "Latency back to normal", "/#/settings/platform"), mail=False)


async def readiness_loop() -> None:
    """Refresh the cached result every 30 s so the public page never triggers a check."""
    while True:
        try:
            res = await check()
            await _notify_transition(res)
            await _latency_watch()
            await _daily_update_check()
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness check failed: %s", exc)
        await asyncio.sleep(30.0)


# ------------------------------------------------------------- per-workspace readiness + onboarding (alpha.97)
def _fix(status: str, label: str, detail: str, url: str, key: str) -> dict[str, Any]:
    return {"key": key, "status": status, "label": label, "detail": detail, "url": url}


def workspace_checks(area_id: int, user: dict[str, Any]) -> dict[str, Any]:
    """The "ready to trade" card: what a User needs, plus what the role adds."""
    from . import backups, mailer, watchdog, web
    from . import copy as cp
    role = web.role_of(user)
    s = config.load_settings(area_id=area_id)
    sessions = state.session_statuses_for(area_id)
    logins = [t for t in (s.get("token_accounts") or []) if t.get("enabled", True)]
    checks: list[dict[str, Any]] = []
    connected = sum(1 for x in sessions if x.get("connected"))
    if not logins:
        checks.append(_fix("fail", "Broker login", "no broker login yet", "/#/settings/accounts", "login"))
    elif not connected:
        checks.append(_fix("fail", "Broker login", f"{len(logins)} login(s), none connected", "/#/settings/accounts", "login"))
    else:
        checks.append(_fix("ok", "Broker login", f"{connected}/{len(sessions) or len(logins)} connected", "/#/settings/accounts", "login"))
    accounts = [a for t in logins for a in (t.get("accounts") or []) if a.get("enabled", True)]
    checks.append(_fix("ok" if accounts else "fail", "Trade accounts", f"{len(accounts)} enabled" if accounts else "no trade account enabled", "/#/settings/accounts", "accounts"))
    checks.append(_fix("ok" if s.get("trading_enabled") else "warn", "Trading switch", "on" if s.get("trading_enabled") else "off — signals are logged only", "/#/", "trading"))
    guarded = sum(1 for a in accounts if (a.get("risk") or {}).get("daily_loss_limit") or (a.get("risk") or {}).get("flatten_at"))
    checks.append(_fix("ok" if guarded else "warn", "Risk guard", f"{guarded}/{len(accounts)} account(s) guarded" if accounts else "no accounts", "/#/settings/accounts", "risk"))
    roll = state.rollover_warnings(area_id)
    checks.append(_fix("warn" if roll else "ok", "Symbol mapping", f"{len(roll)} contract(s) due to roll" if roll else "no rollover due", "/#/settings/symbols", "rollover"))
    dl = db.delivery_status(area_id)
    any_ok = any(v.get("last_ok") for v in dl.values())
    degraded = [k for k, v in dl.items() if v.get("degraded")]
    channels_on = bool(s.get("alert_push_enabled", True) and db.list_push_subscriptions(area_id)) or bool(s.get("alert_discord_enabled") and s.get("alert_discord_webhook_url")) or bool(s.get("alert_email_enabled") and s.get("alert_email_to"))
    checks.append(_fix("fail" if degraded else ("ok" if any_ok else ("warn" if channels_on else "warn")), "Alert channel",
                       f"{', '.join(degraded)} failing" if degraded else ("delivered" if any_ok else ("enabled, never tested" if channels_on else "no channel enabled")), "/#/settings/alerts", "alerts"))
    if s.get("heartbeat_url"):
        hb = watchdog.status(area_id)
        checks.append(_fix("ok" if hb.get("ok") else "warn", "External watchdog", "pinging" if hb.get("ok") else (hb.get("error") or "no ping yet"), "/#/settings/alerts", "watchdog"))
    checks.append(_fix("ok" if user.get("totp_enabled") else "warn", "Two-factor", "on" if user.get("totp_enabled") else "off", "/#/settings/security", "2fa"))
    if role in ("broadcaster", "admin"):
        listings = [w for w in (s.get("webhooks") or []) if (w.get("sharing") or {}).get("enabled")]
        groups = [g for g in cp.load_groups(area_id) if (g.get("sharing") or {}).get("enabled")]
        n = len(listings) + len(groups)
        checks.append(_fix("ok" if n else "warn", "Marketplace listing", f"{n} published" if n else "nothing published yet", "/#/webhooks", "listing"))
        paused = [x for w in listings for x in db.list_subscribers(area_id, w["id"]) if x.get("status") == "paused" or not x.get("enabled")]
        checks.append(_fix("warn" if paused else "ok", "Subscribers", f"{len(paused)} paused" if paused else "all subscribers running", "/#/webhooks", "subscribers"))
        sized = sum(1 for w in listings if w.get("sized_for_k"))
        if listings:
            checks.append(_fix("ok" if sized == len(listings) else "warn", "Sizing hint", f"{sized}/{len(listings)} listings state their account size", "/#/webhooks", "sizing"))
    if role == "admin":
        bst = backups.status()
        checks.append(_fix("fail" if bst["stale"] and bst["enabled"] and bst["count"] else ("warn" if not bst["enabled"] or bst["age_hours"] is None else "ok"), "Backup",
                           "off" if not bst["enabled"] else (f"verified {bst['age_hours']} h ago" if bst["age_hours"] is not None else "no verified backup yet"), "/#/settings/backups", "backup"))
        checks.append(_fix("ok" if mailer.configured() else "warn", "Platform mailer", mailer.get_config()["provider"] if mailer.configured() else "off — invites and resets fall back to workspace SMTP", "/#/settings/platform", "mailer"))
        lr = _last_result or {}
        disk = (lr.get("checks") or {}).get("disk") or {}
        checks.append(_fix("ok" if disk.get("status", "ok") == "ok" else disk.get("status", "ok").replace("degraded", "warn").replace("down", "fail"), "Disk", disk.get("detail", "unknown"), "/#/settings/platform", "disk"))
    ok = sum(1 for c in checks if c["status"] == "ok")
    return {"role": role, "ok": ok, "total": len(checks), "checks": checks}


ONBOARDING = {
    "user": [("login", "Connect a broker login", "/#/settings/accounts"), ("risk", "Set a risk guard on an account", "/#/settings/accounts"),
             ("alerts", "Test an alert channel", "/#/settings/alerts"), ("signal", "Create a webhook or subscribe on the marketplace", "/#/webhooks")],
    "broadcaster": [("login", "Connect a broker login", "/#/settings/accounts"), ("record", "Import your track record (journal)", "/#/journal"),
                    ("listing", "Publish a webhook or copy group", "/#/webhooks"), ("sizing", "State the account size your signals are sized for", "/#/webhooks"),
                    ("alerts", "Test an alert channel", "/#/settings/alerts")],
    "admin": [("mailer", "Set up the platform mailer", "/#/settings/platform"), ("backup", "Switch on off-site backups", "/#/settings/backups"),
              ("heartbeat", "Point a monitor at the heartbeat", "/#/settings/platform"), ("status", "Open the public status page once", "/status"),
              ("login", "Connect a broker login", "/#/settings/accounts")],
}


def onboarding(area_id: int, user: dict[str, Any], checks: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The first-days checklist per role, with done flags computed from the workspace."""
    from . import backups, mailer, web
    role = web.role_of(user)
    ch = {c["key"]: c for c in (checks or workspace_checks(area_id, user))["checks"]}
    s = config.load_settings(area_id=area_id)
    done: dict[str, bool] = {
        "login": ch.get("login", {}).get("status") == "ok",
        "risk": ch.get("risk", {}).get("status") == "ok",
        "alerts": ch.get("alerts", {}).get("status") == "ok",
        "signal": bool(s.get("webhooks")) or bool(db.list_subscriptions(area_id)),
        "record": bool(db.journal_accounts(area_id)) if hasattr(db, "journal_accounts") else False,
        "listing": ch.get("listing", {}).get("status") == "ok",
        "sizing": ch.get("sizing", {}).get("status", "ok") == "ok" and ch.get("listing", {}).get("status") == "ok",
        "mailer": mailer.configured(),
        "backup": backups.get_config()["offsite"] != "off",
        "heartbeat": bool(heartbeat_config()["url"]),
        "status": db.meta_get("incidents") is not None or bool(db.meta_get(f"onboarding_status_seen:{user['id']}")),
    }
    steps = [{"key": k, "label": label, "url": url, "done": bool(done.get(k))} for k, label, url in ONBOARDING.get(role, ONBOARDING["user"])]
    dismissed = db.meta_get(f"onboarding_dismissed:{user['id']}") == "1"
    return {"role": role, "steps": steps, "done": sum(1 for x in steps if x["done"]), "total": len(steps),
            "complete": all(x["done"] for x in steps), "dismissed": dismissed}


"""The Broadcaster's business (alpha.98).

* **Announcements** — a publisher writes to the subscribers of one listing (or
  all): inbox + push on the subscriber's side, mail for those who keep
  "broadcaster announcements" on, at most :data:`ANNOUNCE_PER_DAY` a day.
* **Tiers** — Bronze / Silver / Gold from facts the bridge knows: how long the
  listing has been published, how many verified trades, how many subscribers,
  how often the fan-out fails. No ratings, no self-description.
* **Cockpit** — one view of the business: subscribers per status, revenue from
  Stripe records, growth per week, signals and latency, the track record.
* **Application** — what the admin sees next to a Broadcaster request: the
  requester's note and their track record.
* **Trial period** — a Broadcaster role with an expiry: three days before it
  ends the admins get a summary and a one-click extension; when it lapses the
  role falls back to User through the usual demotion.
* **Weekly report** — Monday morning in the workspace's timezone: P&L, hit
  rate, best and worst session, fees, open risks; Broadcasters add subscriber
  development and revenue, admins the platform numbers.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config, context, db, mailer, releases, state, track_record, web
from . import copy as cp
from . import marketplace as mp

log = logging.getLogger("nexuspred.broadcaster")

ANNOUNCE_PER_DAY = 3
ANNOUNCE_TITLE_MAX = 120
ANNOUNCE_BODY_MAX = 2000
TRIAL_WARN_DAYS = 3
WEEKLY_HOUR = 7
LOOP_TICK_S = 600.0
TIERS = ("", "bronze", "silver", "gold")


# ------------------------------------------------------------------ announcements
def _listing_title(publisher_area_id: int, key: str) -> str:
    if key.startswith("copy:"):
        g, sh = cp.find_published(publisher_area_id, key[5:])
        return (sh.get("title") or (g or {}).get("name") or key) if g else key
    wh, sh = mp.find_published(publisher_area_id, key)
    return (sh.get("title") or (wh or {}).get("name") or key) if wh else key


def listing_keys(publisher_area_id: int) -> list[tuple[str, str]]:
    """(key, title) of every published listing of the workspace."""
    out = []
    s = config.load_settings(area_id=publisher_area_id)
    for wh in s.get("webhooks") or []:
        sh = mp.sharing_of(wh)
        if sh.get("enabled"):
            out.append((str(wh.get("id")), sh.get("title") or wh.get("name") or "Signal"))
    for g in cp.load_groups(publisher_area_id):
        sh = cp.sharing_of(g)
        if sh.get("enabled"):
            out.append((f"copy:{g['id']}", sh.get("title") or g.get("name") or "Copy group"))
    return out


async def announce(publisher_area_id: int, publisher: dict[str, Any], title: str, body: str, listing_key: str = "") -> dict[str, Any]:
    """Send one announcement. Raises ValueError on bad input or the daily cap."""
    from . import alerts
    title, body = str(title or "").strip(), str(body or "").strip()
    if not title or not body:
        raise ValueError("title and text are required")
    if len(title) > ANNOUNCE_TITLE_MAX or len(body) > ANNOUNCE_BODY_MAX:
        raise ValueError(f"at most {ANNOUNCE_TITLE_MAX} characters of title and {ANNOUNCE_BODY_MAX} of text")
    if listing_key and listing_key not in {k for k, _ in listing_keys(publisher_area_id)}:
        raise ValueError("that listing is not published")
    if db.announcements_today(publisher_area_id) >= ANNOUNCE_PER_DAY:
        raise ValueError(f"at most {ANNOUNCE_PER_DAY} announcements a day")
    recipients = db.publisher_subscribers(publisher_area_id, listing_key or None)
    label = _listing_title(publisher_area_id, listing_key) if listing_key else ""
    head = f"{publisher['email']}" + (f" · {label}" if label else "")
    url = "/#/subscriptions"
    mailed = 0
    for r in recipients:
        try:
            await alerts.subscriber_announcement(int(r["area_id"]), head, title, body, url=url)
        except Exception as exc:  # noqa: BLE001
            log.warning("announcement inbox for area %s failed: %s", r["area_id"], exc)
        uid = r.get("user_id")
        if uid and releases.wants(int(uid), "announcements") and mailer.can_send(int(r["area_id"])):
            lang = mailer.lang_for_area(int(r["area_id"]))
            mailer.send_template(r["email"], "announcement", {"publisher": publisher["email"], "listing": label or ("all listings" if lang == "en" else "alle Angebote"),
                                                              "title": title, "body": body, "url": (config.PUBLIC_URL or "") + url,
                                                              "unsubscribe_url": releases.unsubscribe_url(int(uid), "announcements")},
                                 lang=lang, area_id=int(r["area_id"]))
            mailed += 1
    row = db.add_announcement(publisher_area_id, listing_key, title, body, len(recipients), mailed)
    db.log_action(publisher["id"], publisher["email"], "announce", label or "all listings", f"{len(recipients)} subscriber(s), {mailed} mailed")
    with context.use_area(publisher_area_id):
        state.log_event("info", f"Announcement '{title}' sent to {len(recipients)} subscriber(s)" + (f" of {label}" if label else ""))
    return {**row, "recipients": len(recipients), "mailed": mailed, "left_today": max(0, ANNOUNCE_PER_DAY - db.announcements_today(publisher_area_id))}


# ------------------------------------------------------------------ tiers
def _days_since(iso: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not iso:
        return None
    try:
        return ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(str(iso))).total_seconds() / 86400
    except ValueError:
        return None


def tier_of(record: Optional[dict[str, Any]], subscribers: int, published_at: Optional[str], error_rate: Optional[float] = None,
            now: Optional[datetime] = None) -> str:
    """Bronze / Silver / Gold from facts; "" when none applies.

    * bronze — published 7+ days, 10+ trades on record (or a subscriber)
    * silver — published 30+ days, 30+ broker-verified trades, no fan-out trouble (error rate under 5 %)
    * gold   — published 90+ days, 100+ verified trades, 90+ days of trading history, 5+ subscribers, error rate under 2 %
    """
    rec = record or {}
    days_pub = _days_since(published_at, now) or 0.0
    trades = int(rec.get("trades") or 0)
    verified = bool(rec.get("verified")) and trades > 0
    hist_days = _days_since(rec.get("first_trade_at"), now) or 0.0
    err = float(error_rate) if error_rate is not None else 0.0
    if days_pub >= 90 and verified and trades >= 100 and hist_days >= 90 and subscribers >= 5 and err < 0.02:
        return "gold"
    if days_pub >= 30 and verified and trades >= 30 and err < 0.05:
        return "silver"
    if days_pub >= 7 and (trades >= 10 or subscribers >= 1):
        return "bronze"
    return ""


def error_rate(publisher_area_id: int, webhook_id: str) -> Optional[float]:
    """Errored share of the last 30 days' signal outcomes (webhook listings only)."""
    try:
        st = db.signal_stats(publisher_area_id, webhook_id, track_record._iso_days_ago(30))
    except Exception:  # noqa: BLE001
        return None
    total = int(st.get("executed") or 0) + int(st.get("errors") or 0)
    if not total:
        return None
    return int(st.get("errors") or 0) / total


# ------------------------------------------------------------------ cockpit
def _week_key(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _recent_weeks(n: int = 8, now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = []
    for i in range(n - 1, -1, -1):
        d = now - timedelta(weeks=i)
        y, w, _ = d.isocalendar()
        out.append(f"{y}-W{w:02d}")
    return out


def cockpit(publisher_area_id: int) -> dict[str, Any]:
    weeks = _recent_weeks()
    listings = []
    totals: dict[str, Any] = {"subscribers": 0, "active": 0, "pending": 0, "paused": 0, "unpaid": 0, "revenue": defaultdict(int), "listings": 0}
    growth = {w: 0 for w in weeks}
    payments = db.list_payments(publisher_area_id=publisher_area_id)
    for key, title in listing_keys(publisher_area_id):
        subs = db.list_subscribers(publisher_area_id, key)
        by = {"active": 0, "pending": 0, "paused": 0, "unpaid": 0}
        for s in subs:
            st = str(s.get("status") or "active")
            if not s.get("enabled") and st == "active":
                st = "paused"
            by[st if st in by else "active"] += 1
            wk = _week_key(str(s.get("created_at") or ""))
            if wk in growth:
                growth[wk] += 1
        rev: dict[str, int] = defaultdict(int)
        for p in payments:
            if p.get("webhook_id") == key and p.get("status") in db.payments.PAID and p.get("price_cents"):
                rev[str(p.get("currency") or "usd")] += int(p["price_cents"])
        for cur, cents in rev.items():
            totals["revenue"][cur] += cents
        if key.startswith("copy:"):
            g, sh = cp.find_published(publisher_area_id, key[5:])
            rec = track_record.copy_record(publisher_area_id, g) if g else None
            err = None
            lat = None
            sig = None
        else:
            wh, sh = mp.find_published(publisher_area_id, key)
            rec = track_record.webhook_record(publisher_area_id, wh) if wh else None
            err = error_rate(publisher_area_id, key)
            lat = track_record.latency_summary(db.signal_latencies(publisher_area_id, key))
            sig = db.signal_stats(publisher_area_id, key, track_record._iso_days_ago(30))
        item = {"key": key, "title": title, "kind": "copy" if key.startswith("copy:") else "webhook", "subscribers": len(subs), **by,
                "revenue": dict(rev), "record": rec, "error_rate": err, "latency": lat, "signals_30d": sig,
                "tier": tier_of(rec, len(subs), (sh or {}).get("published_at"), err), "published_at": (sh or {}).get("published_at"),
                "paid": bool((sh or {}).get("price_cents")), "price_cents": int((sh or {}).get("price_cents") or 0)}
        listings.append(item)
        totals["subscribers"] += len(subs)
        for k in by:
            totals[k] += by[k]
        totals["listings"] += 1
    totals["revenue"] = dict(totals["revenue"])
    return {"totals": totals, "listings": listings, "growth": [{"week": w, "new": growth[w]} for w in weeks],
            "announcements": db.list_announcements(publisher_area_id, 20), "announce_left_today": max(0, ANNOUNCE_PER_DAY - db.announcements_today(publisher_area_id)),
            "listing_keys": [{"key": k, "title": t} for k, t in listing_keys(publisher_area_id)]}


# ------------------------------------------------------------------ application (admin view of a request)
def application(user: dict[str, Any]) -> dict[str, Any]:
    area = db.user_primary_area(user["id"])
    rec: dict[str, Any] = {}
    if area:
        trades = db.list_journal_trades(area)
        rec = track_record.compact(track_record.summarize_trades(trades, track_record._zone(area), detail=False))
        rec["days_active"] = round(_days_since(rec.get("first_trade_at")) or 0)
    return {"user_id": user["id"], "email": user["email"], "role": web.role_of(user), "role_request": user.get("role_request") or "",
            "requested_at": user.get("role_requested_at") or "", "note": user.get("role_note") or {}, "record": rec,
            "member_since": user.get("created_at"), "subscriptions": len(db.list_subscriptions(area)) if area else 0}


# ------------------------------------------------------------------ trial period
async def trial_check(now: Optional[datetime] = None) -> dict[str, int]:
    """Warn the admins three days before a trial Broadcaster role lapses; demote when it has."""
    from . import alerts, roles
    now = now or datetime.now(timezone.utc)
    out = {"warned": 0, "expired": 0}
    for u in db.list_users():
        exp = str(u.get("role_expires_at") or "")
        if not exp or web.role_of(u) != "broadcaster":
            continue
        try:
            until = datetime.fromisoformat(exp)
        except ValueError:
            continue
        area = db.user_primary_area(u["id"])
        if until <= now:
            effects = await roles.demote_publisher(area) if area else {}
            db.set_role(u["id"], "user", expires_at="")
            db.log_action(None, "system", "role_set", u["email"], f"broadcaster → user (trial ended{', ' + str(effects) if effects else ''})")
            if area and mailer.can_send(area):
                lang = mailer.lang_for_area(area)
                mailer.send_template(u["email"], "role_changed", {**mailer.role_ctx("user", lang, "Fluxbridge"), "url": (config.PUBLIC_URL or "") + "/"}, lang=lang, area_id=area)
            await alerts.notify_admins("notice", {"title": f"Trial ended: {u['email']}", "message": f"The Broadcaster trial of {u['email']} ended; the role fell back to User and their listings were unpublished.",
                                                  "button": "Open Users", "url": (config.PUBLIC_URL or "") + "/#/settings/users"},
                                       inbox=("trial.ended", "info", f"Trial ended: {u['email']}", "/#/settings/users"), mail=False)
            out["expired"] += 1
            continue
        days_left = (until - now).total_seconds() / 86400
        stamp = db.meta_get(f"trial_warned:{u['id']}")
        if days_left <= TRIAL_WARN_DAYS and stamp != exp:
            db.meta_set(f"trial_warned:{u['id']}", exp)
            summary = cockpit(area)["totals"] if area else {}
            msg = (f"{u['email']}'s Broadcaster trial ends {until.strftime('%Y-%m-%d')}: {summary.get('subscribers', 0)} subscriber(s), "
                   f"{summary.get('paused', 0)} paused, {summary.get('listings', 0)} listing(s). Extend or let it lapse under Settings → Users.")
            await alerts.notify_admins("notice", {"title": f"Trial ending: {u['email']}", "message": msg, "button": "Open Users", "url": (config.PUBLIC_URL or "") + "/#/settings/users"},
                                       inbox=("trial.ending", "warn", f"Trial ending: {u['email']}", "/#/settings/users"))
            out["warned"] += 1
    return out


# ------------------------------------------------------------------ weekly report
def _money(v: float) -> str:
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def weekly_report(area_id: int, user: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """The figures of the last seven days for one workspace, per role."""
    from . import readiness
    now = now or datetime.now(timezone.utc)
    zone = track_record._zone(area_id)
    frm = (now - timedelta(days=7)).isoformat()
    trades = [t for t in db.list_journal_trades(area_id, frm=frm)]
    st = track_record.summarize_trades(trades, zone, now=now, detail=False) if trades else None
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        try:
            day = datetime.fromisoformat(t["exit_ts"]).astimezone(zone).strftime("%a %d.%m.")
        except ValueError:
            day = "?"
        by_day[day] += float(t.get("net_pnl") or 0)
    best = max(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    worst = min(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    checks = readiness.workspace_checks(area_id, user)
    risks = [c for c in checks["checks"] if c["status"] != "ok"]
    lines = []
    lang = mailer.lang_for_area(area_id)
    de = lang == "de"
    if st:
        lines.append((f"Netto {_money(st['net_pnl'])} aus {st['trades']} Trades, Trefferquote {round(st['win_rate'] * 100)} %, Gebühren ${st['fees']:,.2f}." if de
                      else f"Net {_money(st['net_pnl'])} from {st['trades']} trades, hit rate {round(st['win_rate'] * 100)} %, fees ${st['fees']:,.2f}."))
        if best and worst and best[0] != worst[0]:
            lines.append((f"Beste Sitzung {best[0]} {_money(best[1])}, schlechteste {worst[0]} {_money(worst[1])}." if de
                          else f"Best session {best[0]} {_money(best[1])}, worst {worst[0]} {_money(worst[1])}."))
    else:
        lines.append("Keine abgeschlossenen Trades in den letzten sieben Tagen." if de else "No closed trades in the last seven days.")
    if risks:
        lines.append(("Offene Risiken: " if de else "Open risks: ") + "; ".join(f"{c['label']} — {c['detail']}" for c in risks[:5]))
    role = web.role_of(user)
    extra: dict[str, Any] = {}
    if role in ("broadcaster", "admin"):
        ck = cockpit(area_id)
        if ck["totals"]["listings"]:
            new = sum(g["new"] for g in ck["growth"][-1:])
            rev = ", ".join(f"{c / 100:,.2f} {cur.upper()}" for cur, c in ck["totals"]["revenue"].items()) or "0"
            lines.append((f"Abonnenten: {ck['totals']['subscribers']} ({ck['totals']['paused']} pausiert, {ck['totals']['unpaid']} unbezahlt), {new} neu diese Woche, Monatsumsatz {rev}." if de
                          else f"Subscribers: {ck['totals']['subscribers']} ({ck['totals']['paused']} paused, {ck['totals']['unpaid']} unpaid), {new} new this week, monthly revenue {rev}."))
            extra["cockpit"] = ck["totals"]
    if role == "admin":
        users = db.list_users()
        hs = db.history_stats(context.DEFAULT_AREA_ID, frm) if hasattr(db, "history_stats") else {}
        lw = readiness.latency_window()
        counts = db.outbox_counts()
        lines.append((f"Plattform: {len(users)} Nutzer, {sum(1 for u in users if web.role_of(u) == 'broadcaster')} Broadcaster, Latenz p95 {lw.get('p95_ms') or '—'} ms, "
                      f"Mail-Ausgang {counts['failed']} fehlgeschlagen." if de
                      else f"Platform: {len(users)} users, {sum(1 for u in users if web.role_of(u) == 'broadcaster')} broadcasters, latency p95 {lw.get('p95_ms') or '—'} ms, "
                           f"mail outbox {counts['failed']} failed."))
        extra["platform"] = {"users": len(users), "signals": (hs.get("totals") or {}) if isinstance(hs, dict) else {}}
    return {"area_id": area_id, "role": role, "lang": lang, "lines": lines, "stats": st, "risks": risks, **extra}


def _week_stamp(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


async def mail_weekly_reports(now: Optional[datetime] = None) -> int:
    """Monday from WEEKLY_HOUR local time: one report per user who wants it, once per ISO week."""
    now = now or datetime.now(timezone.utc)
    if not mailer.configured():
        return 0
    n = 0
    for u in db.list_users():
        area = db.user_primary_area(u["id"])
        if not area or not releases.wants(u["id"], "weekly"):
            continue
        zone = track_record._zone(area)
        local = now.astimezone(zone)
        if local.weekday() != 0 or local.hour < WEEKLY_HOUR:
            continue
        stamp = _week_stamp(local)
        key = f"weekly_sent:{u['id']}"
        if db.meta_get(key) == stamp:
            continue
        db.meta_set(key, stamp)
        try:
            rep = await asyncio.to_thread(weekly_report, area, u, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("weekly report for %s failed: %s", u["email"], exc)
            continue
        mailer.send_template(u["email"], "weekly", {"week": stamp, "lines": "\n".join(rep["lines"]), "url": (config.PUBLIC_URL or "") + "/#/journal",
                                                    "unsubscribe_url": releases.unsubscribe_url(u["id"], "weekly")}, lang=rep["lang"], area_id=area)
        n += 1
    return n


async def roles_report(now: Optional[datetime] = None) -> int:
    """alpha.99: on the 1st of the month, once: who holds which role, which
    Broadcasters sent no signal in 30 days, open requests, support views last month."""
    from . import alerts
    now = now or datetime.now(timezone.utc)
    if now.day != 1:
        return 0
    stamp = now.strftime("%Y-%m")
    if db.meta_get("roles_report") == stamp:
        return 0
    db.meta_set("roles_report", stamp)
    users = db.list_users()
    by_role = {r: sum(1 for u in users if web.role_of(u) == r) for r in ("admin", "broadcaster", "user")}
    since = track_record._iso_days_ago(30, now)
    idle = []
    for u in users:
        if web.role_of(u) != "broadcaster":
            continue
        area = db.user_primary_area(u["id"])
        n = sum(db.count_signal_outcomes(area, k, since) for k, _ in listing_keys(area) if not k.startswith("copy:")) if area else 0
        if n == 0:
            idle.append(u["email"])
    pending = [u["email"] for u in users if u.get("role_request")]
    trials = [f"{u['email']} until {u['role_expires_at'][:10]}" for u in users if u.get("role_expires_at")]
    month_ago = (now - timedelta(days=31)).isoformat()
    views = [a for a in db.list_audit(500) if a.get("action") in ("support_view", "support_write") and str(a.get("ts") or a.get("created_at") or "") >= month_ago]
    lines = [f"Roles: {by_role['admin']} admin, {by_role['broadcaster']} broadcaster, {by_role['user']} user.",
             f"Broadcasters without a signal in 30 days: {', '.join(idle) or 'none'}.",
             f"Open Broadcaster requests: {', '.join(pending) or 'none'}.",
             f"Trials: {', '.join(trials) or 'none'}.",
             f"Support views last month: {len(views)}."]
    await alerts.notify_admins("notice", {"title": f"Roles report {stamp}", "message": "\n".join(lines), "button": "Open Users", "url": (config.PUBLIC_URL or "") + "/#/settings/users"},
                               inbox=("roles.report", "info", f"Roles report {stamp}", "/#/settings/users"))
    return len(users)


async def loop() -> None:
    while True:
        try:
            await mail_weekly_reports()
            await trial_check()
            await roles_report()
        except Exception as exc:  # noqa: BLE001
            log.warning("broadcaster loop tick failed: %s", exc)
        await asyncio.sleep(LOOP_TICK_S)

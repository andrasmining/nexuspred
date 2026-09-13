"""Outbound notifications for connection and trade events.

Three channels, each independently toggled in Settings:

* **Discord** — a POST to a webhook URL, optionally prefixed with ``@everyone``.
* **Email** — sent via SMTP (e.g. Gmail with an App Password), connection
  events only (trade executions are Discord-only, per the trigger design).
* **Push** — Web Push to every device that installed the dashboard (desktop
  browsers and the iPhone home-screen app); gets every trigger, trades included.

Each of the three triggers (connection lost, connection restored, trade
executed) has its own on/off switch. A failure sending a notification is
logged and swallowed — a broken webhook URL or bad SMTP login must never
break a health check or a trade.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import smtplib
import ssl
import time
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config, context, db, http, push, security, state

# ------------------------------------------------------------- alpha.97: severities, quiet hours, digest, inbox
SEVERITIES = ("info", "warn", "critical")
DIGEST_KINDS = frozenset({"trade.executed", "position.opened", "position.added", "position.closed"})
DIGEST_TICK_S = 15.0
_gate: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar("alert_gate", default=None)
_inbox_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="inbox")   # one writer: rows keep their order
_digest: dict[int, list[tuple[float, str, str]]] = {}          # area → [(ts, title, message)]


def _rank(sev: str) -> int:
    return SEVERITIES.index(sev) if sev in SEVERITIES else 0


def quiet_now(s: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Inside the workspace's quiet hours (``alert_quiet_from``–``alert_quiet_to``,
    local to ``journal_timezone``)? A window may wrap midnight."""
    a, b = str(s.get("alert_quiet_from") or ""), str(s.get("alert_quiet_to") or "")
    if not a or not b or a == b:
        return False
    try:
        tz = ZoneInfo(str(s.get("journal_timezone") or "UTC"))
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    now = now.astimezone(tz) if now else datetime.now(tz)
    cur = now.strftime("%H:%M")
    return (a <= cur < b) if a < b else (cur >= a or cur < b)


def _allowed(channel: str, s: dict[str, Any]) -> bool:
    """Per channel: the alert's severity meets the channel's threshold and the
    quiet hours do not apply (critical always passes). No gate = always."""
    g = _gate.get()
    if g is None:
        return True
    sev = g["severity"]
    if sev == "critical":
        return True
    if _rank(sev) < _rank(str(s.get(f"alert_min_severity_{channel}") or "info")):
        return False
    return not quiet_now(s)


def _inbox(kind: str, severity: str, title: str, message: str, url: str) -> None:
    """The notification inbox row (off the loop) and the live badge."""
    area = context.get_area()
    text = push.strip_markdown(message)
    try:
        asyncio.get_running_loop().run_in_executor(_inbox_pool, db.add_notification, area, kind, severity, push.strip_markdown(title), text, url)
    except RuntimeError:
        db.add_notification(area, kind, severity, push.strip_markdown(title), text, url)
    state.publish("notification", {"kind": kind, "severity": severity, "title": push.strip_markdown(title), "url": url})


async def _send_telegram(title: str, message: str, *, settings: dict[str, Any] | None = None) -> None:
    """alpha.99: the workspace's linked Telegram chat (own switch and threshold)."""
    from . import telegram
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_telegram_enabled") or not telegram.configured() or not _allowed("telegram", s):
        return
    area = context.get_area()
    if not telegram.chat_for(area):
        return
    try:
        await telegram.send_area(area, f"*{push.strip_markdown(title)}*\n{push.strip_markdown(message)}")
        _record("telegram", True, title)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"Telegram alert failed: {exc}")
        _record("telegram", False, title, str(exc))


def _fire(coro: Any) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


def _begin(kind: str, severity: str, title: str, message: str, url: str = "/#/", *, settings: dict[str, Any] | None = None) -> bool:
    """Every alert starts here: inbox row, then the channel gate for the sends
    that follow. Returns False when the alert was folded into the trade digest
    (nothing more to send now)."""
    s = settings if settings is not None else config.load_settings()
    if severity == "critical" and s.get("alert_escalation") and kind not in ("digest", "announcement", "test"):
        from . import escalation
        try:
            esc = escalation.open_escalation(context.get_area(), kind, push.strip_markdown(title), push.strip_markdown(message), url)
            message = f"{message}\nAcknowledge: {escalation.ack_url(esc['id'])}"
        except Exception as exc:  # noqa: BLE001
            state.log_event("warn", f"escalation not opened: {exc}")
    _inbox(kind, severity, title, message, url)
    if s.get("alert_telegram_enabled") and kind not in DIGEST_KINDS or (s.get("alert_telegram_enabled") and not s.get("alert_digest_trades")):
        ctx = contextvars.copy_context()
        ctx.run(_gate.set, {"severity": severity, "kind": kind})
        _fire(_telegram_in(ctx, title, message, s))
    if kind in DIGEST_KINDS and severity == "info" and s.get("alert_digest_trades"):
        buf = _digest.setdefault(context.get_area(), [])
        buf.append((time.time(), push.strip_markdown(title), message))
        if len(buf) > 200:
            del buf[:-200]
        return False
    _gate.set({"severity": severity, "kind": kind})
    return True


async def _telegram_in(ctx: contextvars.Context, title: str, message: str, s: dict[str, Any]) -> None:
    await asyncio.create_task(_send_telegram(title, message, settings=s), context=ctx)


async def token_expiring(account: str, minutes: int, error: str = "") -> None:
    """alpha.99: a broker token that will lapse soon while its refresh keeps failing —
    before the connection is lost, not after."""
    s = config.load_settings()
    tr = _tr(s)
    detail = f" — {error}" if error else ""
    message = tr("⏳ **Token expiring** — login `{account}` expires in {minutes} min and the refresh failed{detail}. Sign in again under Settings → Broker Accounts.",
                 account=account, minutes=minutes, detail=detail)
    _begin("token.expiring", "warn", tr("Token expiring: {account}", account=account), message, "/#/settings/accounts", settings=s)
    await asyncio.gather(_send_discord(message), _send_email(tr("Fluxbridge: token expiring ({account})", account=account), message),
                         _send_push(tr("Token expiring: {account}", account=account), message, url="/#/settings/accounts"))


async def flush_digest(area_id: int, *, force: bool = False) -> bool:
    """Send one combined message for the buffered trade alerts of a workspace."""
    buf = _digest.get(area_id) or []
    if not buf:
        return False
    with context.use_area(area_id):
        s = config.load_settings()
        minutes = max(1, int(s.get("alert_digest_minutes") or 15))
        if not force and time.time() - buf[0][0] < minutes * 60:
            return False
        _digest[area_id] = []
        tr = _tr(s)
        lines = "\n".join(f"• {m}" for _, _, m in buf[-30:])
        head = tr("📬 **{n} trade updates** (last {minutes} min)", n=len(buf), minutes=minutes)
        _gate.set({"severity": "info", "kind": "digest"})
        await asyncio.gather(_send_discord(f"{head}\n{lines}"),
                             _send_push(tr("{n} trade updates", n=len(buf)), "\n".join(t for _, t, _ in buf[-5:]), url="/#/orders"))
        _gate.set(None)
    return True


async def digest_loop() -> None:
    while True:
        await asyncio.sleep(DIGEST_TICK_S)
        for aid in list(_digest.keys()):
            try:
                await flush_digest(aid)
            except Exception as exc:  # noqa: BLE001
                state.log_event("warn", f"alert digest failed: {exc}")


def reset_digest() -> None:
    _digest.clear()


def drain_inbox(timeout: float = 5.0) -> None:
    """Wait for queued inbox writes (tests switch the database between runs)."""
    try:
        _inbox_pool.submit(lambda: None).result(timeout)
    except Exception:  # noqa: BLE001
        pass


def account_alerts_on(spec: str, settings: dict[str, Any] | None = None) -> bool:
    """Whether account-level alerts are wanted for this trade account.
    ``alert_accounts`` lists the wanted specs; an empty list means all."""
    s = settings if settings is not None else config.load_settings()
    wanted = s.get("alert_accounts") or []
    return not wanted or str(spec) in set(wanted)


def alert_accounts(accounts: list[str], settings: dict[str, Any] | None = None) -> list[str]:
    s = settings if settings is not None else config.load_settings()
    return [a for a in accounts if account_alerts_on(a, s)]


def _record(channel: str, ok: bool, title: str, error: str = "") -> None:
    """alpha.95: every delivery attempt in the workspace's delivery log (off the
    event-loop thread; a failure to log is itself swallowed)."""
    area = context.get_area()
    try:
        asyncio.get_running_loop().run_in_executor(None, db.record_delivery, area, channel, ok, push.strip_markdown(title)[:120], error)
    except RuntimeError:
        db.record_delivery(area, channel, ok, title[:120], error)


async def _send_push(title: str, message: str, *, url: str = "/", settings: dict[str, Any] | None = None) -> None:
    """Web Push to the area's installed apps (see :mod:`app.push`)."""
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_push_enabled", True) or not push.available() or not _allowed("push", s):
        return
    try:
        r = await push.send_current_area(title, push.strip_markdown(message), url=url)
    except Exception as exc:  # noqa: BLE001 - never let a notification failure escalate
        state.log_event("warn", f"Push alert failed: {exc}")
        _record("push", False, title, str(exc))
        return
    if isinstance(r, dict) and r.get("sent", 0) == 0 and r.get("failed", 0) > 0:
        _record("push", False, title, f"{r.get('failed')} device(s) rejected the push")
    elif not isinstance(r, dict) or r.get("sent", 0) > 0 or r.get("total", 1) == 0:
        _record("push", True, title)


async def _send_discord(message: str, *, settings: dict[str, Any] | None = None) -> None:
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_discord_enabled") or not s.get("alert_discord_webhook_url") or not _allowed("discord", s):
        return
    everyone = bool(s.get("alert_discord_mention_everyone"))
    content = (f"@everyone {message}" if everyone else message)[:2000]          # Discord refuses longer bodies
    url = str(s["alert_discord_webhook_url"])
    problem = await asyncio.to_thread(security.check_outbound_url, url)     # what it resolves to *now*, not at save time
    if problem:
        state.log_event("warn", f"Discord alert not sent — target rejected: {problem}")
        _record("discord", False, message, f"target rejected: {problem}")
        return
    try:
        resp = await http.client("outbound").post(
            url,
            # only the configured @everyone may ping: a webhook name or an error text carrying @here does not
            json={"content": content, "allowed_mentions": {"parse": ["everyone"] if everyone else []}}, timeout=10.0)
        if resp.status_code >= 400:
            state.log_event("warn", f"Discord alert failed: {resp.status_code} {resp.text}")
            _record("discord", False, message, f"{resp.status_code} {resp.text[:200]}")
        else:
            _record("discord", True, message)
    except Exception as exc:  # noqa: BLE001 - never let a notification failure escalate
        state.log_event("warn", f"Discord alert failed: {exc}")
        _record("discord", False, message, str(exc))


def _check_smtp_target(host: str, port: int) -> None:
    """The SMTP host must still resolve to a public address when the credentials
    go out (the same rule the settings form applied when it was saved)."""
    h = str(host).strip()
    if ":" in h and not h.startswith("["):
        h = f"[{h}]"
    problem = security.check_outbound_url(f"https://{h}:{int(port)}/")
    if problem:
        raise ValueError(f"SMTP target rejected: {problem}")


def _send_email_sync(subject: str, body: str) -> None:
    s = config.load_settings()
    to_addr = s.get("alert_email_to")
    username = s.get("alert_smtp_username")
    password = s.get("alert_smtp_password")
    if not s.get("alert_email_enabled") or not to_addr or not username or not password:
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr
    host = s.get("alert_smtp_host") or "smtp.gmail.com"
    port = int(s.get("alert_smtp_port") or 587)
    _check_smtp_target(host, port)
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls(context=ssl.create_default_context())  # verified TLS: credentials never go to an impostor
        server.login(username, password)
        server.send_message(msg)


async def _send_email(subject: str, body: str) -> None:
    s = config.load_settings()
    if not s.get("alert_email_enabled") or not s.get("alert_email_to") or not s.get("alert_smtp_username") or not s.get("alert_smtp_password"):
        return                                      # channel off or incomplete: nothing attempted, nothing logged
    if not _allowed("email", s):
        return
    try:
        await asyncio.to_thread(_send_email_sync, subject, body)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"Email alert failed: {exc}")
        _record("email", False, subject, str(exc))
        return
    _record("email", True, subject)


def smtp_configured() -> bool:
    """True when SMTP is set up well enough to send a message to any recipient."""
    s = config.load_settings()
    return bool(s.get("alert_smtp_username") and s.get("alert_smtp_password"))


def _send_to_sync(to_addr: str, subject: str, body: str) -> None:
    s = config.load_settings()
    username = s.get("alert_smtp_username")
    password = s.get("alert_smtp_password")
    if not to_addr or not username or not password:
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr
    host = s.get("alert_smtp_host") or "smtp.gmail.com"
    port = int(s.get("alert_smtp_port") or 587)
    _check_smtp_target(host, port)
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls(context=ssl.create_default_context())  # verified TLS: credentials never go to an impostor
        server.login(username, password)
        server.send_message(msg)


async def send_email_to(to_addr: str, subject: str, body: str) -> bool:
    """Send a one-off email to an arbitrary recipient via the configured SMTP.

    Returns True if a send was attempted (SMTP configured + recipient present).
    Used for invite / password-reset delivery, independent of the alert toggles.
    """
    if not smtp_configured() or not to_addr:
        return False
    try:
        await asyncio.to_thread(_send_to_sync, to_addr, subject, body)
        return True
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"Email send failed ({to_addr}): {exc}")
        return False


BROKER_LABELS = {"tradovate": "Tradovate", "rithmic": "Rithmic", "projectx": "ProjectX"}


def _tr(settings: dict[str, Any]):
    """The workspace's alert translator (see app.i18n.alert_translator)."""
    from . import i18n
    return i18n.alert_translator(settings)


async def connection_lost(account: str, environment: str, error: str, broker: str = "tradovate") -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_lost", True):
        return
    tr = _tr(s)
    detail = f" — {error}" if error else ""
    message = tr("🔴 **Connection lost** — login `{account}` ({environment}, {broker}){detail}", account=account, environment=environment, broker=BROKER_LABELS.get(broker, broker), detail=detail)
    _begin("connection.lost", "warn", tr("Connection lost: {account}", account=account), message, "/#/settings/accounts", settings=s)
    message = tr("🔴 **Connection lost** — login `{account}` ({environment}, {broker}){detail}", account=account, environment=environment, broker=BROKER_LABELS.get(broker, broker), detail=detail)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: connection lost ({account})", account=account), message),
                         _send_push(tr("Connection lost: {account}", account=account), message, url="/#/accounts"))


async def connection_restored(account: str, environment: str, broker: str = "tradovate") -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_restored", True):
        return
    tr = _tr(s)
    message = tr("🟢 **Connection restored** — login `{account}` ({environment}, {broker})", account=account, environment=environment, broker=BROKER_LABELS.get(broker, broker))
    _begin("connection.restored", "info", tr("Connection restored: {account}", account=account), message, "/#/settings/accounts", settings=s)
    message = tr("🟢 **Connection restored** — login `{account}` ({environment}, {broker})", account=account, environment=environment, broker=BROKER_LABELS.get(broker, broker))
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: connection restored ({account})", account=account), message),
                         _send_push(tr("Connection restored: {account}", account=account), message, url="/#/accounts"))


async def trade_executed(
    webhook_name: str, action: str, contract: str, accounts: list[str], *,
    settings: dict[str, Any] | None = None,
) -> None:
    """``settings`` is the executing signal's settings snapshot (saves three
    settings reads per fill; the alert preferences rarely change mid-signal)."""
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_on_trade_executed", True):
        return
    accounts = alert_accounts(accounts, s)
    if not accounts:
        return  # none of the traded accounts is on the alert list
    tr = _tr(s)
    accts = ", ".join(accounts)
    message = tr("⚡ **Trade executed** — strategy `{webhook}`: {action} {contract} on {accounts}", webhook=webhook_name, action=action.upper(), contract=contract, accounts=accts)
    if not _begin("trade.executed", "info", tr("Trade executed: {action} {contract}", action=action.upper(), contract=contract), message, "/#/orders", settings=s):
        return
    message = tr("⚡ **Trade executed** — strategy `{webhook}`: {action} {contract} on {accounts}", webhook=webhook_name, action=action.upper(), contract=contract, accounts=accts)
    await asyncio.gather(_send_discord(message, settings=s),
                         _send_push(tr("Trade executed: {action} {contract}", action=action.upper(), contract=contract), message, url="/#/orders", settings=s))


def _money(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if x > 0 else ("−" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


def _price(v: Any) -> str:
    return f"{float(v):.4f}".rstrip("0").rstrip(".")


async def trade_opened(account: str, symbol: str, direction: str, qty: float, price: Any = None) -> None:
    """A position appeared on the broker side (bridge signal, manual or otherwise)."""
    s = config.load_settings()
    if not s.get("alert_on_trade_opened", True) or not account_alerts_on(account, s):
        return
    tr = _tr(s)
    q = f"{qty:g}"
    at = f" @ {_price(price)}" if isinstance(price, (int, float)) and price else ""
    message = tr("🟢 **Opened** {direction} {qty} × {symbol}{at} · `{account}`", direction=direction, qty=q, symbol=symbol, at=at, account=account)
    if not _begin("position.opened", "info", tr("Opened {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account), message, "/#/", settings=s):
        return
    message = tr("🟢 **Opened** {direction} {qty} × {symbol}{at} · `{account}`", direction=direction, qty=q, symbol=symbol, at=at, account=account)
    await asyncio.gather(_send_discord(message),
                         _send_push(tr("Opened {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account), tr("{qty} contracts{at}" if qty != 1 else "{qty} contract{at}", qty=q, at=at), url="/#/"))


async def position_added(account: str, symbol: str, direction: str, added: float, total: float) -> None:
    s = config.load_settings()
    if not s.get("alert_on_trade_opened", True) or not account_alerts_on(account, s):
        return
    tr = _tr(s)
    message = tr("➕ **Added** {added} × {symbol} → {direction} {total} · `{account}`", added=f"{added:g}", symbol=symbol, direction=direction, total=f"{total:g}", account=account)
    if not _begin("position.added", "info", tr("Added {added} {symbol} · {account}", added=f"{added:g}", symbol=symbol, account=account), message, "/#/", settings=s):
        return
    message = tr("➕ **Added** {added} × {symbol} → {direction} {total} · `{account}`", added=f"{added:g}", symbol=symbol, direction=direction, total=f"{total:g}", account=account)
    await asyncio.gather(_send_discord(message),
                         _send_push(tr("Added {added} {symbol} · {account}", added=f"{added:g}", symbol=symbol, account=account), tr("Now {direction} {total}", direction=direction, total=f"{total:g}"), url="/#/"))


async def trade_closed(account: str, symbol: str, direction: str, qty: float, pnl: Any, duration: str = "",
                       *, remaining: float = 0) -> None:
    """A position (or part of it) was closed; ``pnl`` is the account's realised
    change between two polls, i.e. the broker's own figure for the close."""
    s = config.load_settings()
    if not s.get("alert_on_trade_closed", True) or not account_alerts_on(account, s):
        return
    tr = _tr(s)
    pnl_txt = _money(pnl) if pnl is not None else tr("P&L n/a")
    tail = f" ({duration})" if duration else ""
    if remaining:
        icon = "🟡"
        head = tr("**Reduced** {direction} {symbol} by {qty} → {remaining} left", direction=direction, symbol=symbol, qty=f"{qty:g}", remaining=f"{remaining:g}")
        title = tr("Reduced {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account)
    else:
        icon = "✅" if (isinstance(pnl, (int, float)) and pnl >= 0) else ("❌" if isinstance(pnl, (int, float)) else "⚪")
        head = tr("**Closed** {direction} {qty} × {symbol}", direction=direction, qty=f"{qty:g}", symbol=symbol)
        title = tr("Closed {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account)
    message = f"{icon} {head} · `{account}` · **{pnl_txt}**{tail}"
    if not _begin("position.closed", "info", title, message, "/#/journal", settings=s):
        return
    message = f"{icon} {head} · `{account}` · **{pnl_txt}**{tail}"
    await asyncio.gather(_send_discord(message),
                         _send_push(title, tr("{pnl}{tail} · {qty} contracts" if qty != 1 else "{pnl}{tail} · {qty} contract", pnl=pnl_txt, tail=tail, qty=f"{qty:g}"), url="/#/journal"))


async def agent_lost(name: str, last_ip: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_agent_lost", True):
        return
    tr = _tr(s)
    where = tr(" (last seen from {ip})", ip=last_ip) if last_ip else ""
    message = tr("🔴 **Execution agent offline** — `{name}` stopped polling{where}. Logins assigned to it cannot trade until it is back.", name=name, where=where)
    _begin("agent.lost", "warn", tr("Agent offline: {name}", name=name), message, "/#/settings/agents", settings=s)
    message = tr("🔴 **Execution agent offline** — `{name}` stopped polling{where}. Logins assigned to it cannot trade until it is back.", name=name, where=where)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: execution agent offline ({name})", name=name), message),
                         _send_push(tr("Agent offline: {name}", name=name), message, url="/#/settings/agents"))


async def agent_restored(name: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_agent_restored", True):
        return
    tr = _tr(s)
    message = tr("🟢 **Execution agent online** — `{name}` is polling again", name=name)
    _begin("agent.restored", "info", tr("Agent online: {name}", name=name), message, "/#/settings/agents", settings=s)
    message = tr("🟢 **Execution agent online** — `{name}` is polling again", name=name)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: execution agent online ({name})", name=name), message),
                         _send_push(tr("Agent online: {name}", name=name), message, url="/#/settings/agents"))


async def risk_triggered(spec: str, kind: str, reason: str, pnl: float, errors: list[str]) -> None:
    """Risk guard: an account hit its daily loss / profit limit or flatten time."""
    s = config.load_settings()
    if not s.get("alert_on_risk", True):
        return
    tr = _tr(s)
    icon = {"loss": "🛑", "profit": "🎯", "time": "⏰"}.get(kind, "🔒")
    message = tr("{icon} **Risk guard** — `{spec}` flattened and locked for today: {reason}.{errors}", icon=icon, spec=spec, reason=reason,
                 errors=tr(" Errors: {errors}", errors="; ".join(errors)) if errors else "")
    _begin("risk.triggered", "critical", tr("Risk guard: {spec}", spec=spec), message, "/#/settings/accounts", settings=s)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: risk guard {spec} ({kind})", spec=spec, kind=kind), message),
                         _send_push(tr("Risk guard: {spec}", spec=spec), message, url="/#/settings/accounts"))


async def execution_problem(title: str, message: str) -> None:
    """Something the operator must look at now: a position without its stop, an
    order whose outcome is unknown, working orders left behind by a close.
    Always sent (no switch), to every channel."""
    tr = _tr(config.load_settings())
    body = tr("🚨 **{title}** — {message}", title=title, message=message)
    _begin("execution.problem", "critical", title, body, "/#/")
    await asyncio.gather(_send_discord(body), _send_email(f"Fluxbridge: {title}", body),
                         _send_push(title, message, url="/#/"))
    body = tr("🚨 **{title}** — {message}", title=title, message=message)
    await asyncio.gather(_send_discord(body), _send_email(f"Fluxbridge: {title}", body),
                         _send_push(title, message, url="/#/"))


async def automation(name: str, message: str) -> None:
    """A rule under Settings → Automations fired: every channel, no switch
    (the rule is the switch)."""
    tr = _tr(config.load_settings())
    body = tr("⚙️ **Automation {name}** — {message}", name=name, message=message)
    _begin("automation.fired", "warn", tr("Automation: {name}", name=name), body, "/#/settings/automations")
    body = tr("⚙️ **Automation {name}** — {message}", name=name, message=message)
    await asyncio.gather(_send_discord(body), _send_email(f"Fluxbridge: {name}", body),
                         _send_push(tr("Automation: {name}", name=name), message, url="/#/settings/automations"))


async def subscription_paused(title: str, reason: str) -> None:
    """A marketplace subscription switched itself off (subscriber control)."""
    tr = _tr(config.load_settings())
    message = tr("Subscription '{title}' switched off: {reason}", title=title, reason=reason)
    body = tr("⏸️ **Subscription paused** — {message}", message=message)
    _begin("subscription.paused", "warn", tr("Subscription paused"), body, "/#/subscriptions")
    body = tr("⏸️ **Subscription paused** — {message}", message=message)
    await asyncio.gather(_send_discord(body), _send_email(f"Fluxbridge: {title}", body),
                         _send_push(tr("Subscription paused"), message, url="/#/subscriptions"))


async def news_lock(title: str, currency: str, until: str, *, flatten: bool = False) -> None:
    """A news-lock window opened: no new entries until ``until`` (and, with
    ``flatten``, open positions are being closed)."""
    tr = _tr(config.load_settings())
    what = f"{title}{' (' + currency + ')' if currency else ''}"
    message = tr("{what}: no new entries until {until}", what=what, until=until) + (tr(" — open positions are being flattened") if flatten else "")
    body = tr("📰 **News lock** — {message}", message=message)
    _begin("news.lock", "critical" if flatten else "warn", tr("News lock"), body, "/#/settings/news")
    await asyncio.gather(_send_discord(body), _send_push(tr("News lock"), message, url="/#/settings/news"))


async def copy_alert(title: str, message: str, *, email: bool = False) -> None:
    """Copy trading: a follower order was rejected or a group paused itself."""
    s = config.load_settings()
    if not s.get("alert_on_copy", True):
        return
    body = _tr(s)("📋 **Copy trading** — {message}", message=message)
    _begin("copy.alert", "critical" if email else "warn", title, body, "/#/copy", settings=s)
    body = _tr(s)("📋 **Copy trading** — {message}", message=message)
    sends = [_send_discord(body), _send_push(title, message, url="/#/copy")]
    if email:
        sends.append(_send_email(f"Fluxbridge: {title}", body))
    await asyncio.gather(*sends)


async def daily_summary(pnl: dict[str, Any], closes: list[dict[str, Any]], day: str) -> None:
    """End-of-day recap: realised P&L per account plus the day's closed trades."""
    s = config.load_settings()
    if not s.get("alert_daily_summary", True):
        return
    accounts = [a for a in (pnl.get("accounts") or []) if account_alerts_on(a.get("spec") or a.get("account_id"), s)]
    closes = [c for c in closes if account_alerts_on(c.get("account", ""), s)]
    tr = _tr(s)
    per = ", ".join(f"{a.get('spec') or a.get('account_id')} {_money(a.get('realized'))}" for a in accounts) or tr("no accounts polled")
    wins = sum(1 for c in closes if isinstance(c.get("pnl"), (int, float)) and c["pnl"] > 0)
    losses = sum(1 for c in closes if isinstance(c.get("pnl"), (int, float)) and c["pnl"] < 0)
    trades = tr("{n} trades closed" if len(closes) != 1 else "{n} trade closed", n=len(closes)) + (tr(" ({wins} win, {losses} loss)", wins=wins, losses=losses) if closes else "")
    total = _money(sum(float(a.get("realized") or 0) for a in accounts))
    open_pnl = _money(sum(float(a.get("open") or 0) for a in accounts))
    message = tr("📊 **Daily summary {day}** — realised **{total}** ({per}) · {trades} · open {open}", day=day, total=total, per=per, trades=trades, open=open_pnl)
    _begin("daily.summary", "info", tr("Daily P&L {total}", total=total), message, "/#/journal", settings=s)
    message = tr("📊 **Daily summary {day}** — realised **{total}** ({per}) · {trades} · open {open}", day=day, total=total, per=per, trades=trades, open=open_pnl)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: daily summary {day} ({total})", day=day, total=total), message),
                         _send_push(tr("Daily P&L {total}", total=total), f"{trades} · {per}", url="/#/journal"))


async def discord_listener_lost(error: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_discord_lost", True):
        return
    tr = _tr(s)
    detail = f" — {error}" if error else ""
    message = tr("🔴 **Discord listener offline** — the signal listener lost its Gateway connection{detail}", detail=detail)
    _begin("discord.lost", "warn", tr("Discord listener offline"), message, "/#/discord", settings=s)
    message = tr("🔴 **Discord listener offline** — the signal listener lost its Gateway connection{detail}", detail=detail)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: Discord listener offline"), message),
                         _send_push(tr("Discord listener offline"), message, url="/#/discord"))


async def discord_listener_restored(user: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_discord_restored", True):
        return
    tr = _tr(s)
    who = tr(" (as `{user}`)", user=user) if user else ""
    message = tr("🟢 **Discord listener online** — the signal listener reconnected to the Gateway{who}", who=who)
    _begin("discord.restored", "info", tr("Discord listener online"), message, "/#/discord", settings=s)
    message = tr("🟢 **Discord listener online** — the signal listener reconnected to the Gateway{who}", who=who)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: Discord listener online"), message),
                         _send_push(tr("Discord listener online"), message, url="/#/discord"))


async def webhook_failed(webhook_name: str, reason: str, *, settings: dict[str, Any] | None = None) -> None:
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_on_webhook_failed", True):
        return
    tr = _tr(s)
    message = tr("⚠️ **Signal not executed** — webhook `{webhook}` received a signal but execution failed: {reason}", webhook=webhook_name, reason=reason)
    _begin("signal.failed", "warn", tr("Signal not executed: {webhook}", webhook=webhook_name), message, "/#/logs", settings=s)
    message = tr("⚠️ **Signal not executed** — webhook `{webhook}` received a signal but execution failed: {reason}", webhook=webhook_name, reason=reason)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: signal not executed ({webhook})", webhook=webhook_name), message),
                         _send_push(tr("Signal not executed: {webhook}", webhook=webhook_name), message, url="/#/events"))


async def contract_rollover(message: str) -> None:
    """A mapped contract is near / past its roll date (Discord + email)."""
    s = config.load_settings()
    if not s.get("alert_on_rollover", True):
        return
    tr = _tr(s)
    _begin("rollover.due", "warn", tr("Contract rollover due"), message, "/#/settings/symbols", settings=s)
    tr = _tr(s)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: contract rollover due"), message),
                         _send_push(tr("Contract rollover due"), message, url="/#/settings/symbols"))


async def publisher_event(publisher_area_id: int, kind: str, title: str, message: str, *, severity: str = "info", url: str = "/#/webhooks") -> None:
    """alpha.97: something happened to a Broadcaster's listing — a subscriber
    joined or left, a payment failed, a subscriber was paused after errors.
    Delivered in the *publisher's* workspace, on its channels."""
    with context.use_area(publisher_area_id):
        s = config.load_settings()
        if not s.get("alert_on_subscribers", True):
            _inbox(kind, severity, title, message, url)
            return
        body = _tr(s)("📣 **Marketplace** — {message}", message=message)
        _begin(kind, severity, title, body, url, settings=s)
        await asyncio.gather(_send_discord(body), _send_push(title, message, url=url),
                             *([_send_email(f"Fluxbridge: {title}", body)] if severity != "info" else []))
        _gate.set(None)


async def subscriber_announcement(area_id: int, publisher: str, title: str, body: str, *, url: str = "/#/subscriptions") -> None:
    """alpha.98: a Broadcaster's message to a subscriber — inbox + push (+ Discord), never a trade alert."""
    with context.use_area(area_id):
        s = config.load_settings()
        message = _tr(s)("📣 **{publisher}** — {title}: {body}", publisher=publisher, title=title, body=body[:600])
        _begin("announcement", "info", f"{publisher}: {title}", message, url, settings=s)
        await asyncio.gather(_send_discord(message, settings=s), _send_push(f"{publisher}: {title}", body[:180], url=url, settings=s))
        _gate.set(None)


async def security_event(area_id: int, kind: str, title: str, message: str, *, url: str = "/#/settings/security") -> None:
    """alpha.97: a sign-in from a new address, a two-factor reset — the user's
    inbox and push, whatever the alert switches say."""
    with context.use_area(area_id):
        _begin(kind, "warn", title, message, url)
        await _send_push(title, message, url=url)
        _gate.set(None)


async def test_alert() -> dict[str, Any]:
    """Send a test notification on every enabled channel; report what was tried."""
    s = config.load_settings()
    message = _tr(s)("🔔 **Test alert** — Fluxbridge notifications are configured correctly.")
    _inbox("test", "info", _tr(s)("Test alert"), message, "/#/settings/alerts")
    channels = {"discord": bool(s.get("alert_discord_enabled") and s.get("alert_discord_webhook_url")),
                "email": bool(s.get("alert_email_enabled") and s.get("alert_email_to")
                              and s.get("alert_smtp_username") and s.get("alert_smtp_password")),
                "push": bool(s.get("alert_push_enabled", True) and push.available()
                             and db.list_push_subscriptions(context.get_area()))}
    sends = []
    if channels["discord"]:
        sends.append(_send_discord(message))
    if channels["email"]:
        sends.append(_send_email("Fluxbridge: test alert", message))
    if channels["push"]:
        sends.append(_send_push("Test alert", message, url="/#/settings/alerts"))
    if sends:
        await asyncio.gather(*sends)
    return channels


# ------------------------------------------------------------- event bus
# The producers announce, the alert functions above listen. Handlers look the
# alert function up at call time, so a test that patches ``alerts.trade_opened``
# still sees the call.
def _register() -> None:
    from . import events as ev
    ev.subscribe("connection.lost", lambda e: connection_lost(e["account"], e["environment"], e.get("error", ""), broker=e.get("broker", "tradovate")))
    ev.subscribe("connection.restored", lambda e: connection_restored(e["account"], e["environment"], broker=e.get("broker", "tradovate")))
    ev.subscribe("trade.executed", lambda e: trade_executed(e["webhook"], e["action"], e["contract"], e["accounts"], **({"settings": e["settings"]} if e.get("settings") is not None else {})))
    ev.subscribe("position.opened", lambda e: trade_opened(e["account"], e["symbol"], e["direction"], e["qty"], e.get("price")))
    ev.subscribe("position.added", lambda e: position_added(e["account"], e["symbol"], e["direction"], e["added"], e["total"]))
    ev.subscribe("position.closed", lambda e: trade_closed(e["account"], e["symbol"], e["direction"], e["qty"], e.get("pnl"), e.get("duration", ""), **({"remaining": e["remaining"]} if e.get("remaining") else {})))
    ev.subscribe("agent.lost", lambda e: agent_lost(e["name"], e.get("last_ip", "")))
    ev.subscribe("agent.restored", lambda e: agent_restored(e["name"]))
    ev.subscribe("risk.triggered", lambda e: risk_triggered(e["spec"], e["kind"], e["reason"], e["pnl"], e.get("errors") or []))
    ev.subscribe("execution.problem", lambda e: execution_problem(e["title"], e["message"]))
    ev.subscribe("news.lock", lambda e: news_lock(e["title"], e["currency"], e["until"], flatten=bool(e.get("flatten"))) if e.get("alert", True) else None)
    ev.subscribe("copy.alert", lambda e: copy_alert(e["title"], e["message"], email=bool(e.get("email"))))
    ev.subscribe("daily.summary", lambda e: daily_summary(e["pnl"], e["closes"], e["day"]))
    ev.subscribe("discord.lost", lambda e: discord_listener_lost(e.get("error", "")))
    ev.subscribe("discord.restored", lambda e: discord_listener_restored(e.get("user", "")))
    ev.subscribe("signal.failed", lambda e: webhook_failed(e["webhook"], e["reason"], **({"settings": e["settings"]} if e.get("settings") is not None else {})))
    ev.subscribe("rollover.due", lambda e: contract_rollover(e["message"]))
    ev.subscribe("subscription.paused", lambda e: subscription_paused(e["title"], e["reason"]))
    ev.subscribe("token.expiring", lambda e: token_expiring(e["account"], int(e.get("minutes") or 0), e.get("error", "")))


_register()


async def notify_admins(kind: str, ctx: dict[str, Any], *, attachment: str | None = None,
                        inbox: tuple[str, str, str, str] | None = None, mail: bool = True) -> int:
    """Queue a template mail (see app.mailer.TEMPLATES) to every admin, each in
    their own language, through the platform mailer, and (``inbox`` =
    (kind, severity, title, url)) a row in every admin's notification inbox.
    Returns how many mails were queued."""
    from . import mailer
    n = 0
    for u in db.list_users():
        if u.get("role") == "admin" or u.get("is_admin"):
            area = db.user_primary_area(u["id"])
            if inbox and area:
                ikind, sev, title, url = inbox
                with context.use_area(area):
                    _inbox(ikind, sev, title, str(ctx.get("message") or ctx.get("email") or ""), url)
            if not mail or not mailer.can_send(area):
                continue
            mailer.send_template(u["email"], kind, ctx, lang=mailer.lang_for_area(area), area_id=area, attachment=attachment)
            n += 1
    return n

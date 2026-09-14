"""Platform mailer (alpha.95): the bridge's own sender for transactional mail.

Until now every e-mail — invites, reset links, the Broadcaster request to the
admins — went through the SMTP settings of *whichever workspace was active*,
i.e. the user's own Gmail App Password. A user without one sent nothing, and
nothing said so. Now:

* **One platform sender**, configured by the admin under Settings → Platform
  or pinned by ``NEXUSPRED_MAIL_*`` environment variables: SMTP, Resend or
  Postmark. Workspace SMTP stays what it was — the user's channel for their
  own trade alerts — and is the fallback when the platform sender is off.
* **Templates** in the recipient's language (the workspace's UI language),
  HTML with a plain-text twin, one button per mail.
* **Outbox**: every mail is a row first (:mod:`app.db.mail`), delivered by
  :func:`outbox_loop` with backoff (1 min → 6 h, five attempts). The admin sees
  pending / sent / failed with the error, can retry, and an address that keeps
  bouncing is flagged so the dashboard can say so.

Nothing here runs on the order path.
"""
from __future__ import annotations

import asyncio
import base64
import html as _html
import json
import logging
import os
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Optional

from . import config, context, crypto, db, http, state

log = logging.getLogger("nexuspred.mailer")

META_KEY = "mailer"
PROVIDERS = ("off", "smtp", "resend", "postmark")
DEFAULTS: dict[str, Any] = {"provider": "off", "host": "", "port": 587, "username": "", "password": "", "api_key": "",
                            "from_addr": "", "from_name": "Fluxbridge", "reply_to": ""}
SECRET_KEYS = ("password", "api_key")
ENV_PREFIX = "NEXUSPRED_MAIL_"
ENV_KEYS = {"provider": "PROVIDER", "host": "HOST", "port": "PORT", "username": "USERNAME", "password": "PASSWORD",
            "api_key": "API_KEY", "from_addr": "FROM", "from_name": "FROM_NAME", "reply_to": "REPLY_TO"}
BACKOFF_S = (60, 300, 900, 3600, 21600)        # after the 1st … 5th failure; then the row is failed
MAX_ATTEMPTS = len(BACKOFF_S)
LOOP_TICK_S = 5.0
BLOCK_AFTER = 5                                 # consecutive failures to one address → flagged
RESEND_API = "https://api.resend.com/emails"
POSTMARK_API = "https://api.postmarkapp.com/email"

_cache: Optional[dict[str, Any]] = None
_wake: Optional[asyncio.Event] = None


# ------------------------------------------------------------------ config
def _env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, suffix in ENV_KEYS.items():
        v = os.environ.get(ENV_PREFIX + suffix)
        if v is not None and v.strip():
            out[k] = v.strip()
    if "port" in out:
        try:
            out["port"] = int(out["port"])
        except ValueError:
            out.pop("port")
    if "provider" in out and out["provider"] not in PROVIDERS:
        out.pop("provider")
    return out


def env_locked() -> list[str]:
    """Keys pinned by the environment (the admin page shows them read-only)."""
    return sorted(_env().keys())


def get_config() -> dict[str, Any]:
    global _cache
    if _cache is None:
        cfg = dict(DEFAULTS)
        raw = db.meta_get(META_KEY)
        if raw:
            try:
                stored = json.loads(raw)
                if isinstance(stored, dict):
                    cfg.update({k: stored[k] for k in DEFAULTS if k in stored})
            except ValueError:
                pass
        for k in SECRET_KEYS:
            v = cfg.get(k) or ""
            cfg[k] = str(crypto.decrypt(v) or "") if v else ""
        _cache = cfg
    return {**_cache, **_env()}


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge validated updates; secrets encrypted at rest. ValueError on bad input."""
    global _cache
    get_config()
    cfg = dict(_cache or DEFAULTS)
    if "provider" in updates:
        p = str(updates["provider"] or "off").lower().strip()
        if p not in PROVIDERS:
            raise ValueError("provider must be off, smtp, resend or postmark")
        cfg["provider"] = p
    for k in ("host", "username", "from_addr", "from_name", "reply_to"):
        if k in updates:
            v = str(updates[k] or "").strip()
            if len(v) > 200:
                raise ValueError(f"{k} is too long")
            cfg[k] = v
    if "port" in updates:
        try:
            port = int(float(updates["port"] or 587))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("port must be a number")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        cfg["port"] = port
    for k in SECRET_KEYS:
        if k in updates and updates[k] != "********":
            v = str(updates[k] or "").strip()
            if len(v) > 500:
                raise ValueError(f"{k} is too long")
            cfg[k] = v
    for k in ("from_addr", "reply_to"):
        if cfg[k] and ("@" not in cfg[k] or any(ch.isspace() for ch in cfg[k])):
            raise ValueError(f"{k} must be an e-mail address")
    if cfg["provider"] == "smtp" and not cfg["host"]:
        raise ValueError("SMTP needs a host")
    if cfg["provider"] in ("resend", "postmark") and not cfg["api_key"]:
        raise ValueError(f"{cfg['provider']} needs an API key")
    if cfg["provider"] != "off" and not (cfg["from_addr"] or cfg["username"]):
        raise ValueError("a sender address is needed")
    stored = dict(cfg)
    for k in SECRET_KEYS:
        stored[k] = crypto.encrypt(cfg[k]) if cfg[k] else ""
    db.meta_set(META_KEY, json.dumps(stored))
    _cache = dict(cfg)
    return get_config()


def public_config() -> dict[str, Any]:
    cfg = get_config()
    out = {k: ("********" if cfg[k] else "") if k in SECRET_KEYS else cfg[k] for k in DEFAULTS}
    out["env_locked"] = env_locked()
    out["configured"] = configured()
    out["counts"] = db.outbox_counts()
    return out


def configured() -> bool:
    cfg = get_config()
    p = cfg["provider"]
    if p == "smtp":
        return bool(cfg["host"] and (cfg["from_addr"] or cfg["username"]))
    if p in ("resend", "postmark"):
        return bool(cfg["api_key"] and cfg["from_addr"])
    return False


def reset() -> None:
    global _cache, _wake
    _cache = None
    _wake = None


def _sender(cfg: dict[str, Any]) -> str:
    addr = cfg["from_addr"] or cfg["username"]
    return formataddr((cfg["from_name"] or "Fluxbridge", addr)) if cfg["from_name"] else addr


# ------------------------------------------------------------- language
def lang_for_area(area_id: Optional[int]) -> str:
    """The recipient's language: the workspace's UI language, English otherwise."""
    from . import i18n
    if not area_id:
        return "en"
    try:
        s = config.load_settings(area_id=area_id)
    except Exception:  # noqa: BLE001
        return "en"
    return i18n.alert_language(s)


def lang_for_user(user_id: int) -> str:
    return lang_for_area(db.user_primary_area(user_id))


# ------------------------------------------------------------- templates
# kind → {lang → {subject, title, body (paragraphs), button, footer?}}; placeholders in {braces}
TEMPLATES: dict[str, dict[str, dict[str, Any]]] = {
    "invite": {
        "en": {"subject": "You're invited to Fluxbridge", "title": "Your invitation",
               "body": ["{inviter} invited you to Fluxbridge{role_note}.", "Create your account with the button below. The link is single-use{expiry}."],
               "button": "Create account", "foot": "If you didn't expect this, ignore this message."},
        "de": {"subject": "Einladung zu Fluxbridge", "title": "Deine Einladung",
               "body": ["{inviter} hat dich zu Fluxbridge eingeladen{role_note}.", "Erstelle dein Konto über den Button unten. Der Link ist einmal gültig{expiry}."],
               "button": "Konto erstellen", "foot": "Falls du das nicht erwartet hast, ignoriere diese Nachricht."},
    },
    "password_reset": {
        "en": {"subject": "Reset your Fluxbridge password", "title": "Password reset",
               "body": ["{who} started a password reset for your Fluxbridge account.", "Set a new password with the button below. The link is single-use and expires in 24 hours."],
               "button": "Set new password", "foot": "If you didn't expect this, contact your administrator."},
        "de": {"subject": "Fluxbridge-Passwort zurücksetzen", "title": "Passwort zurücksetzen",
               "body": ["{who} hat ein Zurücksetzen des Passworts für dein Fluxbridge-Konto gestartet.", "Setze über den Button unten ein neues Passwort. Der Link ist einmal gültig und läuft in 24 Stunden ab."],
               "button": "Neues Passwort setzen", "foot": "Falls du das nicht erwartet hast, wende dich an deinen Administrator."},
    },
    "role_request": {
        "en": {"subject": "Broadcaster request from {email}", "title": "Broadcaster request",
               "body": ["{email} asked for the Broadcaster role.", "Approve or decline it under Settings → Users."],
               "button": "Open Users", "foot": "You receive this because you are an administrator."},
        "de": {"subject": "Broadcaster-Anfrage von {email}", "title": "Broadcaster-Anfrage",
               "body": ["{email} hat die Broadcaster-Rolle beantragt.", "Freigeben oder ablehnen unter Einstellungen → Benutzer."],
               "button": "Benutzer öffnen", "foot": "Du erhältst diese Nachricht, weil du Administrator bist."},
    },
    "role_changed": {
        "en": {"subject": "Your Fluxbridge role is now {role}", "title": "Role changed",
               "body": ["{admin} set your role to {role}.", "{role_note}"],
               "button": "Open Fluxbridge", "foot": ""},
        "de": {"subject": "Deine Fluxbridge-Rolle ist jetzt {role}", "title": "Rolle geändert",
               "body": ["{admin} hat deine Rolle auf {role} gesetzt.", "{role_note}"],
               "button": "Fluxbridge öffnen", "foot": ""},
    },
    "test": {
        "en": {"subject": "Fluxbridge: test e-mail", "title": "The platform mailer works",
               "body": ["This message was sent by the platform mailer ({route}).", "Invites, reset links and role notices now arrive this way."],
               "button": "Open Fluxbridge", "foot": ""},
        "de": {"subject": "Fluxbridge: Test-E-Mail", "title": "Der Plattform-Mailer funktioniert",
               "body": ["Diese Nachricht wurde vom Plattform-Mailer gesendet ({route}).", "Einladungen, Reset-Links und Rollen-Hinweise kommen ab jetzt auf diesem Weg."],
               "button": "Fluxbridge öffnen", "foot": ""},
    },
    "backup": {
        "en": {"subject": "Fluxbridge backup {name}", "title": "Off-site backup",
               "body": ["Attached: the encrypted database snapshot {name} ({size}, sha256 {sha256}…).",
                        "Keep it somewhere safe. Decrypt on a host with the same NEXUSPRED_ENCRYPTION_KEY / SESSION_SECRET: python -m app.backups decrypt FILE.db.enc FILE.db"],
               "button": "Open Backups", "foot": "You receive this because you are an administrator and off-site backups are set to e-mail."},
        "de": {"subject": "Fluxbridge-Backup {name}", "title": "Externe Sicherung",
               "body": ["Im Anhang: der verschlüsselte Datenbank-Snapshot {name} ({size}, sha256 {sha256}…).",
                        "Sicher aufbewahren. Entschlüsseln auf einem Host mit demselben NEXUSPRED_ENCRYPTION_KEY / SESSION_SECRET: python -m app.backups decrypt FILE.db.enc FILE.db"],
               "button": "Backups öffnen", "foot": "Du erhältst diese Nachricht, weil du Administrator bist und externe Sicherungen per E-Mail eingestellt sind."},
    },
    "release": {
        "en": {"subject": "Fluxbridge {version}: what's new", "title": "What's new in {version}",
               "body": ["{notes}"], "button": "Open Fluxbridge", "foot": "You receive this because product updates are on in your mail preferences."},
        "de": {"subject": "Fluxbridge {version}: was ist neu", "title": "Neu in {version}",
               "body": ["{notes}"], "button": "Fluxbridge öffnen", "foot": "Du erhältst diese Nachricht, weil Produkt-Updates in deinen Mail-Einstellungen eingeschaltet sind."},
    },
    "announcement": {
        "en": {"subject": "{publisher}: {title}", "title": "{title}",
               "body": ["A message from {publisher} to the subscribers of {listing}:", "{body}"], "button": "Open subscriptions",
               "foot": "You receive this because you subscribe to this publisher and keep broadcaster announcements on."},
        "de": {"subject": "{publisher}: {title}", "title": "{title}",
               "body": ["Eine Nachricht von {publisher} an die Abonnenten von {listing}:", "{body}"], "button": "Abos öffnen",
               "foot": "Du erhältst diese Nachricht, weil du diesen Anbieter abonniert hast und Broadcaster-Ankündigungen eingeschaltet sind."},
    },
    "weekly": {
        "en": {"subject": "Fluxbridge weekly report {week}", "title": "Your week {week}", "body": ["{lines}"], "button": "Open the journal",
               "foot": "You receive this because the weekly report is on in your mail preferences."},
        "de": {"subject": "Fluxbridge Wochenreport {week}", "title": "Deine Woche {week}", "body": ["{lines}"], "button": "Journal öffnen",
               "foot": "Du erhältst diese Nachricht, weil der Wochenreport in deinen Mail-Einstellungen eingeschaltet ist."},
    },
    "notice": {
        "en": {"subject": "Fluxbridge: {title}", "title": "{title}", "body": ["{message}"], "button": "{button}", "foot": ""},
        "de": {"subject": "Fluxbridge: {title}", "title": "{title}", "body": ["{message}"], "button": "{button}", "foot": ""},
    },
}
ROLE_NOTES = {
    "en": {"user": "You can trade your own accounts and webhooks, run copy groups and subscribe on the marketplace.",
           "broadcaster": "You can now publish webhooks and copy groups on the marketplace and manage your subscribers.",
           "admin": "You can now manage users, roles, payments and the platform."},
    "de": {"user": "Du kannst eigene Konten und Webhooks handeln, Copy-Gruppen führen und im Marketplace abonnieren.",
           "broadcaster": "Du kannst jetzt Webhooks und Copy-Gruppen im Marketplace veröffentlichen und deine Abonnenten verwalten.",
           "admin": "Du kannst jetzt Benutzer, Rollen, Zahlungen und die Plattform verwalten."},
}
ROLE_LABELS = {"user": "User", "broadcaster": "Broadcaster", "admin": "Admin"}
_FOOTER = {"en": "Fluxbridge · TradingView to broker bridge", "de": "Fluxbridge · Brücke von TradingView zum Broker"}
_UNSUB = {"en": "Unsubscribe from these messages", "de": "Diese Nachrichten abbestellen"}


def _fill(text: str, ctx: dict[str, Any]) -> str:
    class _Safe(dict):
        def __missing__(self, k: str) -> str:
            return "{" + k + "}"
    return text.format_map(_Safe(ctx))


def render(kind: str, lang: str, ctx: dict[str, Any]) -> tuple[str, str, str]:
    """(subject, html, text) for a template in ``lang`` (en fallback)."""
    tpl = TEMPLATES[kind]
    t = tpl.get(lang) or tpl["en"]
    lang = lang if lang in tpl else "en"
    subject = _fill(t["subject"], ctx)
    title = _fill(t["title"], ctx)
    paras = [p for p in (_fill(p, ctx) for p in t["body"]) if p.strip()]
    button = _fill(t["button"], ctx) if ctx.get("url") and t.get("button") else ""
    foot = _fill(t.get("foot") or "", ctx)
    url = str(ctx.get("url") or "")
    unsub = str(ctx.get("unsubscribe_url") or "")
    e = _html.escape
    ps = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55;color:#1f2933">{"<br>".join(e(line) for line in p.split(chr(10)))}</p>' for p in paras)
    btn = (f'<p style="margin:22px 0 8px"><a href="{e(url)}" style="display:inline-block;background:#0d7c66;color:#ffffff;text-decoration:none;'
           f'font-weight:600;font-size:15px;padding:11px 20px;border-radius:6px">{e(button)}</a></p>'
           f'<p style="margin:0 0 14px;font-size:12px;color:#6b7785;word-break:break-all">{e(url)}</p>') if button else ""
    footp = f'<p style="margin:18px 0 0;font-size:13px;color:#6b7785">{e(foot)}</p>' if foot else ""
    unsubp = f'<p style="margin:8px 0 0;font-size:12px"><a href="{e(unsub)}" style="color:#6b7785">{e(_UNSUB[lang])}</a></p>' if unsub else ""
    html_doc = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>'
        '<body style="margin:0;padding:0;background:#f3f5f7;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f5f7"><tr><td align="center" style="padding:28px 12px">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#ffffff;border:1px solid #e1e5ea;border-radius:8px">'
        '<tr><td style="padding:18px 28px 0;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#0d7c66;font-weight:600">Fluxbridge</td></tr>'
        f'<tr><td style="padding:10px 28px 0"><h1 style="margin:0 0 16px;font-size:22px;line-height:1.25;color:#16202b">{e(title)}</h1>{ps}{btn}{footp}</td></tr>'
        f'<tr><td style="padding:16px 28px 22px;border-top:1px solid #e1e5ea;font-size:12px;color:#8593a1">{e(_FOOTER[lang])}{unsubp}</td></tr>'
        '</table></td></tr></table></body></html>')
    text_lines = [title, ""] + paras
    if button:
        text_lines += ["", f"{button}: {url}"]
    if foot:
        text_lines += ["", foot]
    if unsub:
        text_lines += ["", f"{_UNSUB[lang]}: {unsub}"]
    text_lines += ["", "-- ", _FOOTER[lang]]
    return subject, html_doc, "\n".join(text_lines)


# ------------------------------------------------------------- queueing
def _kick() -> None:
    if _wake is not None:
        _wake.set()


def enqueue(to_addr: str, subject: str, html_body: str, text_body: str, *, kind: str = "", area_id: Optional[int] = None,
            kick: bool = True, attachment: Optional[str] = None) -> int:
    """Queue a mail; the worker delivers it. Returns the outbox row id.
    ``kick=False`` leaves the worker asleep — for a caller that delivers the
    row itself right away (:func:`send_now`), so the two never race.
    ``attachment`` is a file path; the file is deleted once no pending row needs it."""
    row_id = db.outbox_add(to_addr, subject, html_body, text_body, kind, area_id, attachment or "")
    if kick:
        _kick()
    return row_id


def send_template(to_addr: str, kind: str, ctx: dict[str, Any], *, lang: Optional[str] = None,
                  area_id: Optional[int] = None, kick: bool = True, attachment: Optional[str] = None) -> int:
    """Render ``kind`` in the recipient's language and queue it. ``area_id`` is
    the workspace whose SMTP is the fallback route and whose language applies
    when ``lang`` is not given."""
    subject, html_body, text_body = render(kind, lang or lang_for_area(area_id), ctx)
    return enqueue(to_addr, subject, html_body, text_body, kind=kind, area_id=area_id, kick=kick, attachment=attachment)


def can_send(area_id: Optional[int] = None) -> bool:
    """Is there any route for a mail: the platform sender, else the workspace's SMTP."""
    if configured():
        return True
    if not area_id:
        return False
    s = config.load_settings(area_id=area_id)
    return bool(s.get("alert_smtp_username") and s.get("alert_smtp_password"))


def address_failures(addr: str) -> int:
    try:
        return int(db.meta_get(f"mail_fail:{addr.lower()}") or 0)
    except ValueError:
        return 0


def address_blocked(addr: str) -> bool:
    return address_failures(addr) >= BLOCK_AFTER


def _note_outcome(addr: str, ok: bool) -> None:
    key = f"mail_fail:{addr.lower()}"
    if ok:
        if db.meta_get(key):
            db.meta_set(key, "0")
    else:
        db.meta_set(key, str(address_failures(addr) + 1))


# ------------------------------------------------------------- transports
def _read_attachment(path: str) -> tuple[str, bytes]:
    p = os.path.abspath(path)
    with open(p, "rb") as f:
        return os.path.basename(p), f.read()


def _header_safe(value: str) -> str:
    """One line, no control characters: a subject with a newline in it makes the
    e-mail package refuse the whole message, so every recipient's row would fail."""
    return " ".join(str(value or "").split())[:300]


def _build(cfg_from: str, to_addr: str, subject: str, html_body: str, text_body: str, reply_to: str = "",
           attachment: str = "") -> MIMEMultipart:
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body or " ", "plain", "utf-8"))
    if html_body:
        alt.attach(MIMEText(html_body, "html", "utf-8"))
    if attachment:
        msg = MIMEMultipart("mixed")
        msg.attach(alt)
        name, data = _read_attachment(attachment)
        part = MIMEApplication(data, Name=name)
        part["Content-Disposition"] = f'attachment; filename="{name}"'
        msg.attach(part)
    else:
        msg = alt
    msg["Subject"] = _header_safe(subject)
    msg["From"] = cfg_from
    msg["To"] = to_addr
    if reply_to:
        msg["Reply-To"] = reply_to
    return msg


def _smtp_send(cfg: dict[str, Any], to_addr: str, subject: str, html_body: str, text_body: str, attachment: str = "") -> None:
    from .alerts import _check_smtp_target
    host, port = cfg["host"], int(cfg["port"] or 587)
    _check_smtp_target(host, port)
    msg = _build(_sender(cfg), to_addr, subject, html_body, text_body, cfg.get("reply_to", ""), attachment)
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if port != 465:
            server.starttls(context=ctx)
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        server.send_message(msg)


def _workspace_send(area_id: int, to_addr: str, subject: str, html_body: str, text_body: str, attachment: str = "") -> None:
    """Fallback: the workspace's own SMTP (the user's alert channel)."""
    from .alerts import _check_smtp_target
    s = config.load_settings(area_id=area_id)
    username, password = s.get("alert_smtp_username"), s.get("alert_smtp_password")
    if not username or not password:
        raise RuntimeError("no platform mailer and no workspace SMTP")
    host = s.get("alert_smtp_host") or "smtp.gmail.com"
    port = int(s.get("alert_smtp_port") or 587)
    _check_smtp_target(host, port)
    msg = _build(str(username), to_addr, subject, html_body, text_body, "", attachment)
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(str(username), str(password))
        server.send_message(msg)


async def _api_send(cfg: dict[str, Any], to_addr: str, subject: str, html_body: str, text_body: str, attachment: str = "") -> None:
    client = http.client("mail")
    att = None
    if attachment:
        name, data = await asyncio.to_thread(_read_attachment, attachment)
        att = (name, base64.b64encode(data).decode())
    if cfg["provider"] == "resend":
        payload: dict[str, Any] = {"from": _sender(cfg), "to": [to_addr], "subject": subject, "html": html_body, "text": text_body}
        if cfg.get("reply_to"):
            payload["reply_to"] = cfg["reply_to"]
        if att:
            payload["attachments"] = [{"filename": att[0], "content": att[1]}]
        r = await client.post(RESEND_API, json=payload, headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=60)
    else:
        payload = {"From": _sender(cfg), "To": to_addr, "Subject": subject, "HtmlBody": html_body, "TextBody": text_body, "MessageStream": "outbound"}
        if cfg.get("reply_to"):
            payload["ReplyTo"] = cfg["reply_to"]
        if att:
            payload["Attachments"] = [{"Name": att[0], "Content": att[1], "ContentType": "application/octet-stream"}]
        r = await client.post(POSTMARK_API, json=payload, headers={"X-Postmark-Server-Token": cfg["api_key"], "Accept": "application/json"}, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{cfg['provider']} answered {r.status_code}: {r.text[:200]}")


async def _deliver(row: dict[str, Any]) -> str:
    """Send one outbox row; returns the route used. Raises on failure."""
    cfg = get_config()
    att = str(row.get("attachment") or "")
    if att and not os.path.exists(att):
        raise RuntimeError(f"attachment {os.path.basename(att)} is gone")
    if configured():
        if cfg["provider"] == "smtp":
            await asyncio.to_thread(_smtp_send, cfg, row["to"], row["subject"], row["html"], row["text"], att)
            return f"smtp:{cfg['host']}"
        await _api_send(cfg, row["to"], row["subject"], row["html"], row["text"], att)
        return cfg["provider"]
    if row.get("area_id"):
        await asyncio.to_thread(_workspace_send, int(row["area_id"]), row["to"], row["subject"], row["html"], row["text"], att)
        return "workspace-smtp"
    raise RuntimeError("no platform mailer configured")


def _release_attachment(row: dict[str, Any]) -> None:
    """Delete the attachment file once no pending row needs it any more."""
    att = str(row.get("attachment") or "")
    if att and not db.outbox_attachment_in_use(att):
        try:
            os.unlink(att)
        except OSError:
            pass


async def deliver_pending(limit: int = 20) -> dict[str, int]:
    """One pass over the due rows. Returns counts {sent, failed, retry}."""
    out = {"sent": 0, "failed": 0, "retry": 0}
    for row in db.outbox_due(limit):
        try:
            route = await _deliver(row)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"[:400]
            attempt = int(row["attempts"]) + 1
            if attempt >= MAX_ATTEMPTS:
                db.outbox_failed(row["id"], err, None)
                out["failed"] += 1
                _note_outcome(row["to"], False)
                _release_attachment(row)
                with context.use_area(context.DEFAULT_AREA_ID):
                    state.log_event("warn", f"E-mail to {row['to']} ({row['kind'] or 'mail'}) given up after {attempt} attempts: {err}")
            else:
                db.outbox_failed(row["id"], err, BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
                out["retry"] += 1
            continue
        db.outbox_sent(row["id"], route)
        _note_outcome(row["to"], True)
        _release_attachment(row)
        out["sent"] += 1
    return out


async def send_now(row_id: int) -> dict[str, Any]:
    """Deliver one row immediately (the admin's test / retry button). Returns the row."""
    row = db.outbox_get(row_id)
    if not row or row["status"] == "sent":
        return row or {}
    try:
        route = await _deliver(row)
    except Exception as exc:  # noqa: BLE001
        db.outbox_failed(row_id, f"{type(exc).__name__}: {exc}"[:400], BACKOFF_S[0])
        _note_outcome(row["to"], False)
    else:
        db.outbox_sent(row_id, route)
        _note_outcome(row["to"], True)
        _release_attachment(row)
    return db.outbox_get(row_id) or {}


async def outbox_loop() -> None:
    """Background worker: delivers due rows, woken early by :func:`enqueue`."""
    global _wake
    _wake = asyncio.Event()
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), timeout=LOOP_TICK_S)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
        try:
            await deliver_pending()
        except Exception as exc:  # noqa: BLE001
            log.warning("outbox pass failed: %s", exc)
            await asyncio.sleep(1.0)


# ------------------------------------------------------------- helpers for callers
def role_ctx(role: str, lang: str, admin_email: str) -> dict[str, Any]:
    return {"role": ROLE_LABELS.get(role, role), "admin": admin_email, "role_note": ROLE_NOTES.get(lang, ROLE_NOTES["en"]).get(role, "")}


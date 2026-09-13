"""Platform operations config and admin broadcasts (alpha.99).

One admin-level config record (``meta.platform``) for what applies to the
whole bridge rather than a workspace: the Telegram bot, the Twilio SMS sender,
the latency / loop-lag thresholds, the canary signal, and the current banner.
Plus the admin's *broadcast*: a message to every user (or one role) as inbox
row, mail and a dismissable banner for a set number of hours.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config, crypto, db, web

META_KEY = "platform"
BANNER_KEY = "platform:banner"
DEFAULTS: dict[str, Any] = {
    "telegram_bot_token": "", "telegram_bot_name": "",
    "twilio_sid": "", "twilio_token": "", "twilio_from": "",
    "latency_p95_warn_ms": 500, "loop_lag_warn_ms": 100,
    "canary_enabled": False, "canary_minutes": 5, "canary_max_ms": 2000,
}
SECRET_KEYS = ("telegram_bot_token", "twilio_token")
_cache: Optional[dict[str, Any]] = None


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
    return dict(_cache)


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    global _cache
    cfg = get_config()
    for k in ("telegram_bot_name", "twilio_sid", "twilio_from"):
        if k in updates:
            v = str(updates[k] or "").strip()
            if len(v) > 200 or any(ch.isspace() for ch in v):
                raise ValueError(f"{k} looks wrong")
            cfg[k] = v
    for k in SECRET_KEYS:
        if k in updates and updates[k] != "********":
            cfg[k] = str(updates[k] or "").strip()[:300]
    for k, lo, hi in (("latency_p95_warn_ms", 50, 60000), ("loop_lag_warn_ms", 20, 10000), ("canary_minutes", 1, 1440), ("canary_max_ms", 100, 60000)):
        if k in updates:
            try:
                n = int(float(updates[k]))
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{k} must be a whole number")
            if not lo <= n <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cfg[k] = n
    if "canary_enabled" in updates:
        cfg["canary_enabled"] = bool(updates["canary_enabled"])
    if cfg["twilio_from"] and not cfg["twilio_from"].startswith("+"):
        raise ValueError("the Twilio sender must be an E.164 number (+41…)")
    stored = dict(cfg)
    for k in SECRET_KEYS:
        stored[k] = crypto.encrypt(cfg[k]) if cfg[k] else ""
    db.meta_set(META_KEY, json.dumps(stored))
    _cache = dict(cfg)
    return dict(cfg)


def public_config() -> dict[str, Any]:
    cfg = get_config()
    return {**{k: ("********" if cfg[k] else "") if k in SECRET_KEYS else cfg[k] for k in DEFAULTS},
            "telegram_configured": bool(cfg["telegram_bot_token"]), "sms_configured": bool(cfg["twilio_sid"] and cfg["twilio_token"] and cfg["twilio_from"])}


def reset() -> None:
    global _cache
    _cache = None


# ------------------------------------------------------------------ broadcast + banner
def banner() -> Optional[dict[str, Any]]:
    raw = db.meta_get(BANNER_KEY)
    if not raw:
        return None
    try:
        b = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(b, dict) or str(b.get("until") or "") < datetime.now(timezone.utc).isoformat():
        return None
    return b


def banner_for(user: dict[str, Any]) -> Optional[dict[str, Any]]:
    b = banner()
    if not b:
        return None
    roles = b.get("roles") or []
    return b if not roles or web.role_of(user) in roles else None


def clear_banner() -> None:
    db.meta_set(BANNER_KEY, "")


async def broadcast(admin: dict[str, Any], title: str, body: str, *, roles: Optional[list[str]] = None,
                    mail: bool = True, banner_hours: int = 0) -> dict[str, Any]:
    """Admin → every user of the chosen roles: inbox row, mail through the
    platform mailer, and (``banner_hours`` > 0) a banner every viewer can dismiss."""
    from . import alerts, mailer
    title, body = str(title or "").strip()[:140], str(body or "").strip()[:2000]
    if not title or not body:
        raise ValueError("title and text are required")
    roles = [r for r in (roles or []) if r in db.ROLES]
    targets = [u for u in db.list_users() if not roles or web.role_of(u) in roles]
    mailed = 0
    for u in targets:
        area = db.user_primary_area(u["id"])
        if not area:
            continue
        from . import context
        with context.use_area(area):
            alerts._inbox("broadcast", "warn", title, body, "/#/")
        if mail and mailer.configured():
            lang = mailer.lang_for_area(area)
            mailer.send_template(u["email"], "notice", {"title": title, "message": body, "button": "Open Fluxbridge" if lang == "en" else "Fluxbridge öffnen",
                                                         "url": (config.PUBLIC_URL or "") + "/"}, lang=lang, area_id=area)
            mailed += 1
    bid = ""
    if banner_hours > 0:
        bid = "bn_" + secrets.token_urlsafe(6)
        db.meta_set(BANNER_KEY, json.dumps({"id": bid, "title": title, "body": body, "roles": roles,
                                           "until": (datetime.now(timezone.utc) + timedelta(hours=min(int(banner_hours), 24 * 30))).isoformat(),
                                           "by": admin["email"], "at": datetime.now(timezone.utc).isoformat()}))
    db.log_action(admin["id"], admin["email"], "broadcast", title, f"{len(targets)} user(s), {mailed} mailed" + (f", banner {banner_hours} h" if bid else ""))
    return {"recipients": len(targets), "mailed": mailed, "banner_id": bid}

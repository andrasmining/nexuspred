"""Release notes and mail preferences (alpha.97).

* **What's new** — the CHANGELOG section of the running version, filtered for
  the reader's role (a User is not told about admin pages), shown once per
  user after an update and served at ``/api/whatsnew``.
* **Release mail** — after a deploy, the same section goes out once through
  the platform mailer to every user who keeps "product updates" on.
* **Mail preferences** — per user: product updates, weekly report, marketplace
  news, broadcaster announcements. Transactional mail is never optional.
  Every non-transactional mail carries a signed one-click unsubscribe link.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import auth, config, db

PREFS_KEY = "mail_prefs:{uid}"
SEEN_KEY = "seen_version:{uid}"
MAILED_KEY = "release_mailed"
PREF_DEFAULTS = {"updates": True, "weekly": True, "marketplace": True, "announcements": True}
PREF_LABELS = {"en": {"updates": "product updates", "weekly": "the weekly report", "marketplace": "marketplace news", "announcements": "broadcaster announcements"},
               "de": {"updates": "Produkt-Updates", "weekly": "den Wochenreport", "marketplace": "Marketplace-Neuigkeiten", "announcements": "Broadcaster-Ankündigungen"}}
_ADMIN_HINTS = ("admin", "Settings → Users", "Settings → Platform", "Settings → Backups", "Settings → Payments", "Settings → Updates", "NEXUSPRED_", "/readyz", "operator")
_BROADCASTER_HINTS = ("Broadcaster", "publisher", "subscribers", "listing")


# ------------------------------------------------------------- changelog
def sections() -> dict[str, list[str]]:
    """version → bullet list (markdown bullets joined, continuation lines merged)."""
    try:
        text = (config.ROOT_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, list[str]] = {}
    cur: Optional[str] = None
    for line in text.splitlines():
        m = re.match(r"^## (\S+)", line)
        if m:
            cur = m.group(1)
            out[cur] = []
            continue
        if cur is None:
            continue
        if line.startswith("- "):
            out[cur].append(line[2:].strip())
        elif line.startswith("  ") and out[cur]:
            out[cur][-1] += " " + line.strip()
        elif line.strip() and not out[cur] and not line.startswith("#"):
            out[cur].append(line.strip())                         # a leading sentence (e.g. "Package 2 …")
    return out


def _bullet_for_role(bullet: str, role: str) -> bool:
    if role == "admin":
        return True
    if any(h in bullet for h in _ADMIN_HINTS):
        return False
    if role == "user" and any(h in bullet for h in _BROADCASTER_HINTS) and "subscribe" not in bullet.lower():
        return False
    return True


def notes_for(role: str, version: Optional[str] = None) -> dict[str, Any]:
    v = version or config.get_version()
    bullets = sections().get(v, [])
    kept = [b for b in bullets if _bullet_for_role(b, role)]
    return {"version": v, "bullets": kept, "all": len(bullets)}


def _plain(bullet: str) -> str:
    return re.sub(r"\*\*|`", "", bullet)


# ------------------------------------------------------------- what's new (per user)
def seen_version(user_id: int) -> str:
    return db.meta_get(SEEN_KEY.format(uid=user_id)) or ""


def mark_seen(user_id: int, version: Optional[str] = None) -> None:
    db.meta_set(SEEN_KEY.format(uid=user_id), version or config.get_version())


def whats_new(user: dict[str, Any]) -> dict[str, Any]:
    """``first_login``: an account created in the last day has nothing to catch up
    on — the notes are marked seen silently. Everyone else sees the current
    version's notes once."""
    from datetime import datetime, timedelta, timezone
    from . import web
    v = config.get_version()
    notes = notes_for(web.role_of(user), v)
    seen = seen_version(user["id"])
    fresh = False
    try:
        created = datetime.fromisoformat(str(user.get("created_at") or ""))
        fresh = datetime.now(timezone.utc) - created < timedelta(days=1)
    except ValueError:
        fresh = False
    return {**notes, "seen": seen == v, "first_login": seen == "" and fresh}


# ------------------------------------------------------------- mail preferences
def prefs(user_id: int) -> dict[str, bool]:
    raw = db.meta_get(PREFS_KEY.format(uid=user_id))
    out = dict(PREF_DEFAULTS)
    if raw:
        try:
            out.update({k: bool(v) for k, v in json.loads(raw).items() if k in PREF_DEFAULTS})
        except ValueError:
            pass
    return out


def save_prefs(user_id: int, updates: dict[str, Any]) -> dict[str, bool]:
    p = prefs(user_id)
    p.update({k: bool(updates[k]) for k in PREF_DEFAULTS if k in updates})
    db.meta_set(PREFS_KEY.format(uid=user_id), json.dumps(p))
    return p


def wants(user_id: int, pref: str) -> bool:
    return prefs(user_id).get(pref, True)


def unsubscribe_token(user_id: int, pref: str) -> str:
    body = auth._b64e(json.dumps({"u": int(user_id), "p": pref}).encode())
    return f"{body}.{auth._sign(body)}"


def unsubscribe_url(user_id: int, pref: str) -> str:
    return f"{config.PUBLIC_URL or ''}/unsubscribe?t={unsubscribe_token(user_id, pref)}"


def apply_unsubscribe(token: str) -> Optional[tuple[int, str]]:
    """Validate a token and switch that preference off. Returns (user_id, pref) or None."""
    import hmac
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, auth._sign(body)):
        return None
    try:
        payload = json.loads(auth._b64d(body))
        uid, pref = int(payload["u"]), str(payload["p"])
    except (ValueError, KeyError, TypeError):
        return None
    if pref not in PREF_DEFAULTS or not db.get_user(uid):
        return None
    save_prefs(uid, {pref: False})
    return uid, pref


# ------------------------------------------------------------- release mail
def mail_release(version: Optional[str] = None) -> int:
    """Once per version: the release notes to every user with product updates on.
    Returns how many mails were queued (0 when already sent or nothing to say)."""
    from . import mailer, web
    v = version or config.get_version()
    if db.meta_get(MAILED_KEY) == v:
        return 0
    db.meta_set(MAILED_KEY, v)
    if not sections().get(v):
        return 0
    if not mailer.configured():
        return 0                                                  # no platform route: never through a user's own SMTP
    n = 0
    for u in db.list_users():
        if not wants(u["id"], "updates"):
            continue
        area = db.user_primary_area(u["id"])
        lang = mailer.lang_for_area(area)
        notes = notes_for(web.role_of(u), v)
        if not notes["bullets"]:
            continue
        body = "\n\n".join("• " + _plain(b) for b in notes["bullets"])
        mailer.send_template(u["email"], "release", {"version": v, "notes": body, "url": (config.PUBLIC_URL or "") + "/",
                                                     "unsubscribe_url": unsubscribe_url(u["id"], "updates")}, lang=lang, area_id=area)
        n += 1
    return n

"""Helpers shared by the routers: template engine, session-cookie plumbing,
the admin gate, and the paths reachable without a login."""
from __future__ import annotations

import re

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, auth, config, i18n

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Paths reachable without a login session: the webhook (TradingView can't send
# auth), static assets, health check, guide/favicon, and the auth pages.
# Prefixes match whole subtrees; pages match exactly (``/loginx`` is *not* exempt).
AUTH_EXEMPT_PREFIXES = ("/webhook/", "/static/", "/api/agent/")
AUTH_EXEMPT_PATHS = frozenset({
    "/healthz", "/readyz", "/status", "/unsubscribe", "/ack", "/api/public/status", "/metrics", "/guide", "/favicon.ico", "/sw.js", "/api/payments/webhook",
    "/login", "/logout", "/register", "/setup", "/reset", "/login/2fa",
})
# Paths a signed-in user who still has to enrol in two-factor may reach.
MFA_SETUP_PATHS = frozenset({"/2fa/setup", "/logout", "/api/me"})
MFA_SETUP_PREFIXES = ("/api/account/2fa",)


def mfa_setup_allowed(path: str) -> bool:
    return path in MFA_SETUP_PATHS or path.startswith(MFA_SETUP_PREFIXES)
AUTH_EXEMPT = AUTH_EXEMPT_PREFIXES + tuple(sorted(AUTH_EXEMPT_PATHS))  # backwards-compat alias


def is_auth_exempt(path: str) -> bool:
    return path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES)


def render(request: Request, name: str, context: dict[str, Any] | None = None) -> HTMLResponse:
    """Render a template with the request and its CSP nonce in scope."""
    lang = i18n.language_of(request)
    tr = i18n.translator(lang)
    ctx = {"nonce": getattr(request.state, "csp_nonce", ""), "lang": lang, "t": tr}
    if context:
        ctx.update(context)
        if ctx.get("error"):
            ctx["error"] = tr(str(ctx["error"]))       # the auth forms' error texts are English source strings
    return templates.TemplateResponse(request, name, ctx)


def secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def set_session_cookie(resp: Response, request: Request, user_id: int) -> None:
    resp.set_cookie(auth.COOKIE, auth.make_session(user_id), max_age=auth.SESSION_TTL,
                    httponly=True, secure=secure(request), samesite="lax", path="/")


def base_url(request: Request) -> str:
    """The origin to build absolute links (invites, password resets) on.

    ``NEXUSPRED_PUBLIC_URL`` pins it (recommended on any public host: a forged
    ``Host`` header can then never end up in an emailed link); otherwise the
    request's own scheme + host are used."""
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def require_feature(request: Request, feature: str) -> None:
    """403 unless the caller's area has been granted ``feature`` by an admin."""
    from . import db
    area = getattr(request.state, "area_id", None)
    if area is None or not db.get_area_features(area).get(feature):
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' is not enabled for your account")


# --------------------------------------------------------------------- roles
# Three roles, one per user, platform-wide, each including the one below it:
#   user         consumes — own broker logins, own webhooks, subscribes, follows, protects
#   broadcaster  produces and sells — publishes listings and copy groups, manages subscribers, simulator
#   admin        operates — users and roles, payments, news, updates, Discord, moderation, support view
ROLE_RANK = {"user": 0, "broadcaster": 1, "admin": 2}
ROLE_LABEL = {"user": "User", "broadcaster": "Broadcaster", "admin": "Admin"}


def role_of(user: dict[str, Any] | None) -> str:
    r = str((user or {}).get("role") or "")
    if r in ROLE_RANK:
        return r
    return "admin" if (user or {}).get("is_admin") else "user"


def has_role(user: dict[str, Any] | None, min_role: str) -> bool:
    return ROLE_RANK[role_of(user)] >= ROLE_RANK[min_role]


def require_role(request: Request, min_role: str) -> dict[str, Any]:
    """403 unless the caller holds ``min_role`` or a higher one. Returns the user."""
    user = getattr(request.state, "user", None)
    if not user or not has_role(user, min_role):
        raise HTTPException(status_code=403, detail=f"{ROLE_LABEL[min_role]} role required")
    sup = getattr(request.state, "support", None)
    if sup and request.method not in ("GET", "HEAD") and not sup.get("write") and request.url.path not in ("/api/support/exit", "/api/support/note"):
        raise HTTPException(status_code=403, detail="Support view is read-only")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    return require_role(request, "admin")


def capabilities(user: dict[str, Any] | None) -> dict[str, bool]:
    """What the dashboard may show; the server re-checks every call regardless."""
    r = role_of(user)
    bc, ad = has_role(user, "broadcaster"), has_role(user, "admin")
    return {"role": r, "trade": True, "webhooks": True, "subscribe": True, "follow": True, "agents": True,
            "publish": bc, "lead": True, "simulator": bc, "settings_io": True,
            "admin": ad, "users": ad, "payments_config": ad, "news": ad, "updates": ad, "discord": ad, "support": ad}


# Route → minimum role, checked by the gate middleware for every request, so a
# forgotten per-route check can never open a producer or operator endpoint to a
# consumer. Entries: (methods or None for all, prefix, min role). First match wins.
ROUTE_POLICY: list[tuple[tuple[str, ...] | None, str, str]] = [
    (("PUT", "POST", "DELETE"), "/api/payments/config", "admin"),
    (None, "/api/users", "admin"),
    (None, "/api/invites", "admin"),
    (None, "/api/audit", "admin"),
    (None, "/api/update/", "admin"),
    (("PUT", "POST", "DELETE"), "/api/news/", "admin"),
    (None, "/api/discord/", "admin"),
    (None, "/api/support/exit", "admin"),
    (None, "/api/mail/", "admin"),
    (None, "/api/backups", "admin"),
    (None, "/api/platform/", "admin"),
    (None, "/api/incidents", "admin"),
    (None, "/api/broadcaster/", "broadcaster"),
    (None, "/api/broadcast", "admin"),
    (("POST",), "/api/update/rollback", "admin"),
    (None, "/api/announcements", "broadcaster"),
    (("PUT", "POST", "DELETE"), "/api/webhooks", "user"),           # own webhooks: every role (sharing / subscribers: see the exact rules)
    (None, "/api/simulator", "broadcaster"),
    (None, "/api/simulate", "broadcaster"),
    (None, "/api/scenarios", "broadcaster"),
    (None, "/api/settings/export", "user"),           # alpha.99: a User takes their workspace with them
    (None, "/api/settings/import", "user"),
]
# finer rules that must beat the prefixes above
ROUTE_POLICY_EXACT: list[tuple[tuple[str, ...] | None, "re.Pattern[str]", str]] = [
    (None, re.compile(r"^/api/webhooks/[^/]+/(sharing|subscribers)(/|$)"), "broadcaster"),   # publish and manage subscribers
    (None, re.compile(r"^/api/copy/groups/[^/]+/(sharing|subscribers)(/|$)"), "broadcaster"),
    (("GET",), re.compile(r"^/api/users/directory$"), "broadcaster"),                        # the "selected users" pick list
    (None, re.compile(r"^/api/me(/|$)"), "user"),
]


SUPPORT_COOKIE = "fb_support"
SUPPORT_TTL = 2 * 3600


def make_support_cookie(admin_id: int, area_id: int, email: str) -> str:
    import json, time
    body = auth._b64e(json.dumps({"kind": "support", "adm": int(admin_id), "area": int(area_id), "email": email,
                                  "exp": int(time.time()) + SUPPORT_TTL}).encode())
    return f"{body}.{auth._sign(body)}"


def read_support_cookie(cookie: str | None, admin_id: int) -> dict[str, Any] | None:
    """The support view an admin entered, or None (bound to the admin, two hours).

    The signed cookie is an authenticated *request* to view a workspace, not a
    two-hour authorization lease. Re-check the target on every request so a
    deleted/reassigned workspace or a user promoted to Admin cannot remain
    visible through a stale support cookie.
    """
    import hmac, json, time
    if not cookie or "." not in cookie:
        return None
    body, _, sig = cookie.partition(".")
    try:
        if not hmac.compare_digest(sig, auth._sign(body)):
            return None
        payload = json.loads(auth._b64d(body))
        if payload.get("kind") != "support" or int(payload.get("adm", 0)) != int(admin_id) or int(payload.get("exp", 0)) < time.time():
            return None
        area_id = int(payload["area"])
        email = str(payload.get("email") or "")
        from . import db
        target = db.get_user_by_email(email)
        if not target or db.user_primary_area(target["id"]) != area_id or int(target["id"]) == int(admin_id):
            return None
        if has_role(target, "admin") and int(admin_id) != 1:
            return None
        return {"area_id": area_id, "email": email}
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------- alpha.99: support grant + quotas
GRANT_KEY = "support_grant:{area}"


def support_grant(area_id: int) -> dict[str, Any] | None:
    import json, time
    raw = db.meta_get(GRANT_KEY.format(area=area_id))
    if not raw:
        return None
    try:
        g = json.loads(raw)
    except ValueError:
        return None
    return g if isinstance(g, dict) and float(g.get("until", 0)) > time.time() else None


def support_grant_active(area_id: int) -> bool:
    return support_grant(area_id) is not None


def set_support_grant(area_id: int, hours: float, by: str) -> dict[str, Any] | None:
    import json, time
    if hours <= 0:
        db.meta_set(GRANT_KEY.format(area=area_id), "")
        return None
    g = {"until": time.time() + min(float(hours), 72) * 3600, "by": by}
    db.meta_set(GRANT_KEY.format(area=area_id), json.dumps(g))
    return g


QUOTAS: dict[str, dict[str, int | None]] = {
    "user": {"webhooks": 5, "groups": 3, "agents": 2},
    "broadcaster": {"webhooks": 25, "groups": 10, "agents": 5},
    "admin": {"webhooks": None, "groups": None, "agents": None},
}


def quota_for(user: dict[str, Any] | None) -> dict[str, int | None]:
    """The role's limits, overridden per user by the admin (meta ``quota:<uid>``)."""
    import json
    q = dict(QUOTAS[role_of(user)])
    if user and user.get("id"):
        raw = db.meta_get(f"quota:{user['id']}")
        if raw:
            try:
                for k, v in json.loads(raw).items():
                    if k in q:
                        q[k] = None if v in (None, "", 0) else int(v)
            except (ValueError, TypeError):
                pass
    return q


def quota_usage(area_id: int, user: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    from . import copy as cp
    q = quota_for(user)
    s = config.load_settings(area_id=area_id)
    used = {"webhooks": len(s.get("webhooks") or []), "groups": len(cp.load_groups(area_id)), "agents": len(db.list_agents(area_id))}
    return {k: {"used": used[k], "max": q[k]} for k in q}


def check_quota(area_id: int, user: dict[str, Any] | None, what: str) -> None:
    """403 when the workspace has used up its ``what`` (webhooks / groups / agents)."""
    u = quota_usage(area_id, user)[what]
    if u["max"] is not None and u["used"] >= u["max"]:
        label = {"webhooks": "webhooks", "groups": "copy groups", "agents": "execution agents"}[what]
        raise HTTPException(status_code=403, detail=f"Quota reached: {u['used']} of {u['max']} {label} — ask your admin for more")


def min_role_for(method: str, path: str) -> str | None:
    for methods, pat, role in ROUTE_POLICY_EXACT:
        if (methods is None or method in methods) and pat.match(path):
            return role
    for methods, prefix, role in ROUTE_POLICY:
        if (methods is None or method in methods) and (path == prefix or path.startswith(prefix)):
            return role
    return None

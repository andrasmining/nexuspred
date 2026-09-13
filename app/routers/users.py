"""User & admin management: current user, users/invites/features, audit log,
self-service password change, admin-issued password resets."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import alerts, config, context, db, mailer, state
from ..discord_signals import listener as discord_listener
from .. import roles, web
from ..web import base_url, require_admin, require_role, set_session_cookie

router = APIRouter(prefix="/api", tags=["users"])


def guard_admin_target(admin: dict[str, Any], target: dict[str, Any] | None, what: str) -> None:
    """Cross-admin actions are reserved for the bootstrap admin (user 1): one
    admin must not take over or lock out another admin's workspace."""
    if target and target.get("is_admin") and target["id"] != admin["id"] and admin["id"] != 1:
        raise HTTPException(status_code=403, detail=f"Only the bootstrap admin can {what}")


@router.get("/me")
async def api_me(request: Request) -> dict[str, Any]:
    u = request.state.user
    support = getattr(request.state, "support", None)
    return {"id": u["id"], "email": u["email"], "is_admin": u["is_admin"], "role": web.role_of(u),
            "role_request": u.get("role_request") or "", "capabilities": web.capabilities(u),
            "features": db.user_features(u["id"]),
            "support": {"area_id": support["area_id"], "email": support["email"]} if support else None,
            "totp_enabled": bool(u.get("totp_enabled")), "totp_required": bool(u.get("totp_required")),
            "mail_blocked": mailer.address_blocked(u["email"])}


@router.post("/me/role-request")
async def api_role_request(request: Request) -> dict[str, Any]:
    """A user asks to become a Broadcaster; an admin approves in Settings → Users."""
    user = require_role(request, "user")
    body = await request.json()
    wanted = str(body.get("role") or "broadcaster")
    if wanted != "broadcaster" or web.has_role(user, "broadcaster"):
        raise HTTPException(status_code=400, detail="Only the Broadcaster role can be requested")
    db.request_role(user["id"], "broadcaster")
    db.log_action(user["id"], user["email"], "role_request", user["email"], "broadcaster")
    state.log_event("info", f"{user['email']} asked for the Broadcaster role — approve it under Settings → Users")
    with context.use_area(context.DEFAULT_AREA_ID):                       # the operator's workspace hears it too
        state.log_event("info", f"{user['email']} asked for the Broadcaster role — approve it under Settings → Users")
    try:
        await alerts.notify_admins("role_request", {"email": user["email"], "url": base_url(request) + "/#/settings/users"})
    except Exception:  # noqa: BLE001 - an alert channel must never fail the request
        pass
    return {"status": "requested", "role": "broadcaster"}


@router.delete("/me/role-request")
async def api_role_request_withdraw(request: Request) -> dict[str, Any]:
    user = require_role(request, "user")
    db.clear_role_request(user["id"])
    return {"status": "withdrawn"}


@router.post("/users/{user_id}/role")
async def api_set_role(request: Request, user_id: int) -> dict[str, Any]:
    """Admin sets a user's role. Losing Broadcaster unpublishes and disables
    what only a Broadcaster may run (nothing is deleted)."""
    admin = require_admin(request)
    body = await request.json()
    role = str(body.get("role") or "")
    if role not in db.ROLES:
        raise HTTPException(status_code=400, detail="role must be user, broadcaster or admin")
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    if user_id == 1 and role != "admin":
        raise HTTPException(status_code=403, detail="The bootstrap admin stays admin")
    if target["id"] == admin["id"] and role != "admin" and db.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="You are the last admin — promote someone else first")
    guard_admin_target(admin, target, "change another administrator's role")
    before = web.role_of(target)
    if before == role:
        db.clear_role_request(user_id)
        return {"user_id": user_id, "role": role, "effects": {}}
    effects: dict[str, int] = {}
    if web.ROLE_RANK[before] >= web.ROLE_RANK["broadcaster"] and web.ROLE_RANK[role] < web.ROLE_RANK["broadcaster"]:
        area = db.user_primary_area(user_id)
        if area:
            effects = await roles.demote_publisher(area)
    db.set_role(user_id, role)
    if mailer.can_send(context.get_area()):
        lang = mailer.lang_for_user(user_id)
        mailer.send_template(target["email"], "role_changed", {**mailer.role_ctx(role, lang, admin["email"]), "url": base_url(request) + "/"},
                             lang=lang, area_id=context.get_area())
    db.log_action(admin["id"], admin["email"], "role_set", target["email"], f"{before} → {role}"
                  + (f" ({effects['listings']} listings unpublished, {effects['groups']} groups disabled)" if effects else ""))
    state.log_event("info", f"Role of {target['email']} set to {role} by {admin['email']}")
    return {"user_id": user_id, "role": role, "effects": effects}


@router.post("/users/{user_id}/support")
async def api_support_enter(request: Request, user_id: int) -> JSONResponse:
    """Admin opens a user's workspace read-only (support view): every read shows
    the user's data, every write is refused until the admin leaves the view."""
    admin = require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="That is your own workspace")
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    guard_admin_target(admin, target, "look into another administrator's workspace")
    area = db.user_primary_area(user_id)
    if not area:
        raise HTTPException(status_code=404, detail="User has no workspace")
    db.log_action(admin["id"], admin["email"], "support_view", target["email"], "entered (read-only)")
    state.log_event("info", f"{admin['email']} opened the support view of {target['email']} (read-only)")
    resp = JSONResponse({"status": "support", "area_id": area, "email": target["email"]})
    resp.set_cookie(web.SUPPORT_COOKIE, web.make_support_cookie(admin["id"], area, target["email"]), max_age=web.SUPPORT_TTL,
                    httponly=True, secure=web.secure(request), samesite="lax", path="/")
    return resp


@router.post("/support/exit")
async def api_support_exit(request: Request) -> JSONResponse:
    user = request.state.user
    support = getattr(request.state, "support", None)
    if support:
        db.log_action(user["id"], user["email"], "support_view", support.get("email", ""), "left")
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(web.SUPPORT_COOKIE, path="/")
    return resp


@router.get("/users")
async def api_users(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"users": db.list_users(), "features": db.FEATURES}


@router.get("/users/directory")
async def api_users_directory(request: Request) -> dict[str, Any]:
    """Who a listing can be limited to ("only selected users"): id and e-mail,
    nothing else — the Broadcaster's pick list, not the admin's user table."""
    me = require_role(request, "broadcaster")
    return {"users": [{"id": u["id"], "email": u["email"]} for u in db.list_users() if u["id"] != me["id"]]}


@router.post("/users/{user_id}/features")
async def api_set_user_feature(request: Request, user_id: int) -> dict[str, Any]:
    """Admin toggles a feature entitlement (e.g. Discord Signals) for a user."""
    admin = require_admin(request)
    body = await request.json()
    feature = str(body.get("feature", ""))
    enabled = bool(body.get("enabled"))
    if feature not in db.FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    area_id = db.user_primary_area(user_id)
    if not area_id:
        raise HTTPException(status_code=404, detail="User has no area")
    feats = db.set_area_feature(area_id, feature, enabled)
    target = (db.get_user(user_id) or {}).get("email", str(user_id))
    db.log_action(admin["id"], admin["email"], "feature_set", target,
                  f"{feature} = {'on' if enabled else 'off'}")
    # Nudge the area's Discord listener so it (dis)connects promptly; the
    # supervisor re-reads the entitlement each loop, so this is only a shortcut.
    try:
        with context.use_area(area_id):
            discord_listener.manager_for(area_id).start()
    except Exception:  # noqa: BLE001
        pass
    return {"user_id": user_id, "features": feats}


@router.post("/users/invite")
async def api_create_invite(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    # Accept the neutral `elevated` key (what the dashboard sends) and fall back
    # to the legacy `is_admin`. The client avoids the `is_admin` key because some
    # WAFs block request bodies containing it as a privilege-escalation attempt.
    elevate = body.get("elevated")
    if elevate is None:
        elevate = body.get("is_admin")
    role = str(body.get("role") or ("admin" if elevate else "user"))
    if role not in db.ROLES:
        raise HTTPException(status_code=400, detail="role must be user, broadcaster or admin")
    email = str(body.get("email", "")).strip()
    code = db.create_invite(admin["id"], email=email, role=role)
    db.log_action(admin["id"], admin["email"], "invite_create", email or "anyone",
                  f"{role} invite" if role != "user" else "")
    url = f"{base_url(request)}/register?code={code}"
    emailed = False
    area = context.get_area()
    if email and "@" in email and bool(body.get("send_email")) and mailer.can_send(area):
        lang = mailer.lang_for_area(area)
        note = {"en": {"broadcaster": " as a Broadcaster", "admin": " as an administrator"}, "de": {"broadcaster": " als Broadcaster", "admin": " als Administrator"}}
        mailer.send_template(email, "invite", {"inviter": admin["email"], "url": url, "expiry": "",
                                               "role_note": note.get(lang, note["en"]).get(role, "")}, lang=lang, area_id=area)
        emailed = True
    return {"code": code, "url": url, "role": role, "emailed": emailed, "smtp_configured": mailer.can_send(area)}


@router.get("/invites")
async def api_invites(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    return db.list_invites()


@router.delete("/invites/{code}")
async def api_delete_invite(request: Request, code: str) -> dict[str, Any]:
    admin = require_admin(request)
    db.delete_invite(code)
    db.log_action(admin["id"], admin["email"], "invite_revoke", code[:8] + "…")
    return {"status": "deleted", "code": code}


@router.delete("/users/{user_id}")
async def api_delete_user(request: Request, user_id: int) -> dict[str, Any]:
    admin = require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if user_id == 1:
        raise HTTPException(status_code=403, detail="The bootstrap admin cannot be deleted")
    target_user = db.get_user(user_id)
    guard_admin_target(admin, target_user, "delete another administrator")
    target = (target_user or {}).get("email", str(user_id))
    area = db.user_primary_area(user_id)
    db.delete_user(user_id)
    if area:
        config.invalidate(area)                          # the deleted workspace leaves the settings cache
    db.log_action(admin["id"], admin["email"], "user_delete", target)
    state.log_event("info", f"User {user_id} deleted by {admin['email']}")
    return {"status": "deleted", "id": user_id}


@router.get("/audit")
async def api_audit(request: Request, kind: str = "actions") -> list[dict[str, Any]]:
    """``kind=actions`` (default) → admin actions, ``logins`` → sign-in events, ``all``."""
    require_admin(request)
    logins = {"actions": False, "logins": True}.get(kind)
    return db.list_audit(100, logins=logins)


@router.post("/account/password")
async def api_change_password(request: Request) -> JSONResponse:
    """Self-service password change: verify the current password, then set a
    new one. Every other session of the user is signed out (the cookie carries
    the password version); this session gets a fresh cookie so the user is not."""
    user = request.state.user
    body = await request.json()
    current = str(body.get("current", ""))
    new = str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    if not await db.authenticate_async(user["email"], current):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    await db.set_password_async(user["id"], new)
    db.log_action(user["id"], user["email"], "password_change", user["email"])
    state.log_event("info", f"Password changed for {user['email']}")
    resp = JSONResponse({"status": "ok"})
    set_session_cookie(resp, request, user["id"])
    return resp


@router.post("/account/sessions/revoke")
async def api_revoke_own_sessions(request: Request) -> JSONResponse:
    """Sign out every other device of the caller: the fingerprint in the session
    cookies is rotated (cookies are stateless), this session gets a fresh one."""
    user = request.state.user
    db.revoke_sessions(user["id"])
    db.log_action(user["id"], user["email"], "sessions_revoke", user["email"], "self")
    resp = JSONResponse({"status": "ok"})
    set_session_cookie(resp, request, user["id"])
    return resp


@router.post("/users/{user_id}/sessions/revoke")
async def api_revoke_user_sessions(request: Request, user_id: int) -> dict[str, Any]:
    """Admin: sign a user out everywhere (lost phone, leaked cookie)."""
    admin = require_admin(request)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    guard_admin_target(admin, target, "sign out another administrator")
    db.revoke_sessions(user_id)
    db.log_action(admin["id"], admin["email"], "sessions_revoke", target["email"])
    state.log_event("info", f"All sessions of {target['email']} were signed out by {admin['email']}")
    return {"status": "ok", "user_id": user_id}


@router.post("/users/{user_id}/reset")
async def api_create_reset(request: Request, user_id: int) -> dict[str, Any]:
    """Admin generates a one-time password-reset link for a user."""
    admin = require_admin(request)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    guard_admin_target(admin, target, "reset another administrator")
    token = db.create_password_reset(user_id)
    db.log_action(admin["id"], admin["email"], "password_reset", target["email"])
    url = f"{base_url(request)}/reset?token={token}"
    area = context.get_area()
    emailed = mailer.can_send(area)
    if emailed:
        lang = mailer.lang_for_user(user_id)
        who = {"en": "An administrator", "de": "Ein Administrator"}.get(lang, "An administrator")
        mailer.send_template(target["email"], "password_reset", {"who": who, "url": url}, lang=lang, area_id=area)
    # The link is a login: hand it to the admin only when it could not be mailed
    # to the user (no mail route) and they have to pass it on out of band.
    return {"user_id": user_id, "url": "" if emailed else url, "emailed": emailed, "smtp_configured": emailed}

"""Professional operations (alpha.99): platform config, canary, escalations,
settings history, support grant + note, Telegram linking, broadcast, rollback."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import canary, config, context, crypto, db, escalation, platform, state, telegram, updater, web
from ..web import require_admin, require_role

router = APIRouter(tags=["ops"])


# ------------------------------------------------------------------ platform config
@router.get("/api/platform/config")
async def api_platform_config(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {**platform.public_config(), "canary": canary.status()}


@router.put("/api/platform/config")
async def api_platform_config_save(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    try:
        platform.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.log_action(admin["id"], admin["email"], "platform_config", "platform", ", ".join(sorted(k for k in body if k in platform.DEFAULTS)))
    return {**platform.public_config(), "canary": canary.status()}


@router.post("/api/platform/canary/run")
async def api_canary_run(request: Request) -> dict[str, Any]:
    require_admin(request)
    res = await canary.run_once()
    return {"result": res, "status": canary.status()}


# ------------------------------------------------------------------ broadcast
@router.post("/api/broadcast")
async def api_broadcast(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    try:
        res = await platform.broadcast(admin, str(body.get("title") or ""), str(body.get("body") or ""), roles=body.get("roles") or [],
                                       mail=bool(body.get("mail", True)), banner_hours=int(body.get("banner_hours") or 0))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    state.log_event("info", f"Broadcast '{body.get('title')}' sent to {res['recipients']} user(s) by {admin['email']}")
    return res


@router.delete("/api/broadcast/banner")
async def api_banner_clear(request: Request) -> dict[str, Any]:
    require_admin(request)
    platform.clear_banner()
    return {"cleared": True}


# ------------------------------------------------------------------ escalations
@router.get("/api/escalations")
async def api_escalations(request: Request) -> dict[str, Any]:
    return escalation.summary(context.get_area())


@router.post("/api/escalations/{esc_id}/ack")
async def api_escalation_ack(request: Request, esc_id: int) -> dict[str, Any]:
    user = require_role(request, "user")
    esc = escalation.acknowledge(esc_id, user["email"], context.get_area())
    if not esc:
        raise HTTPException(status_code=404, detail="No open escalation with that id")
    return esc


@router.get("/ack", response_class=HTMLResponse)
async def ack_page(request: Request, t: str = "") -> HTMLResponse:
    """The acknowledge link in a push / mail / Telegram message (no login)."""
    esc_id = escalation.parse_token(t)
    esc = escalation.acknowledge(esc_id, "link") if esc_id else None
    already = bool(esc_id) and esc is None and db.get_escalation(esc_id) is not None
    return web.render(request, "ack.html", {"ok": esc is not None, "already": already, "title": (esc or db.get_escalation(esc_id or 0) or {}).get("title", "")})


# ------------------------------------------------------------------ settings history
@router.get("/api/settings/history")
async def api_settings_history(request: Request) -> dict[str, Any]:
    return {"versions": db.list_settings_versions(context.get_area())}


@router.post("/api/settings/history/{version_id}/restore")
async def api_settings_restore(request: Request, version_id: int) -> dict[str, Any]:
    user = require_role(request, "user")
    area = context.get_area()
    v = db.get_settings_version(area, version_id)
    if not v:
        raise HTTPException(status_code=404, detail="No such version")
    try:
        snap = crypto.decrypt_settings(json.loads(v["snapshot"]))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"version unreadable: {exc}")
    config.save_settings(snap, area_id=area)
    db.log_action(user["id"], user["email"], "settings_restore", f"version {version_id}", v["ts"])
    state.log_event("warn", f"Settings restored to the version of {v['ts'][:16].replace('T', ' ')} by {user['email']}")
    return {"restored": version_id, "ts": v["ts"], "keys": v["keys"]}


# ------------------------------------------------------------------ support grant + note
@router.post("/api/me/support-grant")
async def api_support_grant(request: Request) -> dict[str, Any]:
    user = require_role(request, "user")
    body = await request.json()
    try:
        hours = float(body.get("hours", 24))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="hours must be a number")
    g = web.set_support_grant(context.get_area(), hours, user["email"])
    db.log_action(user["id"], user["email"], "support_grant", user["email"], f"{hours:g} h" if g else "revoked")
    state.log_event("info", f"Support write access {'granted for %g h' % hours if g else 'revoked'} by {user['email']}")
    return {"grant": g}


@router.post("/api/support/note")
async def api_support_note(request: Request) -> dict[str, Any]:
    """Admin in the support view leaves a note the user sees in their inbox."""
    admin = require_admin(request)
    support = getattr(request.state, "support", None)
    if not support:
        raise HTTPException(status_code=400, detail="Only inside a support view")
    body = await request.json()
    text = str(body.get("message") or "").strip()[:1000]
    if not text:
        raise HTTPException(status_code=400, detail="message is required")
    from .. import alerts
    with context.use_area(support["area_id"]):
        alerts._inbox("support.note", "info", f"Note from support ({admin['email']})", text, "/#/settings/account")
    db.log_action(admin["id"], admin["email"], "support_note", support.get("email", ""), text[:120])
    return {"left": True}


# ------------------------------------------------------------------ telegram linking
@router.get("/api/telegram")
async def api_telegram_status(request: Request) -> dict[str, Any]:
    return telegram.status(context.get_area())


@router.post("/api/telegram/link-code")
async def api_telegram_code(request: Request) -> dict[str, Any]:
    require_role(request, "user")
    if not telegram.configured():
        raise HTTPException(status_code=400, detail="Telegram is not set up on this bridge (Settings → Platform)")
    return {"code": telegram.link_code(context.get_area()), "bot_name": platform.get_config()["telegram_bot_name"], "expires_in": telegram.LINK_TTL_S}


@router.delete("/api/telegram/link")
async def api_telegram_unlink(request: Request) -> dict[str, Any]:
    require_role(request, "user")
    telegram.unlink(context.get_area())
    return {"linked": False}


@router.post("/api/telegram/test")
async def api_telegram_test(request: Request) -> dict[str, Any]:
    require_role(request, "user")
    try:
        ok = await telegram.send_area(context.get_area(), "🔔 Fluxbridge: Telegram is linked to this workspace.")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=400, detail="No Telegram chat linked yet")
    return {"sent": True}


# ------------------------------------------------------------------ rollback
@router.get("/api/update/rollback")
async def api_rollback_point(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"point": updater.rollback_point()}


@router.post("/api/update/rollback")
async def api_rollback(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    restore_db = bool((body or {}).get("restore_db"))
    db.log_action(admin["id"], admin["email"], "update_rollback", "restore_db" if restore_db else "code only")
    return await updater.rollback(restore_db=restore_db)


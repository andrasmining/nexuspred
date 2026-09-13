"""Notification inbox, workspace readiness / onboarding, what's new, mail preferences (alpha.97)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import context, db, readiness, releases, web
from ..web import require_role

router = APIRouter(tags=["notify"])


@router.get("/api/notifications")
async def api_notifications(request: Request, unread: int = 0, limit: int = 50, before: int = 0) -> dict[str, Any]:
    area = context.get_area()
    return {"unread": db.unread_count(area), "rows": db.list_notifications(area, unread_only=bool(unread), limit=limit, before_id=before or None)}


@router.get("/api/notifications/count")
async def api_notifications_count() -> dict[str, Any]:
    return {"unread": db.unread_count(context.get_area())}


@router.post("/api/notifications/read")
async def api_notifications_read(request: Request) -> dict[str, Any]:
    require_role(request, "user")
    body = await request.json()
    ids = body.get("ids") if isinstance(body, dict) else None
    if ids is not None and not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    n = db.mark_read(context.get_area(), [int(i) for i in ids] if ids else None)
    return {"marked": n, "unread": db.unread_count(context.get_area())}


@router.get("/api/workspace/readiness")
async def api_workspace_readiness(request: Request) -> dict[str, Any]:
    user = request.state.user
    area = context.get_area()
    checks = readiness.workspace_checks(area, user)
    return {"checks": checks, "onboarding": readiness.onboarding(area, user, checks)}


@router.post("/api/workspace/onboarding/dismiss")
async def api_onboarding_dismiss(request: Request) -> dict[str, Any]:
    user = require_role(request, "user")
    db.meta_set(f"onboarding_dismissed:{user['id']}", "1")
    return {"dismissed": True}


@router.post("/api/workspace/onboarding/status-seen")
async def api_onboarding_status_seen(request: Request) -> dict[str, Any]:
    user = require_role(request, "user")
    db.meta_set(f"onboarding_status_seen:{user['id']}", "1")
    return {"ok": True}


@router.get("/api/whatsnew")
async def api_whatsnew(request: Request) -> dict[str, Any]:
    return releases.whats_new(request.state.user)


@router.post("/api/whatsnew/seen")
async def api_whatsnew_seen(request: Request) -> dict[str, Any]:
    user = require_role(request, "user")
    releases.mark_seen(user["id"])
    return {"seen": True}


@router.get("/api/me/mail-prefs")
async def api_mail_prefs(request: Request) -> dict[str, Any]:
    return releases.prefs(request.state.user["id"])


@router.put("/api/me/mail-prefs")
async def api_mail_prefs_save(request: Request) -> dict[str, Any]:
    user = require_role(request, "user")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    return releases.save_prefs(user["id"], body)


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(request: Request, t: str = "") -> HTMLResponse:
    """One-click opt-out from a signed link in a non-transactional mail (no login)."""
    res = releases.apply_unsubscribe(t)
    return web.render(request, "unsubscribe.html", {"ok": res is not None, "pref": res[1] if res else ""})

"""Platform mailer (admin): config, test mail, outbox log; alert delivery log (workspace)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import context, db, mailer, state
from ..web import base_url, require_admin

router = APIRouter(tags=["mail"])


@router.get("/api/mail/config")
async def api_mail_config(request: Request) -> dict[str, Any]:
    require_admin(request)
    return mailer.public_config()


@router.put("/api/mail/config")
async def api_mail_save(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    try:
        mailer.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.log_action(admin["id"], admin["email"], "mail_config", "platform mailer", mailer.get_config()["provider"])
    state.log_event("info", f"Platform mailer set to {mailer.get_config()['provider']} by {admin['email']}")
    return mailer.public_config()


@router.post("/api/mail/test")
async def api_mail_test(request: Request) -> dict[str, Any]:
    """Queue a test mail to the admin's own address and deliver it right away."""
    admin = require_admin(request)
    area = context.get_area()
    if not mailer.can_send(area):
        raise HTTPException(status_code=400, detail="No mail route: set up the platform mailer or your workspace SMTP first")
    route = mailer.get_config()["provider"] if mailer.configured() else "workspace SMTP"
    row_id = mailer.send_template(admin["email"], "test", {"route": route, "url": base_url(request) + "/"}, area_id=area, kick=False)
    row = await mailer.send_now(row_id)
    return {"id": row_id, "status": row.get("status"), "route": row.get("route"), "error": row.get("last_error", "")}


@router.get("/api/mail/log")
async def api_mail_log(request: Request, limit: int = 100, status: str = "") -> dict[str, Any]:
    require_admin(request)
    limit = max(1, min(int(limit), 500))
    return {"rows": db.outbox_list(limit, status or None), "counts": db.outbox_counts()}


@router.post("/api/mail/retry/{row_id}")
async def api_mail_retry(request: Request, row_id: int) -> dict[str, Any]:
    require_admin(request)
    if not db.outbox_retry(row_id):
        raise HTTPException(status_code=404, detail="No failed mail with that id")
    row = await mailer.send_now(row_id)
    return {"id": row_id, "status": row.get("status"), "error": row.get("last_error", "")}


@router.get("/api/alerts/deliveries")
async def api_alert_deliveries(request: Request, limit: int = 50) -> dict[str, Any]:
    """The workspace's alert channels: last delivery per channel and the recent attempts."""
    area = context.get_area()
    return {"channels": db.delivery_status(area), "recent": db.recent_deliveries(area, max(1, min(int(limit), 200)))}

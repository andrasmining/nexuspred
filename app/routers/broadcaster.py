"""Broadcaster business (alpha.98): announcements to subscribers, the cockpit."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import broadcaster, context
from ..web import require_role

router = APIRouter(tags=["broadcaster"])


@router.get("/api/broadcaster/cockpit")
async def api_cockpit(request: Request) -> dict[str, Any]:
    require_role(request, "broadcaster")
    return await asyncio.to_thread(broadcaster.cockpit, context.get_area())


@router.get("/api/announcements")
async def api_announcements(request: Request) -> dict[str, Any]:
    require_role(request, "broadcaster")
    area = context.get_area()
    from .. import db
    return {"rows": db.list_announcements(area, 50), "left_today": max(0, broadcaster.ANNOUNCE_PER_DAY - db.announcements_today(area)),
            "listings": [{"key": k, "title": t} for k, t in broadcaster.listing_keys(area)]}


@router.post("/api/announcements")
async def api_announce(request: Request) -> dict[str, Any]:
    user = require_role(request, "broadcaster")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    try:
        return await broadcaster.announce(context.get_area(), user, str(body.get("title") or ""), str(body.get("body") or ""), str(body.get("listing_key") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

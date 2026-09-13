"""GitHub-backed self-updater endpoints."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import backups, crypto, db, updater
from ..security import client_ip
from ..web import require_admin

router = APIRouter(prefix="/api/update", tags=["updater"])


@router.get("/check")
async def api_update_check(request: Request) -> dict[str, Any]:
    require_admin(request)                      # an outbound GitHub call is an admin's to trigger
    return await updater.check_for_update()


@router.get("/backup")
async def api_download_backup(request: Request) -> FileResponse:
    """Admin: download the whole database (every tenant) as one SQLite file —
    the way to move an installation, e.g. from Render to your own server
    (``fluxbridge restore FILE``). Consistent even while the bridge is trading."""
    admin = require_admin(request)
    db.init()
    if crypto.key_source() == "db":
        raise HTTPException(status_code=409, detail=(
            "The encryption key still lives inside the database, so a backup would carry it. "
            "Set SESSION_SECRET (or NEXUSPRED_ENCRYPTION_KEY) in the environment and restart — "
            "the stored secrets are re-encrypted under it at startup — then download the backup."))
    db.log_action(admin["id"], admin["email"], "backup_download", client_ip(request))
    fd, path = tempfile.mkstemp(prefix="fluxbridge-backup-", suffix=".db")
    os.close(fd)
    try:
        await asyncio.to_thread(backups.write_snapshot, path)
    except Exception as exc:  # noqa: BLE001
        os.unlink(path)
        raise HTTPException(status_code=500, detail=f"backup failed: {exc}") from exc
    name = f"fluxbridge-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name,
                        background=BackgroundTask(os.unlink, path))


@router.post("/apply")
async def api_update_apply(request: Request) -> dict[str, Any]:
    """Pull + restart the whole process (every tenant): admins only."""
    require_admin(request)
    result = await updater.apply_update()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result

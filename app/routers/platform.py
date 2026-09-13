"""Platform operations (alpha.96): backups, deep health, status page, heartbeat, incidents."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import backups, db, metrics, readiness, state, web
from ..security import client_ip
from ..web import require_admin

router = APIRouter(tags=["platform"])


# ------------------------------------------------------------------ backups
@router.get("/api/backups")
async def api_backups(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"config": backups.public_config(), "status": backups.status(), "backups": backups.list_backups()}


@router.put("/api/backups/config")
async def api_backups_config(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object")
    try:
        backups.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.log_action(admin["id"], admin["email"], "backup_config", "backups", f"offsite={backups.get_config()['offsite']}")
    return {"config": backups.public_config(), "status": backups.status()}


@router.post("/api/backups/run")
async def api_backups_run(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    db.log_action(admin["id"], admin["email"], "backup_run", client_ip(request))
    try:
        entry = await backups.run("manual")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"backup failed: {exc}")
    return {"backup": entry, "status": backups.status()}


@router.post("/api/backups/offsite/test")
async def api_backups_offsite_test(request: Request) -> dict[str, Any]:
    require_admin(request)
    try:
        return await backups.test_offsite()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}


@router.get("/api/backups/{name}")
async def api_backup_download(request: Request, name: str) -> FileResponse:
    admin = require_admin(request)
    path = backups.path_of(name)
    if not path:
        raise HTTPException(status_code=404, detail="No such backup")
    db.log_action(admin["id"], admin["email"], "backup_download", name)
    return FileResponse(str(path), media_type="application/vnd.sqlite3", filename=name)


@router.delete("/api/backups/{name}")
async def api_backup_delete(request: Request, name: str) -> dict[str, Any]:
    admin = require_admin(request)
    if not backups.path_of(name):
        raise HTTPException(status_code=404, detail="No such backup")
    backups.delete(name)
    db.log_action(admin["id"], admin["email"], "backup_delete", name)
    return {"deleted": name, "status": backups.status()}


# ------------------------------------------------------------------ health
def _token_ok(request: Request) -> bool:
    if metrics.authorized(request.headers.get("authorization")):
        return True
    tok = request.query_params.get("token")
    return bool(tok) and metrics.authorized(f"Bearer {tok}")


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Deep health for uptime monitors (``Authorization: Bearer <NEXUSPRED_METRICS_TOKEN>``
    or ``?token=``). 200 for ok / degraded, 503 for down. Admins read the same
    result signed in at ``/api/platform/health``."""
    if not _token_ok(request):
        if not metrics.enabled():
            raise HTTPException(status_code=401, detail="Set NEXUSPRED_METRICS_TOKEN and send it as a bearer token (or ?token=)")
        raise HTTPException(status_code=401, detail="unauthorized")
    res = await readiness.check()
    return JSONResponse(res, status_code=503 if res["status"] == "down" else 200, headers={"Cache-Control": "no-store"})


@router.get("/api/platform/health")
async def api_platform_health(request: Request) -> dict[str, Any]:
    require_admin(request)
    return await readiness.check()


@router.get("/api/platform/heartbeat")
async def api_heartbeat(request: Request) -> dict[str, Any]:
    require_admin(request)
    return readiness.heartbeat_status()


@router.put("/api/platform/heartbeat")
async def api_heartbeat_save(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    try:
        cfg = await asyncio.to_thread(readiness.save_heartbeat, str(body.get("url") or ""), int(body.get("interval") or 60))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.log_action(admin["id"], admin["email"], "heartbeat_config", cfg["url"] or "off")
    ok = await readiness.heartbeat_once() if cfg["url"] else False
    return {**readiness.heartbeat_status(), "pinged": ok}


# ------------------------------------------------------------------ incidents
@router.get("/api/incidents")
async def api_incidents(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    return readiness.incidents(include_resolved_days=3650)


@router.post("/api/incidents")
async def api_incident_create(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    try:
        inc = readiness.save_incident(body, admin["email"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.log_action(admin["id"], admin["email"], "incident", inc["title"], inc["status"])
    state.log_event("warn", f"Incident opened on the status page: {inc['title']} ({inc['status']})")
    return inc


@router.put("/api/incidents/{incident_id}")
async def api_incident_update(request: Request, incident_id: str) -> dict[str, Any]:
    admin = require_admin(request)
    body = await request.json()
    try:
        inc = readiness.save_incident(body, admin["email"], incident_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such incident")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.log_action(admin["id"], admin["email"], "incident", inc["title"], inc["status"])
    return inc


@router.delete("/api/incidents/{incident_id}")
async def api_incident_delete(request: Request, incident_id: str) -> dict[str, Any]:
    require_admin(request)
    if not readiness.delete_incident(incident_id):
        raise HTTPException(status_code=404, detail="No such incident")
    return {"deleted": incident_id}


# ------------------------------------------------------------------ public status
@router.get("/api/public/status")
async def api_public_status() -> JSONResponse:
    return JSONResponse(readiness.public_summary(), headers={"Cache-Control": "no-store"})


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    """Public status page: no login, no tenant data — bridge up, version,
    broker connectivity, signal latency, admin-posted incidents."""
    return web.render(request, "status.html", {"summary": readiness.public_summary()})

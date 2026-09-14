"""Dashboard execution transport, exposure, automations and metrics."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from .. import automations, config, context, db, exposure, metrics, security, state
from ..execution import service
from ..execution.contracts import AccountTarget, ClosePosition, ExecutionError, ManualOrder
from ..commercial.workspaces import WorkspaceAccessDenied, current_actor
from .execution_errors import http_error

router = APIRouter()
_TICKET_LIMIT = security.RateLimiter(30, 60)


def _user(request: Request) -> dict[str, Any]:
    return getattr(request.state, "user", None) or {}


@router.post("/api/orders/manual")
async def api_manual_order(request: Request, response: Response) -> dict[str, Any]:
    user = _user(request)
    if not _TICKET_LIMIT.hit(f"ticket:{user.get('id', 0)}"):
        raise HTTPException(status_code=429, detail="Too many manual orders — wait a minute")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        command = ManualOrder.from_payload(current_actor(user.get("id"), getattr(request.state, "support", None)), body, request.headers.get("Idempotency-Key"))
        result = await service.execution.place_manual_order(command)
    except WorkspaceAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ExecutionError as exc:
        raise http_error(exc) from exc
    # Existing clients keep the exact response body. Clients opting into safe
    # HTTP retries retain the same Idempotency-Key for one intended operation.
    response.headers["X-Execution-Command-Id"] = result.command_id
    response.headers["X-Execution-Outcome"] = result.outcome.value
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result.payload


@router.post("/api/positions/close")
async def api_close_position(request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        command = ClosePosition(current_actor(_user(request).get("id"), getattr(request.state, "support", None)), AccountTarget.from_payload(body),
                                str(body.get("symbol") or "").strip())
        return await service.execution.close_position(command)
    except WorkspaceAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ExecutionError as exc:
        raise http_error(exc) from exc


@router.get("/api/execution/commands/{command_id}")
async def api_execution_status(request: Request, command_id: str) -> dict[str, Any]:
    try:
        return await service.execution.status(current_actor(_user(request).get("id"), getattr(request.state, "support", None)), command_id)
    except WorkspaceAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ExecutionError as exc:
        raise http_error(exc) from exc


@router.get("/api/exposure")
async def api_exposure() -> dict[str, Any]:
    rows = await exposure.collect_positions()
    return {"positions": rows, **exposure.summarize(rows)}


# ------------------------------------------------------------ automations
def _automations_view(area_id: int) -> dict[str, Any]:
    return {"rules": automations.rules_for(area_id), "log": automations.recent(area_id),
            "events": [{"id": k, "filters": sorted(v)} for k, v in automations.KINDS.items()], "actions": list(automations.ACTIONS)}


@router.get("/api/automations")
async def api_automations() -> dict[str, Any]:
    return _automations_view(context.get_area())


@router.put("/api/automations")
async def api_save_automations(request: Request) -> dict[str, Any]:
    user = _user(request)
    body = await request.json()
    raw = body.get("rules") if isinstance(body, dict) else body
    try:
        rules = automations.normalize_rules(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save_settings({"automations": rules})
    state.log_event("info", f"Automations saved ({len(rules)} rule(s))")
    if user:
        db.log_action(user["id"], user["email"], "automations_saved", "", f"{len(rules)} rule(s)")
    return _automations_view(context.get_area())


# ---------------------------------------------------------------- metrics
@router.get("/metrics")
async def api_metrics(request: Request) -> PlainTextResponse:
    if not metrics.enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if not metrics.authorized(request.headers.get("authorization")):
        return PlainTextResponse("unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8",
                             headers={"Cache-Control": "no-store"})

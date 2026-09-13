"""Paid subscriptions: operator config (admin), Checkout / portal links for
subscribers, the Stripe webhook."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import config, context, copy, db, marketplace, payments, state
from ..web import require_admin

router = APIRouter(prefix="/api/payments", tags=["payments"])


OPERATOR_USER_ID = 1          # the first admin owns the bridge (and the Stripe account)


def _base_url(request: Request) -> str:
    """Where Stripe sends the subscriber back. NEXUSPRED_PUBLIC_URL when set;
    else the host the request was bound to — never a forwarded header a
    caller could spoof into the Checkout session."""
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    proto = "https" if request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https" else request.url.scheme
    return f"{proto}://{request.url.netloc}"


def require_operator(request: Request) -> dict[str, Any]:
    """The bridge operator: the first admin. Publishers are admins too, so the
    Stripe keys and the full payment ledger need the stricter check."""
    user = require_admin(request)
    if int(user.get("id") or 0) != OPERATOR_USER_ID:
        raise HTTPException(status_code=403, detail="Operator only")
    return user


@router.get("/config")
async def api_config(request: Request) -> dict[str, Any]:
    require_operator(request)
    return payments.public_config()


@router.put("/config")
async def api_save_config(request: Request) -> dict[str, Any]:
    user = require_operator(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        was = payments.configured()
        payments.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payments.configured() and not was:
        # switched on: paid listings now require a paid record, whoever subscribed while it was free
        for pa in db.all_area_ids():
            for w in (config.load_settings(area_id=pa).get("webhooks") or []):
                sh = marketplace.sharing_of(w)
                if sh["enabled"] and sh["price_cents"]:
                    payments.demote_unpaid(pa, str(w.get("id") or ""))
            for g in copy.load_groups(pa):
                sh = marketplace.sharing_of(g)
                if sh["enabled"] and sh["price_cents"]:
                    payments.demote_unpaid(pa, f"copy:{g['id']}")
    db.log_action(user["id"], user["email"], "payments_config", "", "enabled" if payments.get_config()["enabled"] else "disabled")
    state.log_event("info", f"Payments {'enabled' if payments.get_config()['enabled'] else 'disabled'} by {user['email']}")
    return payments.public_config()


@router.get("")
async def api_list(request: Request) -> list[dict[str, Any]]:
    """Operator: every payment record; a publisher: the payments for their own listings."""
    user = getattr(request.state, "user", None) or {}
    if int(user.get("id") or 0) == OPERATOR_USER_ID and user.get("is_admin"):
        return db.list_payments()
    return db.list_payments(publisher_area_id=context.get_area())


@router.get("/mine")
async def api_mine() -> list[dict[str, Any]]:
    return [{k: v for k, v in p.items() if k != "email"} for p in db.list_payments(context.get_area())]


@router.post("/checkout")
async def api_checkout(request: Request) -> dict[str, Any]:
    """Start Checkout for one paid listing (the subscription must exist)."""
    user = request.state.user
    area = context.get_area()
    body = await request.json()
    try:
        pa = int(body.get("publisher_area_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="publisher_area_id is required")
    key = str(body.get("key") or "")
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    if not payments.configured():
        raise HTTPException(status_code=409, detail="Payments are not enabled on this bridge")
    if key.startswith("copy:"):
        g, sh = copy.find_published(pa, key[5:])
        title = (sh.get("title") or (g or {}).get("name") or key) if g else ""
    else:
        wh, sh = marketplace.find_published(pa, key)
        title = (sh.get("title") or (wh or {}).get("name") or key) if wh else ""
    if not title or not marketplace.visible_to(sh, user["id"]):
        raise HTTPException(status_code=404, detail="That listing isn't published")
    price = int(sh.get("price_cents") or 0)
    if not price:
        raise HTTPException(status_code=400, detail="That listing is free")
    if payments.has_paid(area, pa, key):
        raise HTTPException(status_code=409, detail="Already paid")
    trial = int(sh.get("trial_days") or 0) or int(payments.get_config()["trial_days_default"] or 0)
    try:
        url = await payments.create_checkout(area_id=area, publisher_area_id=pa, key=key, title=title, price_cents=price, trial_days=trial,
                                             email=str(user.get("email") or ""), base_url=_base_url(request))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.log_action(user["id"], user["email"], "checkout_started", title, f"{price / 100:.2f} {payments.get_config()['currency']}/month")
    return {"url": url}


@router.post("/portal")
async def api_portal(request: Request) -> dict[str, Any]:
    if not payments.configured():
        raise HTTPException(status_code=409, detail="Payments are not enabled on this bridge")
    try:
        return {"url": await payments.create_portal(context.get_area(), _base_url(request))}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/webhook")
async def api_webhook(request: Request) -> PlainTextResponse:
    """Stripe → bridge. Unauthenticated path; the signature is the credential."""
    cfg = payments.get_config()
    raw = await request.body()
    if not payments.verify_signature(raw, request.headers.get("stripe-signature", ""), cfg["stripe_webhook_secret"]):
        return PlainTextResponse("bad signature\n", status_code=400)
    try:
        event = json.loads(raw)
    except ValueError:
        return PlainTextResponse("bad json\n", status_code=400)
    if not isinstance(event, dict):
        return PlainTextResponse("bad event\n", status_code=400)
    try:
        done = await payments.handle_event(event)
    except Exception:  # noqa: BLE001 - Stripe retries on 5xx; the cause stays in the log
        payments.log.exception("stripe event %s failed", event.get("type"))
        return PlainTextResponse("error\n", status_code=500)
    payments.log.info("stripe %s: %s", event.get("type"), done)
    return PlainTextResponse("ok\n")

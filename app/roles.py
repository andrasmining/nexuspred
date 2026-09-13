"""Role changes and their consequences.

A user who loses the Broadcaster role must not keep selling: every listing of
their workspace is unpublished, its subscribers' Stripe subscriptions are
cancelled and their rows removed, every published copy group is taken off the
marketplace and its marketplace followers released. If Stripe cannot confirm a
cancellation, the subscriber row is retained for a later retry and the role
change is refused. Nothing is deleted — the webhooks and groups stay and keep
running for the workspace's own accounts.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from . import config, context, db, payments, state
from . import copy as cp

log = logging.getLogger(__name__)


async def demote_publisher(area_id: int) -> dict[str, int]:
    """Undo everything only a Broadcaster may run in ``area_id``. Returns counts.

    Publication is removed before Stripe cleanup so no further subscriber
    execution can occur. A live Stripe subscription that cannot be cancelled is
    never orphaned by deleting its local subscription row: the row is retained
    and the caller is told to retry the demotion while the Broadcaster role is
    still intact.
    """
    out = {"listings": 0, "subscriptions": 0, "groups": 0}
    with context.use_area(area_id):
        # Include stale rows from an earlier interrupted/failed demotion as well
        # as currently published listings. This makes a retry able to finish the
        # billing cleanup even though the first attempt already unpublished it.
        keys = {str(k) for k in db.subscriber_counts(area_id) if k}
        keys.update(str(p.get("webhook_id") or "") for p in db.list_payments(publisher_area_id=area_id)
                    if p.get("webhook_id"))

        s = config.load_settings(area_id=area_id)
        webhooks = list(s.get("webhooks") or [])
        changed = False
        for wh in webhooks:
            sh = wh.get("sharing") or {}
            if sh.get("enabled"):
                key = str(wh.get("id") or "")
                if key:
                    keys.add(key)
                wh["sharing"] = {**sh, "enabled": False}
                changed = True
                out["listings"] += 1
        if changed:
            config.save_settings({"webhooks": webhooks}, area_id=area_id)

        groups = cp.load_groups(area_id)
        gchanged = False
        for g in groups:
            sh = g.get("sharing") or {}
            if sh.get("enabled"):
                key = f"copy:{g['id']}"
                keys.add(key)
                g["sharing"] = {**sh, "enabled": False}           # the group keeps running for its own followers
                gchanged = True
                out["groups"] += 1
        if gchanged:
            cp.save_groups(groups, area_id=area_id)
            try:
                await cp.sync_area(area_id)                 # marketplace followers are released from the runners
            except Exception as exc:  # noqa: BLE001
                log.warning("copy sync after demotion failed: %s", exc)

        # Cancel billing only after publication is off. cancel_for_listing marks
        # each broker-confirmed cancellation in the payment row. Any row that is
        # still LIVE afterwards is an unresolved financial side effect and must
        # keep its local subscription identity for a deterministic retry.
        for key in sorted(k for k in keys if k):
            try:
                await payments.cancel_for_listing(area_id, key)
            except Exception as exc:  # noqa: BLE001
                log.warning("stripe cancel for listing %s failed: %s", key, exc)

        remaining = [p for p in db.list_payments(publisher_area_id=area_id)
                     if str(p.get("webhook_id") or "") in keys
                     and p.get("stripe_subscription") and p.get("status") in db.payments.LIVE]
        live_keys = {str(p.get("webhook_id") or "") for p in remaining}
        for key in sorted(k for k in keys if k and k not in live_keys):
            out["subscriptions"] += db.delete_subscriptions_for_webhook(area_id, key)

        if remaining:
            n = len(remaining)
            state.log_event("error", f"Broadcaster role withdrawal paused: {n} live Stripe subscription(s) could not be cancelled. "
                                     "Listings are unpublished and subscriber rows are retained for retry; the role was not changed.")
            raise HTTPException(status_code=502, detail="Stripe could not cancel all paid subscriptions — listings were unpublished, "
                                                            "subscriber records were retained for retry, and the Broadcaster role was not changed")

        if out["listings"] or out["groups"] or out["subscriptions"]:
            state.log_event("warn", f"Broadcaster role withdrawn: {out['listings']} listing(s) unpublished, {out['groups']} copy group(s) taken off the marketplace, "
                                    f"{out['subscriptions']} subscription(s) ended")
    return out

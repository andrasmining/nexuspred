"""Role changes and their consequences.

A user who loses the Broadcaster role must not keep selling: every listing of
their workspace is unpublished, its subscribers' Stripe subscriptions are
cancelled and their rows removed, every published copy group is taken off the
marketplace and its marketplace followers released. Nothing is deleted — the
webhooks and groups stay and keep running for the workspace's own accounts.
"""
from __future__ import annotations

import logging

from . import config, context, db, payments, state
from . import copy as cp

log = logging.getLogger(__name__)


async def demote_publisher(area_id: int) -> dict[str, int]:
    """Undo everything only a Broadcaster may run in ``area_id``. Returns counts."""
    out = {"listings": 0, "subscriptions": 0, "groups": 0}
    with context.use_area(area_id):
        s = config.load_settings(area_id=area_id)
        webhooks = list(s.get("webhooks") or [])
        changed = False
        for wh in webhooks:
            sh = wh.get("sharing") or {}
            if sh.get("enabled"):
                wh["sharing"] = {**sh, "enabled": False}
                changed = True
                out["listings"] += 1
                try:
                    await payments.cancel_for_listing(area_id, str(wh.get("id") or ""))
                except Exception as exc:  # noqa: BLE001
                    log.warning("stripe cancel for listing %s failed: %s", wh.get("id"), exc)
                out["subscriptions"] += db.delete_subscriptions_for_webhook(area_id, str(wh.get("id") or ""))
        if changed:
            config.save_settings({"webhooks": webhooks}, area_id=area_id)
        groups = cp.load_groups(area_id)
        gchanged = False
        for g in groups:
            sh = g.get("sharing") or {}
            if sh.get("enabled"):
                g["sharing"] = {**sh, "enabled": False}           # the group keeps running for its own followers
                gchanged = True
                out["groups"] += 1
                key = f"copy:{g['id']}"
                try:
                    await payments.cancel_for_listing(area_id, key)
                except Exception as exc:  # noqa: BLE001
                    log.warning("stripe cancel for group %s failed: %s", g.get("id"), exc)
                out["subscriptions"] += db.delete_subscriptions_for_webhook(area_id, key)
        if gchanged:
            cp.save_groups(groups, area_id=area_id)
            try:
                await cp.sync_area(area_id)                 # marketplace followers are released from the runners
            except Exception as exc:  # noqa: BLE001
                log.warning("copy sync after demotion failed: %s", exc)
        if out["listings"] or out["groups"]:
            state.log_event("warn", f"Broadcaster role withdrawn: {out['listings']} listing(s) unpublished, {out['groups']} copy group(s) taken off the marketplace, "
                                    f"{out['subscriptions']} subscription(s) ended")
    return out

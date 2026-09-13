"""Strategy-agnostic position management: ``close_all`` and ``set_sl_tp``."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import config, state
from ..tradovate import OrderOutcomeUnknown, TradovateError
from .common import OrdersLeftWorking, _close_contract, _close_untracked, _lock, _price, _trade_key, _untrack_after_close


async def handle_close_all(root, target, executors, active_map, tag, webhook):
    key = _trade_key(webhook["id"], root)
    with _lock:
        tracked = active_map.get(key)
        tracked_names = set((tracked or {}).get("accounts") or {})

    # For a partially failed retry, only target accounts that are still tracked.
    # Without tracking (for example after a restart), keep the existing safety-net
    # behaviour and flatten every currently enabled account for the symbol.
    targets = [ex for ex in executors if ex.name in tracked_names] if tracked_names else list(executors)

    async def close_account(ex) -> int:
        contract = await ex.resolve_contract(target)
        return await _close_contract(ex, tag, contract)

    results = await asyncio.gather(*(close_account(ex) for ex in targets), return_exceptions=True)
    cancelled = sum(r for r in results if isinstance(r, int))
    failed = [ex.name for ex, r in zip(targets, results) if isinstance(r, Exception)]
    succeeded = [ex.name for ex, r in zip(targets, results) if isinstance(r, int)]
    extra_closed, extra_failed = await _close_untracked(executors, {ex.name for ex in targets}, tag, target) if tracked_names else ([], [])
    for ex, r in zip(targets, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}close_all FAILED for {ex.name}: {r} — "
                                     + ("the position is closed but its orders are not: cancel them by hand" if isinstance(r, OrdersLeftWorking) else "the position may still be open"))

    _untrack_after_close(active_map, key, succeeded, failed)

    failures = failed + extra_failed
    state.log_event(
        "error" if failures else "info",
        f"{tag}[{webhook.get('name', '?')}] Closed all for {root} on "
        f"{len(targets) + len(extra_closed)} account(s) ({cancelled} working orders cancelled)"
        + (f"; untracked position closed on {', '.join(extra_closed)}" if extra_closed else "")
        + (f"; failed: {', '.join(failures)}" if failures else ""),
    )
    return {"status": "error" if failures else "ok", "action": "close_all",
            "accounts": len(targets) + len(extra_closed), "cancelled": cancelled,
            "failed": failures, "simulated": tag != ""}


async def _order_still_working(ex: Any, order_id: int) -> bool | None:
    """Broker-truth reconciliation after an uncertain cancel.

    True/False means the broker answered; None means the state is still unknown.
    No mutation is retried here.
    """
    try:
        rows = await ex.working_orders()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return None
    return any(int(o.get("id") or 0) == int(order_id) for o in rows or [])


async def handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook, *, settings=None):
    """Set/replace protection without claiming stronger state than the broker.

    Existing stops are modified in place rather than temporarily creating two
    full-size stops. Replacement targets are placed before old targets are
    retired; uncertain cancellations are reconciled against working-order state.
    Every order id that may still be live remains tracked, and per-account
    failures are returned explicitly instead of being reported as success/no-position.
    """
    s = settings if settings is not None else config.load_settings()
    raw_sl = payload.get("stop_price", payload.get("new_sl"))
    raw_tp = payload.get("target_price", payload.get("tp"))
    if raw_sl is None and raw_tp is None:
        return {"status": "skipped", "reason": "no_sl_or_tp", "action": "set_sl_tp"}

    # Parse both before the first broker call so an invalid target can never follow
    # an already-applied stop mutation.
    new_sl = _price(raw_sl, "stop_price") if raw_sl is not None else None
    new_tp = _price(raw_tp, "target_price") if raw_tp is not None else None

    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    sl_type = s.get("sl_order_type", "Stop")
    tp_type = s.get("tp_order_type", "Limit")

    async def apply(ex):
        contract = await ex.resolve_contract(target)
        net = 0
        try:
            for p in await ex.positions():
                if p.get("symbol") == contract:
                    net = int(p.get("netPos") or 0)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.log_event("warn", f"{tag}set_sl_tp: position lookup failed for {ex.name}: {exc}")
            return {"name": ex.name, "open": None, "info": None, "changed": False,
                    "errors": [f"position lookup failed: {exc}"]}
        if net == 0:
            return {"name": ex.name, "open": False, "info": None, "changed": False, "errors": []}

        qty = abs(net)
        exit_side = "Sell" if net > 0 else "Buy"
        tracked = (active or {}).get("accounts", {}).get(ex.name) or {}
        info = {**tracked, "name": ex.name, "contract": contract, "qty": qty}
        changed = False
        errors: list[str] = []

        if new_sl is not None:
            old = info.get("sl_order_id")
            if old:
                # Updating the tracked stop in place avoids a period where two
                # full-size stops could both execute.
                try:
                    await ex.modify_order(old, qty=qty, order_type=sl_type, stop_price=new_sl)
                    info["sl_stop"] = new_sl
                    info["sl_type"] = sl_type
                    changed = True
                except OrderOutcomeUnknown as exc:
                    info["protection_outcome_unknown"] = True
                    errors.append(f"stop update outcome unknown: {exc}")
                    state.log_event("error", f"{tag}{ex.name}: stop {old} update outcome unknown: {exc} — reconcile before another protection change")
                except TradovateError as exc:
                    errors.append(f"stop update failed: {exc}")
                    state.log_event("error", f"{tag}set stop for {ex.name} failed: {exc} (the previous stop stays tracked)")
            else:
                try:
                    order = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                                 order_type=sl_type, stop_price=new_sl)
                    info["sl_order_id"] = order.get("order_id")
                    info["sl_stop"] = new_sl
                    info["sl_type"] = sl_type
                    changed = True
                except OrderOutcomeUnknown as exc:
                    info["protection_outcome_unknown"] = True
                    errors.append(f"stop placement outcome unknown: {exc}")
                    state.log_event("error", f"{tag}{ex.name}: stop placement outcome unknown: {exc} — position protection is unresolved")
                except TradovateError as exc:
                    errors.append(f"stop placement failed: {exc}")
                    state.log_event("error", f"{tag}set stop for {ex.name} failed: {exc} — position without a tracked stop")

        if new_tp is not None:
            old_tps = [int(x) for x in (info.get("tp_order_ids") or []) if x]
            try:
                order = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                             order_type=tp_type, price=new_tp)
                new_id = int(order.get("order_id") or 0)
                if not new_id:
                    raise OrderOutcomeUnknown("replacement target was accepted without a usable order id")
            except OrderOutcomeUnknown as exc:
                # The replacement may be live but cannot be safely identified.
                # Never retire known old targets in this state.
                info["protection_outcome_unknown"] = True
                errors.append(f"target placement outcome unknown: {exc}")
                state.log_event("error", f"{tag}{ex.name}: replacement target placement outcome unknown: {exc} — old targets remain tracked")
            except TradovateError as exc:
                errors.append(f"target placement failed: {exc}")
                state.log_event("error", f"{tag}set target for {ex.name} failed: {exc}" + (" (the previous target stays)" if old_tps else ""))
            else:
                remaining_old: list[int] = []
                for oid in old_tps:
                    try:
                        await ex.cancel_order(oid)
                    except OrderOutcomeUnknown as exc:
                        still = await _order_still_working(ex, oid)
                        if still is False:
                            state.log_event("warn", f"{tag}{ex.name}: cancel of old target {oid} lost its answer, but broker reconciliation confirms it is gone")
                            continue
                        remaining_old.append(oid)
                        detail = "still working" if still else "could not be reconciled"
                        errors.append(f"old target {oid} cancel outcome unknown ({detail}): {exc}")
                        state.log_event("error", f"{tag}{ex.name}: old target {oid} cancel outcome unknown and {detail}: {exc}")
                    except TradovateError as exc:
                        remaining_old.append(oid)
                        errors.append(f"old target {oid} cancel failed: {exc}")
                        state.log_event("error", f"{tag}{ex.name}: old target {oid} could not be cancelled after the new one was placed: {exc}")

                if not remaining_old:
                    info["tp_order_ids"] = [new_id]
                    changed = True
                else:
                    # Do not knowingly leave old and replacement targets together.
                    # Roll back the replacement once; if that answer is lost,
                    # broker truth decides which ids must remain tracked.
                    replacement_live = True
                    try:
                        await ex.cancel_order(new_id)
                        replacement_live = False
                    except OrderOutcomeUnknown as exc:
                        still = await _order_still_working(ex, new_id)
                        if still is False:
                            replacement_live = False
                            state.log_event("warn", f"{tag}{ex.name}: replacement target {new_id} rollback lost its answer, but broker reconciliation confirms it is gone")
                        else:
                            detail = "still working" if still else "could not be reconciled"
                            errors.append(f"replacement target {new_id} rollback outcome unknown ({detail}): {exc}")
                            state.log_event("error", f"{tag}{ex.name}: replacement target {new_id} rollback outcome unknown and {detail}: {exc}")
                    except TradovateError as exc:
                        errors.append(f"replacement target {new_id} rollback failed: {exc}")
                        state.log_event("error", f"{tag}{ex.name}: replacement target {new_id} could not be rolled back: {exc} — multiple targets may be working")
                    info["tp_order_ids"] = ([new_id] if replacement_live else []) + remaining_old
                    if replacement_live:
                        info["protection_outcome_unknown"] = True

        return {"name": ex.name, "open": True, "info": info, "changed": changed, "errors": errors}

    results = await asyncio.gather(*(apply(ex) for ex in executors), return_exceptions=True)

    acct_state: dict[str, dict[str, Any]] = {}
    applied = 0
    failed: list[str] = []
    saw_open = False
    for ex, result in zip(executors, results):
        if isinstance(result, Exception):
            failed.append(ex.name)
            state.log_event("warn", f"{tag}set_sl_tp failed for {ex.name}: {result}")
            continue
        if result["open"] is None:
            failed.append(result["name"])
            continue
        if not result["open"]:
            continue
        saw_open = True
        info = result["info"]
        if info is not None:
            acct_state[result["name"]] = info
        if result["changed"]:
            applied += 1
        if result["errors"]:
            failed.append(result["name"])

    if acct_state:
        with _lock:
            cur = active_map.get(key) or {
                "webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                "root": root, "contract": target, "side": (active or {}).get("side"),
                "accounts": {}, "ts": time.time(),
            }
            cur.setdefault("accounts", {}).update(acct_state)
            active_map[key] = cur

    parts = []
    if new_sl is not None:
        parts.append(f"SL {new_sl}")
    if new_tp is not None:
        parts.append(f"TP {new_tp}")

    failed = list(dict.fromkeys(failed))
    if failed:
        state.log_event("error", f"{tag}[{webhook.get('name', '?')}] {' & '.join(parts) or 'SL/TP'} unresolved on {', '.join(failed)} for {root}")
        return {"status": "error", "reason": "protection_update_failed", "action": "set_sl_tp",
                "accounts": applied, "failed": failed, "sl": new_sl, "tp": new_tp,
                "simulated": tag != ""}
    if not saw_open:
        state.log_event("info", f"{tag}[{webhook.get('name', '?')}] {' & '.join(parts) or 'SL/TP'} — no open position to protect for {root}")
        return {"status": "skipped", "reason": "no_open_position", "action": "set_sl_tp"}
    if applied == 0:
        state.log_event("error", f"{tag}[{webhook.get('name', '?')}] {' & '.join(parts) or 'SL/TP'} could not be applied to the open position for {root}")
        return {"status": "error", "reason": "protection_update_failed", "action": "set_sl_tp",
                "accounts": 0, "sl": new_sl, "tp": new_tp, "simulated": tag != ""}

    state.log_event("info", f"{tag}[{webhook.get('name', '?')}] {' & '.join(parts)} set on {applied}/{len(executors)} account(s) for {root}")
    return {"status": "ok", "action": "set_sl_tp", "accounts": applied,
            "sl": new_sl, "tp": new_tp, "simulated": tag != ""}

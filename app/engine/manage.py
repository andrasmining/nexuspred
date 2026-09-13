"""Strategy-agnostic position management: ``close_all`` and ``set_sl_tp``."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import broker, config, state
from ..tradovate import OrderOutcomeUnknown, TradovateError
from .common import OrdersLeftWorking, _close_contract, _close_untracked, _lock, _order_still_working, _price, _trade_key, _untrack_after_close


async def _nothing() -> tuple[list[str], list[str]]:
    return [], []


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
        # Only this contract's working orders — other symbols keep their stops.
        return await _close_contract(ex, tag, contract)

    # the tracked closes and the look at the untracked accounts (closed too when
    # they hold the contract) run at the same time: nothing waits for a read
    results, (extra_closed, extra_failed) = await asyncio.gather(
        asyncio.gather(*(close_account(ex) for ex in targets), return_exceptions=True),
        _close_untracked(executors, {ex.name for ex in targets}, tag, target) if tracked_names else _nothing(),
    )
    cancelled = sum(r for r in results if isinstance(r, int))
    failed = [ex.name for ex, r in zip(targets, results) if isinstance(r, Exception)]
    succeeded = [ex.name for ex, r in zip(targets, results) if isinstance(r, int)]
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


async def handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook, *, settings=None):
    """Set / replace the protective STOP and/or TARGET on the current open position
    to match a signal provider's latest stop/target.

    Unlike ``move_sl`` (which only *moves* a pre-existing tracked stop), this works
    when the entry placed no bracket yet — it looks at the live position, and:
      * sets a **stop** when the signal carries one: a tracked stop is modified
        in place (one broker call, never two full-size stops working at once);
        a rejected modify falls back to place-new-then-cancel-old, so a stale
        or ineligible tracked id still ends in a protected position; with no
        tracked stop a fresh one is placed,
      * places/replaces a **target** (limit) order when the signal carries one:
        the new target is placed first, then the old ones are retired; when an
        old target will not cancel the replacement is rolled back, so two
        targets never knowingly work at once,
      * ignores the side that isn't present (e.g. a target-only move, stop = "—"),
      * flattens nothing — it only manages protective orders.
    An order whose outcome is unknown is never re-placed: it stays tracked and
    the account is reported in ``failed``. Every account whose protection could
    not be brought to the requested state is in ``failed`` (status ``error``);
    an account the broker would not even list positions for is a failure too,
    never "no open position".
    """
    s = settings if settings is not None else config.load_settings()
    new_sl = payload.get("stop_price", payload.get("new_sl"))
    new_tp = payload.get("target_price", payload.get("tp"))
    if new_sl is None and new_tp is None:
        return {"status": "skipped", "reason": "no_sl_or_tp", "action": "set_sl_tp"}
    # both prices parsed before any broker call: a bad target must not follow a
    # stop that was already replaced (two stops working on one position)
    if new_sl is not None:
        new_sl = _price(new_sl, "stop_price")
    if new_tp is not None:
        new_tp = _price(new_tp, "target_price")

    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    sl_type = s.get("sl_order_type", "Stop")
    tp_type = s.get("tp_order_type", "Limit")

    async def set_stop(ex, contract, exit_side, qty, info, errors) -> bool:
        """The stop leg; True when the position now carries the requested stop."""
        old = info.get("sl_order_id")
        if old:
            try:
                await ex.modify_order(old, qty=qty, order_type=sl_type, stop_price=new_sl)
                info["sl_stop"] = new_sl
                info["sl_type"] = sl_type
                return True
            except OrderOutcomeUnknown as exc:
                # the modify may have applied: another stop could double the protection
                errors.append(f"stop update outcome unknown: {exc}")
                state.log_event("error", f"{tag}{ex.name}: update of stop {old} lost its answer ({exc}) — the stop stays tracked as it was; check it by hand")
                return False
            except TradovateError as exc:
                # confirmed rejection (stale id, filled, not eligible): fall back to a
                # fresh stop, then retire the old one — the position is protected either way
                state.log_event("warn", f"{tag}{ex.name}: stop {old} could not be modified ({exc}) — placing a fresh stop instead")
        try:
            o = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                     order_type=sl_type, stop_price=new_sl)
        except OrderOutcomeUnknown as exc:
            errors.append(f"stop placement outcome unknown: {exc}")
            state.log_event("error", f"{tag}{ex.name}: placement of the stop lost its answer ({exc}) — not re-placed; check the position by hand")
            return False
        except TradovateError as exc:
            errors.append(f"stop placement failed: {exc}")
            state.log_event("error", f"{tag}set stop for {ex.name} failed: {exc}" + (" (the previous stop stays)" if old else " — position without a stop"))
            return False
        info["sl_order_id"] = o.get("order_id")
        info["sl_stop"] = new_sl
        info["sl_type"] = sl_type
        if old:
            try:
                await ex.cancel_order(old)
            except TradovateError as exc:
                errors.append(f"old stop {old} not cancelled: {exc}")
                state.log_event("error", f"{tag}{ex.name}: old stop {old} could not be cancelled after the new one was placed: {exc} — two stops may be working, cancel it by hand")
        return True

    async def cancel_or_confirm_gone(ex, oid, what, errors) -> bool:
        """Cancel one order; True when it is gone (confirmed, or the broker no
        longer lists it after a lost answer). A refused cancel is False."""
        try:
            await ex.cancel_order(oid)
            return True
        except OrderOutcomeUnknown as exc:
            still = await _order_still_working(ex, oid)
            if still is False:
                state.log_event("warn", f"{tag}{ex.name}: cancel of {what} {oid} lost its answer but the broker no longer lists it")
                return True
            detail = "still working" if still else "could not be re-read"
            errors.append(f"{what} {oid} cancel outcome unknown ({detail}): {exc}")
            state.log_event("error", f"{tag}{ex.name}: cancel of {what} {oid} lost its answer and the order is {detail}: {exc}")
            return False
        except TradovateError as exc:
            errors.append(f"{what} {oid} not cancelled: {exc}")
            state.log_event("error", f"{tag}{ex.name}: {what} {oid} could not be cancelled: {exc}")
            return False

    async def set_target(ex, contract, exit_side, qty, info, errors) -> bool:
        """The target leg; True when the position now carries the requested target."""
        old_tps = [oid for oid in (info.get("tp_order_ids") or []) if oid]
        try:
            o = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                     order_type=tp_type, price=new_tp)
        except OrderOutcomeUnknown as exc:
            # the replacement may be live but cannot be identified: the old targets
            # stay tracked and nothing is retired
            errors.append(f"target placement outcome unknown: {exc}")
            state.log_event("error", f"{tag}{ex.name}: placement of the target lost its answer ({exc}) — the previous target(s) stay; check the account by hand")
            return False
        except TradovateError as exc:
            errors.append(f"target placement failed: {exc}")
            state.log_event("error", f"{tag}set target for {ex.name} failed: {exc}" + (" (the previous target stays)" if old_tps else ""))
            return False
        new_id = o.get("order_id")
        # retire the previous target only once the new one works
        remaining_old = [oid for oid in old_tps if not await cancel_or_confirm_gone(ex, oid, "old target", errors)]
        if not remaining_old:
            info["tp_order_ids"] = [new_id] if new_id else []
            return True
        # an old target survives: two targets working could close more than the
        # position holds, so the replacement is rolled back — one target, the old one
        replacement_live = True
        if new_id:
            rollback_errors: list[str] = []
            replacement_live = not await cancel_or_confirm_gone(ex, new_id, "replacement target", rollback_errors)
            errors.extend(rollback_errors)
        info["tp_order_ids"] = ([new_id] if replacement_live and new_id else []) + remaining_old
        state.log_event("error", f"{tag}{ex.name}: target {new_tp} not applied — the previous target could not be retired"
                                 + ("; the replacement is working too, cancel one by hand" if replacement_live else "; the replacement was rolled back"))
        return False

    async def apply(ex):
        contract = await ex.resolve_contract(target)
        net = 0
        try:
            with broker.urgent():                        # a protective change reads first: not behind the polls
                rows = await ex.positions()
            for p in rows:
                if p.get("symbol") == contract:
                    net = int(p.get("netPos") or 0)
                    break
        except TradovateError as exc:
            # unreadable is not "flat": the account is reported, never skipped
            state.log_event("error", f"{tag}set_sl_tp: position lookup failed for {ex.name}: {exc} — its protection was not touched")
            return {"name": ex.name, "open": None, "info": None, "errors": [f"position lookup failed: {exc}"]}
        if net == 0:
            return {"name": ex.name, "open": False, "info": None, "errors": []}
        qty = abs(net)
        exit_side = "Sell" if net > 0 else "Buy"

        tracked = (active or {}).get("accounts", {}).get(ex.name) or {}
        info = {**tracked, "name": ex.name, "contract": contract, "qty": qty}
        errors: list[str] = []
        ok = True
        if new_sl is not None:
            ok = await set_stop(ex, contract, exit_side, qty, info, errors) and ok
        if new_tp is not None:
            ok = await set_target(ex, contract, exit_side, qty, info, errors) and ok
        return {"name": ex.name, "open": True, "info": info, "errors": errors, "ok": ok}

    results = await asyncio.gather(*(apply(ex) for ex in executors), return_exceptions=True)

    acct_state: dict[str, dict[str, Any]] = {}
    applied = 0
    failed: list[str] = []
    saw_open = False
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            failed.append(ex.name)
            state.log_event("error", f"{tag}set_sl_tp failed for {ex.name}: {r}")
            continue
        if r["open"] is None:
            failed.append(r["name"])
            continue
        if not r["open"]:
            continue
        saw_open = True
        acct_state[r["name"]] = r["info"]           # tracked either way: every id that may be live stays known
        if r["ok"] and not r["errors"]:
            applied += 1
        else:
            failed.append(r["name"])

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
    what = " & ".join(parts) or "SL/TP"
    failed = list(dict.fromkeys(failed))
    if failed:
        state.log_event("error", f"{tag}[{webhook.get('name', '?')}] {what} could not be applied on {', '.join(failed)} for {root}"
                                 + (f" (applied on {applied})" if applied else ""))
        return {"status": "error", "reason": "protection_update_failed", "action": "set_sl_tp",
                "accounts": applied, "failed": failed, "sl": new_sl, "tp": new_tp, "simulated": tag != ""}
    if not saw_open:
        state.log_event("info", f"{tag}[{webhook.get('name', '?')}] {what} — no open position to protect for {root}")
        return {"status": "skipped", "reason": "no_open_position", "action": "set_sl_tp"}
    state.log_event("info", f"{tag}[{webhook.get('name', '?')}] {what} set on "
                    f"{applied}/{len(executors)} account(s) for {root}")
    return {"status": "ok", "action": "set_sl_tp", "accounts": applied,
            "sl": new_sl, "tp": new_tp, "simulated": tag != ""}

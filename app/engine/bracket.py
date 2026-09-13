"""``bracket`` strategy: market entry + TP1-3 limits + protective stop, then
``move_sl`` (break-even / trailing) and ``trail_active`` (stop resize)."""
from __future__ import annotations

import time

import asyncio
from typing import Any

from .. import config, events, state
from ..tradovate import OrderOutcomeUnknown, TradovateError
from .common import _collect_entries, _lock, _opposite, _place_stop_with_retry, _price, _resize_stop, _signal_qty, SignalError, _tp_index_from_event, _trade_key
from ..sizing import account_qty


async def handle_entry(payload, action, root, target, executors, active_map, tag, webhook, *, settings=None):
    s = settings if settings is not None else config.load_settings()
    default_qty = int(webhook.get("default_qty", 3))
    base_qty = int(_signal_qty(payload.get("qty", payload.get("contracts")), default_qty, strict=False))
    base_tp_qty = int(webhook.get("tp_qty", 1))
    entry_side = "Buy" if action == "buy" else "Sell"
    exit_side = _opposite(action)
    sl_type = s.get("sl_order_type", "Stop")
    # Every numeric field is parsed before the first broker mutation.
    sl_price = _price(payload["sl"], "sl") if payload.get("sl") is not None else None
    tps = [_price(payload[k], k) for k in ("tp1", "tp2", "tp3") if payload.get(k) is not None]
    entry_price = _price(payload["entry"], "entry") if payload.get("entry") is not None else None

    async def place_for(ex):
        contract = await ex.resolve_contract(target)
        entry_qty = account_qty(ex, base_qty)
        tp_qty = min(entry_qty, account_qty(ex, base_tp_qty, of_entry=base_qty))

        entry = await ex.place_order(
            symbol=contract, action=entry_side, qty=entry_qty,
            order_type=s.get("entry_order_type", "Market"), price=entry_price,
        )
        acc_orders = [entry]

        bracket: list[tuple[str, Any]] = []
        remaining = entry_qty
        for tp_price in tps:
            if remaining > 0:
                slice_qty = min(tp_qty, remaining)
                remaining -= slice_qty
                bracket.append(("tp", ex.place_order(
                    symbol=contract, action=exit_side, qty=slice_qty,
                    order_type=s.get("tp_order_type", "Limit"), price=tp_price)))
        tp_ids: list[int] = []
        sl_id = None
        if bracket:
            kinds = [k for k, _ in bracket]
            results = await asyncio.gather(*(c for _, c in bracket), return_exceptions=True)
            for kind, res in zip(kinds, results):
                if isinstance(res, Exception):
                    state.log_event("warn", f"{tag}{kind} order failed for {ex.name}: {res}")
                    continue
                acc_orders.append(res)
                if kind == "tp" and res.get("order_id"):
                    tp_ids.append(res["order_id"])

        protection_error = ""
        if sl_price is not None:
            try:
                sl = await _place_stop_with_retry(
                    ex, symbol=contract, action=exit_side, qty=entry_qty,
                    order_type=sl_type, stop_price=sl_price, tag=tag,
                    cancel_ids=tp_ids,
                    resting_entry_id=entry.get("order_id") if str(entry.get("order_type") or s.get("entry_order_type", "Market")) != "Market" else None,
                )
            except OrderOutcomeUnknown as exc:
                # The entry is confirmed, and the stop may also be live. Never
                # place another stop and never forget the confirmed exposure.
                sl = None
                protection_error = str(exc)
                state.log_event("error", f"{tag}{ex.name}: protective stop outcome unknown after confirmed entry: {exc}")
            if sl is not None:
                acc_orders.append(sl)
                sl_id = sl.get("order_id")
            elif not protection_error:
                # Known stop failure plus an unconfirmed automatic close: the
                # position can still be live and must remain tracked.
                protection_error = "protective stop failed and automatic entry close was not confirmed"

        info = {
            "name": ex.name, "contract": contract, "entry_qty": entry_qty,
            "tp_qty": tp_qty, "qty": entry_qty, "entry_price": entry_price,
            "sl_order_id": sl_id, "sl_type": sl_type,
            "sl_stop": sl_price,
            "tp_order_ids": tp_ids,
        }
        if protection_error:
            info["protection_outcome_unknown"] = True
            info["protection_error"] = protection_error[:300]
        return ex.name, info, acc_orders, contract

    results = await asyncio.gather(*(place_for(ex) for ex in executors), return_exceptions=True)

    acct_state, orders, summary, contract = _collect_entries(
        executors, results, tag=tag, label="Entry", fallback_contract=target, qty_key="entry_qty"
    )
    failed = [ex.name for ex, res in zip(executors, results) if isinstance(res, Exception)]
    failed += [res[0] for res in results
               if not isinstance(res, Exception) and res[1].get("protection_outcome_unknown")]
    failed = list(dict.fromkeys(failed))

    if acct_state:
        key = _trade_key(webhook["id"], root)
        with _lock:
            active_map[key] = {
                "webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                "root": root, "contract": contract, "side": action, "qty": base_qty,
                "accounts": acct_state, "ts": time.time(),
            }

    state.log_event(
        "error" if failed else "info",
        f"{tag}[{webhook.get('name', '?')}] Entry {action.upper()} {contract} "
        f"placed on {len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}"
        + (f"; unresolved/failed: {', '.join(failed)}" if failed else ""),
    )
    if acct_state and not tag:
        events.emit("trade.executed", webhook=webhook.get("name", "?"), action=action, contract=contract, accounts=list(acct_state), settings=s)
    out = {"status": "error" if failed else "ok", "action": action, "contract": contract,
           "accounts": summary, "orders": orders, "simulated": tag != ""}
    if failed:
        out["failed"] = failed
    return out


def _remaining_qty(info: dict[str, Any], tp_index: int | None) -> int:
    if tp_index is None:
        return int(info.get("qty") or info.get("entry_qty", 1))
    return max(0, int(info["entry_qty"]) - tp_index * int(info["tp_qty"]))


def _is_breakeven_move(payload: dict[str, Any], tp_index: int | None) -> bool:
    msg = str(payload.get("message", "")).lower()
    return tp_index == 1 or "breakeven" in msg or "break-even" in msg


async def handle_move_sl(payload, root, executors, active_map, tag, webhook, *, settings=None):
    s = settings if settings is not None else config.load_settings()
    new_sl = payload.get("new_sl", payload.get("sl"))
    if new_sl is not None:
        new_sl = _price(new_sl, "new_sl")

    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    if not active or not active.get("accounts"):
        state.log_event("warn", f"{tag}No tracked stop-loss for {root} to move")
        return {"status": "skipped", "reason": "no_active_stop", "action": "move_sl"}

    tp_index = _tp_index_from_event(payload)
    use_entry = bool(s.get("breakeven_to_entry", True)) and _is_breakeven_move(payload, tp_index)
    if not use_entry and new_sl is None:
        raise SignalError("move_sl signal missing 'new_sl'")

    async def move_account(ex):
        info = active["accounts"].get(ex.name)
        if not info or not info.get("sl_order_id"):
            return None
        entry_price = info.get("entry_price")
        if use_entry and entry_price is not None:
            stop = float(entry_price)
        elif new_sl is not None:
            stop = new_sl
        else:
            state.log_event("warn", f"{tag}move_sl for {root}: no stop price available")
            return None
        qty = _remaining_qty(info, tp_index)
        if qty <= 0:
            # A zero-quantity stop is not a safe substitute for retiring it.
            await ex.cancel_order(info["sl_order_id"])
            info["sl_order_id"] = None
            info["qty"] = 0
            return None
        await _resize_stop(ex, info, qty, stop)
        info["sl_stop"] = stop
        return stop

    results = await asyncio.gather(*(move_account(ex) for ex in executors), return_exceptions=True)
    stops = [r for r in results if isinstance(r, (int, float))]
    moved = len(stops)
    last_stop = stops[-1] if stops else None
    failed = [ex.name for ex, r in zip(executors, results) if isinstance(r, Exception)]
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}move_sl failed for {ex.name}/{root}: {r}")

    where = "break-even/entry" if use_entry else "new_sl"
    state.log_event(
        "error" if failed else "info",
        f"{tag}Stop-loss for {root} moved to {last_stop} ({where}, qty→remaining) on {moved} account(s)"
        + (f"; failed: {', '.join(failed)}" if failed else ""),
    )
    out = {"status": "error" if failed else "ok", "action": "move_sl", "new_sl": last_stop,
           "breakeven_to_entry": use_entry, "accounts": moved, "simulated": tag != ""}
    if failed:
        out["failed"] = failed
    return out


async def handle_trail_active(payload, root, executors, active_map, tag, webhook):
    """Resize the stop to the remaining position and surface any broker failure."""
    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    tp_index = _tp_index_from_event(payload)
    if not active or not active.get("accounts") or tp_index is None:
        state.log_event("info", f"{tag}Trailing active for {root} (handled by strategy)")
        return {"status": "ok", "action": "trail_active", "note": "acknowledged",
                "simulated": tag != ""}

    async def resize_account(ex) -> bool:
        info = active["accounts"].get(ex.name)
        if not info or not info.get("sl_order_id"):
            return False
        qty = _remaining_qty(info, tp_index)
        if qty <= 0:
            await ex.cancel_order(info["sl_order_id"])
            info["sl_order_id"] = None
            info["qty"] = 0
            return True
        await _resize_stop(ex, info, qty, info.get("sl_stop"))
        return True

    results = await asyncio.gather(*(resize_account(ex) for ex in executors), return_exceptions=True)
    resized = sum(1 for r in results if r is True)
    failed = [ex.name for ex, r in zip(executors, results) if isinstance(r, Exception)]
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}trail_active failed for {ex.name}/{root}: {r}")

    state.log_event(
        "error" if failed else "info",
        f"{tag}Trailing active for {root} — stop-loss qty→remaining on {resized} account(s)"
        + (f"; failed: {', '.join(failed)}" if failed else ""),
    )
    out = {"status": "error" if failed else "ok", "action": "trail_active", "accounts": resized,
           "simulated": tag != ""}
    if failed:
        out["failed"] = failed
    return out

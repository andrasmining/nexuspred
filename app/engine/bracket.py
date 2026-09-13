"""``bracket`` strategy: market entry + TP1-3 limits + protective stop, then
``move_sl`` (break-even / trailing) and ``trail_active`` (stop resize)."""
from __future__ import annotations


import asyncio
from typing import Any

from .. import config, state
from ..tradovate import TradovateError
from .common import _collect_entries, _entry_result, _lock, _opposite, _place_stop_with_retry, _price, _resize_stop, _retire_extra_stops, _signal_qty, SignalError, _tp_index_from_event, _trade_key, _untrack_if_flat, _track_entry
from ..sizing import account_qty


async def handle_entry(payload, action, root, target, executors, active_map, tag, webhook, *, settings=None):
    s = settings if settings is not None else config.load_settings()
    # Honour the signal's contract count (payload 'qty'/'contracts'); fall back to
    # the webhook default only when the signal doesn't specify one.
    default_qty = int(webhook.get("default_qty", 3))
    base_qty = int(_signal_qty(payload.get("qty", payload.get("contracts")), default_qty, strict=False))
    base_tp_qty = int(webhook.get("tp_qty", 1))
    entry_side = "Buy" if action == "buy" else "Sell"
    exit_side = _opposite(action)
    sl_type = s.get("sl_order_type", "Stop")
    # every price is parsed before the first broker call: a malformed target must
    # never leave a live entry untracked and unprotected
    sl_price = _price(payload["sl"], "sl") if payload.get("sl") is not None else None
    tps = [(n, _price(payload[k], k)) for n, k in ((1, "tp1"), (2, "tp2"), (3, "tp3")) if payload.get(k) is not None]
    entry_price = _price(payload["entry"], "entry") if payload.get("entry") is not None else None

    async def place_for(ex):
        contract = await ex.resolve_contract(target)
        entry_qty = account_qty(ex, base_qty)
        tp_qty = min(entry_qty, account_qty(ex, base_tp_qty, of_entry=base_qty))

        # 1) Market entry first (so the position exists before the brackets).
        entry = await ex.place_order(
            symbol=contract, action=entry_side, qty=entry_qty,
            order_type=s.get("entry_order_type", "Market"), price=entry_price,
        )
        acc_orders = [entry]

        # 2) The protective stop and the TP limit orders leave together — the
        #    stop first in the line, so the position is covered one round trip
        #    after the entry instead of after the last target's answer. A stop
        #    that fails on this first attempt gets its retry (or the
        #    cancel-and-close resolution) once the targets' ids are known.
        bracket: list[tuple[str, Any]] = []
        remaining = entry_qty                       # the TP slices together never exceed the entry
        tp_slices: list[list[int]] = []
        for n, tp_price in tps:
            if remaining > 0:
                slice_qty = min(tp_qty, remaining)
                remaining -= slice_qty
                tp_slices.append([n, slice_qty])
                bracket.append(("tp", ex.place_order(
                    symbol=contract, action=exit_side, qty=slice_qty,
                    order_type=s.get("tp_order_type", "Limit"), price=tp_price)))
        if sl_price is not None:
            bracket.insert(0, ("stop", ex.place_order(symbol=contract, action=exit_side, qty=entry_qty,
                                                     order_type=sl_type, stop_price=sl_price)))
        tp_ids: list[int] = []
        sl_id = None
        sl: dict[str, Any] | None = None
        first_error: Exception | None = None
        if bracket:
            kinds = [k for k, _ in bracket]
            results = await asyncio.gather(*(c for _, c in bracket), return_exceptions=True)
            for kind, res in zip(kinds, results):
                if kind == "stop":
                    if isinstance(res, asyncio.CancelledError):
                        raise res
                    if isinstance(res, Exception):
                        first_error = res
                    else:
                        sl = res
                    continue
                if isinstance(res, Exception):
                    state.log_event("warn", f"{tag}{kind} order failed for {ex.name}: {res}")
                    continue
                acc_orders.append(res)
                if kind == "tp" and res.get("order_id"):
                    tp_ids.append(res["order_id"])
        if sl_price is not None and sl is None:
            # the first attempt failed: one retry on a confirmed rejection, a loud
            # resolution otherwise — the entry is live by now
            sl = await _place_stop_with_retry(ex, symbol=contract, action=exit_side, qty=entry_qty,
                                              order_type=sl_type, stop_price=sl_price, tag=tag,
                                              cancel_ids=tp_ids, first_error=first_error,
                                              resting_entry_id=entry.get("order_id") if str(entry.get("order_type") or s.get("entry_order_type", "Market")) != "Market" else None)
        if sl is not None:
            acc_orders.append(sl)
            sl_id = sl.get("order_id")

        info = {
            "name": ex.name, "contract": contract, "entry_qty": entry_qty,
            **({"unprotected": True} if sl_price is not None and sl is None else {}),   # the stop failed and the close after it failed too
            "tp_qty": tp_qty, "qty": entry_qty, "entry_price": entry_price,
            "sl_order_id": sl_id, "sl_type": sl_type,
            "sl_stop": sl_price,
            "tp_order_ids": tp_ids, "tp_slices": tp_slices,
        }
        return ex.name, info, acc_orders, contract

    # All enabled accounts execute simultaneously.
    results = await asyncio.gather(*(place_for(ex) for ex in executors), return_exceptions=True)

    acct_state, orders, summary, contract = _collect_entries(executors, results, tag=tag, label="Entry", fallback_contract=target, qty_key="entry_qty")

    _track_entry(active_map, _trade_key(webhook["id"], root), {"side": action, "qty": base_qty}, acct_state,
                 tag=tag, webhook=webhook, root=root, action=action, contract=contract, settings=s)
    # a failed account is isolated (and, after a failed stop, closed again by the
    # engine): the entry stays "ok" for the others; the names travel in ``failed``
    return _entry_result({"status": "ok", "action": action, "contract": contract, "accounts": summary, "orders": orders, "simulated": tag != ""},
                         executors, results, acct_state, tag=tag,
                         line=f"{tag}[{webhook.get('name', '?')}] Entry {action.upper()} {contract} placed on {len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}")


def _remaining_qty(info: dict[str, Any], tp_index: int | None) -> int:
    """Position left after ``tp_index`` take-profits filled. The entry records
    the slices it placed as ``tp_slices`` [[n, qty], ...] (a payload with tp1 and
    tp3 only has two); a record without them (set_sl_tp's) counts ``tp_qty``
    per target. 0 means the last target closed the position: the stop is retired."""
    entry = int(info.get("entry_qty") or info.get("qty") or 0)
    if tp_index is None:
        return int(info.get("qty") or entry or 1)
    slices = info.get("tp_slices")
    if slices:
        return max(0, entry - sum(int(q) for n, q in slices if int(n) <= tp_index))
    return max(0, entry - tp_index * int(info.get("tp_qty") or 1))


def _is_breakeven_move(payload: dict[str, Any], tp_index: int | None) -> bool:
    """A move_sl that means 'go to break-even' (TP1, or a breakeven message)."""
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

    tp_index = _tp_index_from_event(payload)   # e.g. tp1_hit -> 1 contract gone -> qty 2
    # Break-even = the original entry price (configurable); trailing moves use new_sl.
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
            # every target filled: a stop left working would open a reverse trade
            try:
                await ex.cancel_order(info["sl_order_id"])
            except TradovateError as exc:
                state.log_event("error", f"{tag}{ex.name}: stop {info['sl_order_id']} could not be retired after the last target: {exc}")
                raise
            info["sl_order_id"] = None
            info["qty"] = 0
            await _retire_extra_stops(ex, info, tag)
            return None
        await _resize_stop(ex, info, qty, stop)
        info["sl_stop"] = stop
        await _retire_extra_stops(ex, info, tag)
        return stop

    results = await asyncio.gather(
        *(move_account(ex) for ex in executors), return_exceptions=True
    )
    _untrack_if_flat(active_map, key)                  # every target filled and the stops retired: the trade is over
    stops = [r for r in results if isinstance(r, (int, float))]
    moved = len(stops)
    last_stop = stops[-1] if stops else None
    failed = [ex.name for ex, r in zip(executors, results) if isinstance(r, Exception)]
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}move_sl failed for {ex.name}/{root}: {r} — its stop is where it was")

    where = "break-even/entry" if use_entry else "new_sl"
    state.log_event(
        "error" if failed else "info",
        f"{tag}Stop-loss for {root} moved to {last_stop} ({where}, "
        f"qty→remaining) on {moved} account(s)"
        + (f"; failed: {', '.join(failed)}" if failed else ""),
    )
    out = {"status": "error" if failed else "ok", "action": "move_sl", "new_sl": last_stop,
           "breakeven_to_entry": use_entry, "accounts": moved, "simulated": tag != ""}
    if failed:
        out["failed"] = failed
    return out


async def handle_trail_active(payload, root, executors, active_map, tag, webhook):
    """TP2 (trail_active): resize the stop to the remaining position; price unchanged."""
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
            # every target filled: a stop left working would open a reverse trade
            # (a modify to qty 0 is a rejection at the broker, not a cancel)
            try:
                await ex.cancel_order(info["sl_order_id"])
            except TradovateError as exc:
                state.log_event("error", f"{tag}{ex.name}: stop {info['sl_order_id']} could not be retired after the last target: {exc} — cancel it by hand")
                raise
            info["sl_order_id"] = None
            info["qty"] = 0
            await _retire_extra_stops(ex, info, tag)
            return True
        await _resize_stop(ex, info, qty, info.get("sl_stop"))
        await _retire_extra_stops(ex, info, tag)
        return True

    results = await asyncio.gather(
        *(resize_account(ex) for ex in executors), return_exceptions=True
    )
    _untrack_if_flat(active_map, key)
    resized = sum(1 for r in results if r is True)
    failed = [ex.name for ex, r in zip(executors, results) if isinstance(r, Exception)]
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}trail_active failed for {ex.name}/{root}: {r} — its stop still covers the old quantity")

    state.log_event(
        "error" if failed else "info",
        f"{tag}Trailing active for {root} — stop-loss qty→remaining "
        f"on {resized} account(s)"
        + (f"; failed: {', '.join(failed)}" if failed else ""),
    )
    out = {"status": "error" if failed else "ok", "action": "trail_active", "accounts": resized,
           "simulated": tag != ""}
    if failed:
        out["failed"] = failed
    return out

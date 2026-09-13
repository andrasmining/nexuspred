"""Primitives shared by every strategy handler."""
from __future__ import annotations

import asyncio
import math
import re
import threading
from typing import Any

from .. import events, state
from ..tradovate import OrderOutcomeUnknown, TradovateError


class SignalError(Exception):
    """Raised for malformed or rejected signals."""


# Guards the active-trade maps (see app.signals) while a handler reads/writes
# a trade record.
_lock = threading.Lock()

_CONTRACT_RE = re.compile(r"^([A-Z]{1,4})([FGHJKMNQUVXZ])(\d{1,2})$")
_TP_RE = re.compile(r"tp(\d)", re.IGNORECASE)


def _tp_index_from_event(payload: dict[str, Any]) -> int | None:
    """How many take-profits have filled, parsed from the event (e.g. ``tp2_hit`` → 2)."""
    m = _TP_RE.search(str(payload.get("event", "")))
    return int(m.group(1)) if m else None


def _base_root(name: str) -> str:
    """Reduce a contract/symbol to its root: ``MNQU6`` → ``MNQ``, ``MNQ1!`` → ``MNQ``."""
    m = _CONTRACT_RE.match(name)
    if m:
        return m.group(1)
    return name.replace("1!", "").strip()


def _resolve_symbol(s: dict[str, Any], tv_symbol: str) -> tuple[str, str, bool]:
    """Return (target_contract, base_root, allowed) for a TradingView symbol.

    The configured mapping (``symbol_map``) is the source of truth: if the symbol
    is mapped, that exact contract (e.g. ``MNQU6``) is traded and the signal is
    allowed. Unmapped symbols fall back to the stripped root and are gated by
    ``allowed_symbols``.
    """
    mapped = s.get("symbol_map", {}).get(tv_symbol)
    if mapped:
        return mapped, _base_root(mapped), True
    root = _base_root(tv_symbol)
    return root, root, root in s.get("allowed_symbols", [])


def _opposite(action: str) -> str:
    return "Sell" if action.lower() == "buy" else "Buy"


def _trade_key(webhook_id: str, root: str) -> str:
    return f"{webhook_id}:{root}"


async def _orders_for_contract(ex: Any, orders: list[dict[str, Any]], contract: str,
                               tag: str) -> list[dict[str, Any]]:
    """The subset of ``orders`` that belongs to ``contract``.

    A close for one symbol must not strip the protective stops / targets of
    positions in other symbols on the same account. Tradovate orders carry a
    numeric ``contractId``; the simulator's carry the contract ``symbol``. When
    neither can be matched the order is left alone (and reported), because
    cancelling it could unprotect an unrelated position.
    """
    try:
        cid = int(await ex.contract_id(contract))
    except (TradovateError, AttributeError, TypeError, ValueError):
        cid = 0
    mine: list[dict[str, Any]] = []
    identifiable = 0
    for o in orders:
        name = str(o.get("symbol") or o.get("contract") or "")
        oid = o.get("contractId")
        has_id = isinstance(oid, int) and oid > 0
        if name or has_id:
            identifiable += 1
        if (name and name == contract) or (cid and has_id and oid == cid):
            mine.append(o)
    if orders and not identifiable:
        state.log_event("error", f"{tag}Working orders on {ex.name} carry no contract — none cancelled; "
                                 f"check the account for stops / targets left on {contract}")
        return []
    if len(mine) < identifiable:
        state.log_event("info", f"{tag}Keeping {identifiable - len(mine)} working order(s) on {ex.name} "
                                f"that belong to other contracts than {contract}")
    return mine


QTY_HARD_CAP = 1000               # the ceiling sizing.normalize enforces for fixed / max_contracts


def _price(value: Any, what: str) -> float:
    """A finite float from a payload field, or SignalError — parsed *before* any
    broker call so a malformed target never leaves a live entry untracked."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise SignalError(f"'{what}' is not a number: {value!r}")
    if not math.isfinite(f):
        raise SignalError(f"'{what}' must be a finite number")
    return f


def _signal_qty(raw: Any, default: Any, *, strict: bool) -> float:
    """The signal's contract count: ``raw`` when it is a finite positive number
    within QTY_HARD_CAP, else ``default`` (bracket) or SignalError (strict)."""
    try:
        q = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        if strict:
            raise SignalError(f"Invalid qty '{raw}'")
        q = float(default)
    if not math.isfinite(q) or q <= 0 or q > QTY_HARD_CAP:
        if strict:
            raise SignalError(f"qty must be positive (at most {QTY_HARD_CAP})")
        q = float(default)
    if not math.isfinite(q) or q <= 0 or q > QTY_HARD_CAP:
        raise SignalError(f"Webhook default qty must be between 1 and {QTY_HARD_CAP}")
    return q


def _untrack_after_close(active_map: dict[str, Any], key: str, succeeded: list[str], failed: list[str]) -> None:
    """After a close: on a mixed broker outcome remove only the accounts whose
    close was confirmed (failed ones stay tracked so a retry cannot forget a
    live position or re-flatten the others); with no failure drop the record."""
    with _lock:
        cur = active_map.get(key)
        if not cur or not cur.get("accounts"):
            return
        if failed:
            accounts = cur["accounts"]
            for name in succeeded:
                accounts.pop(name, None)
            if not accounts:
                active_map.pop(key, None)
        else:
            active_map.pop(key, None)


async def _resize_stop(ex: Any, info: dict[str, Any], qty: int, stop_price: Any) -> None:
    """Modify the tracked stop to ``qty`` @ ``stop_price``; the record follows
    the broker, never precedes it."""
    await ex.modify_order(info["sl_order_id"], qty=qty, order_type=info.get("sl_type", "Stop"), stop_price=stop_price)
    info["qty"] = qty


def _collect_entries(executors: list[Any], results: list[Any], *, tag: str, label: str, fallback_contract: str,
                     qty_key: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    """The common tail of every entry handler: split the per-account results
    of ``place_for`` into the tracked accounts, the orders placed and the
    summary; a failed account is logged and skipped. Returns
    ``(acct_state, orders, summary, contract)``."""
    orders: list[dict[str, Any]] = []
    acct_state: dict[str, dict[str, Any]] = {}
    summary: list[dict[str, Any]] = []
    contract = fallback_contract
    for ex, res in zip(executors, results):
        if isinstance(res, Exception):
            state.log_event("error", f"{tag}{label} failed for {ex.name}: {res}")
            continue
        name, info, acc_orders, contract = res
        acct_state[name] = info
        orders.extend(acc_orders)
        summary.append({"account": name, "qty": info[qty_key]})
    return acct_state, orders, summary, contract


STOP_PENALTY_WAIT_S = 30.0        # the longest a protective stop waits for a 429 penalty before its retry
FLATTEN_VERIFY_DELAY_S = 0.25     # short broker-settlement wait; mutations are never replayed
FLATTEN_VERIFY_READS = 3


class StopFailed(TradovateError):
    """The protective stop could not be placed twice and the entry was closed
    again at market: the account holds nothing of this trade (policy: never
    leave an entry live without its stop)."""


async def _place_stop_with_retry(ex: Any, *, symbol: str, action: str, qty: int, order_type: str,
                                 stop_price: float, tag: str, what: str = "stop",
                                 cancel_ids: list[int] | None = None,
                                 resting_entry_id: int | None = None) -> dict[str, Any] | None:
    """Place a protective stop; retry only a confirmed failure once.

    ``OrderOutcomeUnknown`` is not a rejection: the first stop may already be
    working. It is propagated immediately so callers can keep the confirmed
    entry tracked and reconcile deliberately instead of placing a duplicate.

    A known failure is retried once. If it fails again the trade's known orders
    are cancelled and the confirmed/filled entry is closed again at market.
    """
    from ..tradovate import RateLimited
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return await ex.place_order(symbol=symbol, action=action, qty=qty, order_type=order_type, stop_price=stop_price)
        except asyncio.CancelledError:
            raise
        except OrderOutcomeUnknown:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 1:
                wait = min(float(getattr(exc, "retry_after", 0) or 0) + 0.2, STOP_PENALTY_WAIT_S) if isinstance(exc, RateLimited) else 0.5
                await asyncio.sleep(wait)

    state.log_event("error", f"{tag}{what} for {ex.name} on {symbol} FAILED twice ({last}) — closing the entry again")
    errors: list[str] = []
    ids = [o for o in [*(cancel_ids or []), resting_entry_id] if o]
    results = await asyncio.gather(*(ex.cancel_order(oid) for oid in ids), return_exceptions=True)
    errors += [f"cancel {oid}: {r}" for oid, r in zip(ids, results) if isinstance(r, Exception)]

    close_qty = qty
    if resting_entry_id:
        try:
            rows = await ex.positions()
            net = sum(int(p.get("netPos") or 0) for p in rows or [] if str(p.get("symbol") or "") == symbol)
        except Exception as exc:  # noqa: BLE001 - unknown fill state -> close the full confirmed risk amount
            state.log_event("warn", f"{tag}{ex.name}: position on {symbol} could not be read after the failed {what} ({exc}) — closing the full entry")
            net = qty if action == "Sell" else -qty
        filled = net if action == "Sell" else -net
        close_qty = max(0, min(qty, filled))
    if close_qty <= 0:
        state.log_event("error", f"{tag}{ex.name}: entry on {symbol} cancelled — the {what} could not be placed and nothing had filled")
        events.emit("execution.problem", title=f"Entry cancelled on {ex.name}", message=f"{symbol}: the {what} could not be placed ({last}); the unfilled entry was cancelled.")
        raise StopFailed(f"{what} could not be placed ({last}); unfilled entry cancelled")
    try:
        await ex.place_order(symbol=symbol, action=action, qty=close_qty, order_type="Market")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        state.log_event("error", f"{tag}{ex.name}: the close after the failed {what} FAILED too ({exc}) — position on {symbol} is unprotected")
        events.emit("execution.problem", title=f"Unprotected position on {ex.name}",
                    message=f"{symbol}: the {what} could not be placed ({last}) and the position could not be closed ({exc}). "
                            "Set a stop by hand or close the position.")
        return None
    left = f"; {len(errors)} order(s) could not be cancelled: {'; '.join(errors)[:200]} — cancel them by hand" if errors else ""
    state.log_event("error", f"{tag}{ex.name}: {close_qty} × {symbol} closed again at market — the {what} could not be placed{left}")
    events.emit("execution.problem", title=f"Entry closed again on {ex.name}",
                message=f"{symbol}: the {what} could not be placed ({last}); the {close_qty}-lot entry was closed at market{left}.")
    raise StopFailed(f"{what} could not be placed ({last}); entry closed again at market{left}")


class OrdersLeftWorking(TradovateError):
    """The position was closed but a working order survived both cancel
    attempts — the account is unresolved (a stop or target on a flat position
    would open a new trade), not "still open"."""


async def _close_contract(ex: Any, tag: str, contract: str) -> int:
    """Cancel the contract's working orders, then liquidate the position, then
    retry any cancel that failed — a stop or target left working on a flat
    position would open a new trade. Failures that survive the retry are
    reported, alerted, and raised so callers cannot treat the close as clean."""
    errors: list[str] = []
    cancelled = await _cancel_working(ex, tag, errors, contract=contract)
    await ex.liquidate_position(contract)
    if errors:
        retry_errors: list[str] = []
        cancelled += await _cancel_working(ex, tag, retry_errors, contract=contract)
        if retry_errors:
            detail = "; ".join(retry_errors)
            state.log_event("error", f"{tag}{ex.name}: working orders on {contract} could not be cancelled after the close: "
                                     f"{detail} — cancel them by hand")
            events.emit("execution.problem", title=f"Orders left working on {ex.name}",
                        message=f"{contract} was closed but {len(retry_errors)} working order(s) could not be cancelled: {detail[:300]}")
            raise OrdersLeftWorking(f"working orders remain after closing {contract}: {detail}")
    return cancelled


async def _close_untracked(executors: list[Any], tracked_names: set[str], tag: str, target: str) -> tuple[list[str], list[str]]:
    """Accounts enabled on the webhook but absent from the trade record: an
    entry whose answer was lost ("outcome unknown"), or an account routed here
    after the entry. They are closed too — but only when the broker shows a
    position in the contract, so a flat account gets no liquidate call and no
    rejection. Returns (closed, failed) account names."""
    async def one(ex: Any) -> bool:
        contract = await ex.resolve_contract(target)
        rows = await ex.positions()
        if not any(str(p.get("symbol") or "") == contract and (p.get("netPos") or 0) for p in rows or []):
            return False
        state.log_event("warn", f"{tag}{ex.name} holds {contract} without a trade record (lost entry answer or manual position) — closing it too")
        await _close_contract(ex, tag, contract)
        return True

    extra = [ex for ex in executors if ex.name not in tracked_names]
    results = await asyncio.gather(*(one(ex) for ex in extra), return_exceptions=True)
    closed, failed = [], []
    for ex, r in zip(extra, results):
        if isinstance(r, Exception):
            failed.append(ex.name)
            state.log_event("error", f"{tag}close of untracked {ex.name} FAILED: {r} — check the account")
        elif r:
            closed.append(ex.name)
    return closed, failed


async def _flatten_account(ex: Any, tag: str = "") -> tuple[int, int, list[str]]:
    """Emergency flatten reconciled against broker truth.

    Each observed symbol is liquidated at most once. Lost liquidation answers
    are never replayed: subsequent calls are reads only. A position counts as
    flattened only when the broker later reports it flat. Working orders are
    re-read after liquidation, and only orders still reported working are
    cancelled again. Returns ``(cancelled, confirmed_flattened, errors)``.
    """
    pre_errors: list[str] = []
    try:
        cancelled = await _cancel_working(ex, tag, pre_errors)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - emergency path still attempts the positions
        cancelled = 0
        pre_errors.append(f"working-order cleanup: {type(exc).__name__}: {exc}")

    try:
        positions = await ex.positions()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        positions = []
        position_read_error = f"list positions: {exc}"
    else:
        position_read_error = ""

    symbols = list(dict.fromkeys(
        str(p.get("symbol")) for p in positions or [] if p.get("symbol") and p.get("netPos")
    ))
    attempts: dict[str, BaseException | None] = {}
    if symbols:
        results = await asyncio.gather(*(ex.liquidate_position(symbol) for symbol in symbols), return_exceptions=True)
        for symbol, result in zip(symbols, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            attempts[symbol] = result if isinstance(result, BaseException) else None

    errors: list[str] = list(pre_errors)
    if position_read_error:
        errors.append(position_read_error)

    flattened = 0
    after: list[dict[str, Any]] | None = None
    if symbols:
        for read_no in range(FLATTEN_VERIFY_READS):
            try:
                after = await ex.positions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"post-close list positions: {exc}")
                after = None
                break
            residual_now = {
                str(p.get("symbol"))
                for p in after or []
                if p.get("symbol") and int(p.get("netPos") or 0)
            }
            if not any(symbol in residual_now for symbol in symbols):
                break
            if read_no + 1 < FLATTEN_VERIFY_READS:
                await asyncio.sleep(FLATTEN_VERIFY_DELAY_S)

    if after is not None:
        residual: dict[str, int] = {}
        for p in after or []:
            symbol = str(p.get("symbol") or "")
            net = int(p.get("netPos") or 0)
            if symbol and net:
                residual[symbol] = residual.get(symbol, 0) + net
        for symbol in symbols:
            result = attempts.get(symbol)
            net = residual.get(symbol, 0)
            if net == 0:
                flattened += 1
                if isinstance(result, BaseException):
                    state.log_event("warn", f"{tag}{ex.name}: liquidation of {symbol} returned {type(result).__name__}, but broker reconciliation confirms the position is flat")
                continue
            if isinstance(result, OrderOutcomeUnknown):
                errors.append(f"flatten {symbol}: outcome unknown; broker still reports position {net:+d}")
            elif isinstance(result, TradovateError):
                errors.append(f"flatten {symbol}: {result}")
            elif isinstance(result, BaseException):
                errors.append(f"flatten {symbol}: {type(result).__name__}: {result}")
            else:
                errors.append(f"flatten {symbol}: broker still reports position {net:+d} after submitted liquidation")
    elif symbols:
        for symbol in symbols:
            result = attempts.get(symbol)
            if isinstance(result, OrderOutcomeUnknown):
                errors.append(f"flatten {symbol}: outcome unknown and broker position could not be reconciled")
            elif isinstance(result, TradovateError):
                errors.append(f"flatten {symbol}: {result}")
            elif isinstance(result, BaseException):
                errors.append(f"flatten {symbol}: {type(result).__name__}: {result}")
            else:
                errors.append(f"flatten {symbol}: liquidation submitted but broker-flat state could not be confirmed")

    post_errors: list[str] = []
    try:
        cancelled += await _cancel_working(ex, tag, post_errors)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        post_errors.append(f"working-order reconciliation: {type(exc).__name__}: {exc}")
    errors.extend(f"post-close {err}" for err in post_errors)
    return cancelled, flattened, errors


async def _cancel_working(ex: Any, tag: str, errors: list[str] | None = None,
                          contract: str | None = None) -> int:
    """Cancel working orders on one account with all cancels in flight at once.
    With ``contract`` only that contract's orders are cancelled (a symbol-scoped
    close); without it every working order goes, as the SOS flatten-all needs.
    Returns how many were cancelled. Tradovate rejections are collected
    (``errors``) or logged per order; anything else propagates, as in v4."""
    try:
        orders = await ex.working_orders()
    except TradovateError as exc:
        if errors is not None:
            errors.append(f"list orders: {exc}")
        else:
            state.log_event("error", f"{tag}Could not list working orders for {ex.name}: {exc} — nothing cancelled")
        return 0
    if contract:
        orders = await _orders_for_contract(ex, orders, contract, tag)
    ids = [o.get("id") for o in orders if o.get("id") is not None]
    results = await asyncio.gather(*(ex.cancel_order(oid) for oid in ids), return_exceptions=True)
    cancelled = 0
    for oid, r in zip(ids, results):
        if isinstance(r, TradovateError):
            if errors is not None:
                errors.append(f"cancel {oid}: {r}")
            else:
                state.log_event("error", f"{tag}{ex.name}: cancel of order {oid} failed: {r}")
        elif isinstance(r, BaseException):
            raise r
        else:
            cancelled += 1
    return cancelled

"""Primitives shared by every strategy handler."""
from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from typing import Any

from .. import broker, events, state
from ..tradovate import OrderOutcomeUnknown, RateLimited, TradovateError


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


async def _contract_id_or_zero(ex: Any, contract: str) -> int:
    try:
        return int(await ex.contract_id(contract))
    except (TradovateError, AttributeError, TypeError, ValueError):
        return 0


async def _orders_for_contract(ex: Any, orders: list[dict[str, Any]], contract: str,
                               tag: str, cid: int | None = None) -> list[dict[str, Any]]:
    """The subset of ``orders`` that belongs to ``contract``.

    A close for one symbol must not strip the protective stops / targets of
    positions in other symbols on the same account. Tradovate orders carry a
    numeric ``contractId``; the simulator's carry the contract ``symbol``. When
    neither can be matched the order is left alone (and reported), because
    cancelling it could unprotect an unrelated position."""
    if cid is None:
        cid = await _contract_id_or_zero(ex, contract)
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
        # Nothing about these orders says which contract they belong to (no
        # contractId, no symbol). Cancelling them all could strip the stops of
        # unrelated positions, so they are left alone and reported loudly.
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
    if not math.isfinite(q) or q <= 0 or q > QTY_HARD_CAP or q != int(q):
        if strict:
            raise SignalError(f"qty must be a positive whole number of contracts (at most {QTY_HARD_CAP})")
        q = float(default)
    if not math.isfinite(q) or q <= 0 or q > QTY_HARD_CAP:
        raise SignalError(f"Webhook default qty must be between 1 and {QTY_HARD_CAP}")
    return q


def _track_entry(active_map: dict[str, Any], key: str, record: dict[str, Any], acct_state: dict[str, dict[str, Any]],
                 *, tag: str, webhook: dict[str, Any], root: str, action: str, contract: str, settings: Any) -> None:
    """The tail every entry handler shares: write the trade record under the
    lock (warning when it replaces a record that still lists protective orders,
    which stay at the broker unmanaged), then the ``trade.executed`` event."""
    if not acct_state:
        return
    with _lock:
        prev = active_map.get(key)
        if prev and any(a.get("sl_order_id") or a.get("tp_order_ids") for a in (prev.get("accounts") or {}).values()):
            # a new entry over a record that still lists protective orders: those orders
            # stay at the broker but are no longer moved with this trade — close_all
            # cancels the contract's orders regardless
            state.log_event("warn", f"{tag}[{webhook.get('name', '?')}] entry for {root} replaces a tracked trade whose stop / targets may still be working — they are not managed by the new trade")
        active_map[key] = {"webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                           "root": root, "contract": contract, **record, "accounts": acct_state, "ts": time.time()}
    if not tag:
        events.emit("trade.executed", webhook=webhook.get("name", "?"), action=action, contract=contract, accounts=list(acct_state), settings=settings)


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


def _entry_result(out: dict[str, Any], executors: list[Any], results: list[Any], acct_state: dict[str, dict[str, Any]],
                  *, tag: str, line: str) -> dict[str, Any]:
    """The common tail of every entry handler: the accounts that failed (the
    engine isolated them — after a failed stop it closed them again) and the
    accounts left **unprotected** (the stop failed and the close after it failed
    too: live, tracked, no confirmed stop) are named in the result and the log
    is written at error level when either list is non-empty. The status stays
    ``ok`` for the accounts that executed; ``unprotected`` is what the
    marketplace error streak and the operator must see."""
    failed = [ex.name for ex, res in zip(executors, results) if isinstance(res, Exception)]
    unprotected = [name for name, info in acct_state.items() if info.get("unprotected")]
    state.log_event("error" if failed or unprotected else "info",
                    line + (f"; failed: {', '.join(failed)}" if failed else "")
                    + (f"; UNPROTECTED (no stop): {', '.join(unprotected)}" if unprotected else ""))
    if failed:
        out["failed"] = failed
    if unprotected:
        out["unprotected"] = unprotected
    return out


async def _retire_extra_stops(ex: Any, info: dict[str, Any], tag: str) -> None:
    """Cancel the stops a repair left behind (``extra_stop_ids``: an old stop that
    would not cancel when a fresh one was placed). Whatever still refuses stays
    in the record — the next stop change tries again, and a close cancels the
    contract's orders anyway."""
    ids = [oid for oid in (info.get("extra_stop_ids") or []) if oid]
    if not ids:
        return
    results = await asyncio.gather(*(ex.cancel_order(oid) for oid in ids), return_exceptions=True)
    left = []
    for oid, r in zip(ids, results):
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, BaseException):
            left.append(oid)
            state.log_event("error", f"{tag}{ex.name}: extra stop {oid} still could not be cancelled: {r} — cancel it by hand")
    if left:
        info["extra_stop_ids"] = left
    else:
        info.pop("extra_stop_ids", None)


def _untrack_if_flat(active_map: dict[str, Any], key: str) -> bool:
    """Drop a trade record whose every account is flat with no stop left (the
    last target filled and the stop was retired): a later close_all would
    otherwise liquidate a flat contract and report an error. Returns True when
    the record was dropped."""
    with _lock:
        cur = active_map.get(key)
        accounts = (cur or {}).get("accounts") or {}
        if not cur or not accounts:
            return False
        if all(int(a.get("qty") or 0) == 0 and not a.get("sl_order_id") and not a.get("extra_stop_ids") for a in accounts.values()):
            active_map.pop(key, None)
            return True
    return False


STOP_PENALTY_WAIT_S = 30.0        # the longest a protective stop waits for a 429 penalty before its retry
RESOLUTION_PENALTY_WAIT_S = 120.0  # a close that protects a live position waits a 429 penalty out (nothing else can be sent meanwhile)


async def _penalty_wait(exc: BaseException, cap: float = RESOLUTION_PENALTY_WAIT_S) -> bool:
    """Wait a 429 penalty out (bounded by ``cap``) and say so; False for any
    other error. Used only on resolution paths — a live position with no stop,
    a kill switch — where waiting beats giving up: the broker refuses every
    request until the penalty ends, so nothing is lost by waiting."""
    if not isinstance(exc, RateLimited):
        return False
    await asyncio.sleep(min(float(getattr(exc, "retry_after", 0) or 0) + 0.2, cap))
    return True


async def _const(value: Any) -> Any:
    """An awaitable that yields ``value`` — the empty half of a gather."""
    return value


class _Unprotected(Exception):
    """Internal: the close after a failed stop failed too — the position is live
    and unprotected; the caller keeps tracking it (returns ``None`` for the stop)."""


class StopFailed(TradovateError):
    """The protective stop could not be placed twice and the entry was closed
    again at market: the account holds nothing of this trade (policy: never
    leave an entry live without its stop)."""


async def _place_stop_with_retry(ex: Any, *, symbol: str, action: str, qty: int, order_type: str,
                                 stop_price: float, tag: str, what: str = "stop",
                                 cancel_ids: list[int] | None = None,
                                 resting_entry_id: int | None = None,
                                 first_error: Exception | None = None) -> dict[str, Any] | None:
    """Place a protective stop; one retry on a *confirmed* failure. When it
    still fails the entry is **closed again**: the trade's own orders
    (``cancel_ids``, the bracket's targets) are cancelled — every working order
    of the contract when the stop's outcome is unknown, since a stop that did
    reach the broker would open a reverse trade on a flat account — then
    ``qty`` is flattened at market, the operator is alerted and ``StopFailed``
    is raised so the caller drops the account from the trade. Only when that
    close fails too does the position stay live: reported at error level and
    on every alert channel.
    An **unknown** outcome (the answer was lost, not the order) is never
    retried: a second attempt could put two full-size stops on the position,
    so it goes straight to the cancel-and-close resolution.
    ``resting_entry_id`` names a limit entry that may not have filled: it is
    cancelled and only what the broker shows as filled is closed (a blind market
    order on an unfilled limit would open the opposite position).
    ``first_error`` is the failure of an attempt the caller already made (the
    bracket sends the stop's first attempt alongside its targets): it counts as
    attempt one, so a confirmed rejection gets the single retry and an unknown
    outcome goes straight to the resolution.
    Returns the stop order."""
    from ..tradovate import RateLimited
    last: Exception | None = None

    async def _penalty(exc: Exception) -> None:
        # a 429 penalty set by some poll must not leave the entry naked: the
        # stop is protective, not latency-critical, so it waits the penalty out
        wait = min(float(getattr(exc, "retry_after", 0) or 0) + 0.2, STOP_PENALTY_WAIT_S) if isinstance(exc, RateLimited) else 0.5
        await asyncio.sleep(wait)

    attempts = (1, 2)
    if first_error is not None:
        last = first_error
        attempts = () if isinstance(first_error, OrderOutcomeUnknown) else (2,)
        if attempts:
            await _penalty(first_error)
    for attempt in attempts:
        try:
            return await ex.place_order(symbol=symbol, action=action, qty=qty, order_type=order_type, stop_price=stop_price)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if isinstance(exc, OrderOutcomeUnknown):
                break                                  # may be working: resolve, never re-place
            if attempt == 1:
                await _penalty(exc)
    how = "outcome unknown, not retried" if isinstance(last, OrderOutcomeUnknown) else "twice"
    state.log_event("error", f"{tag}{what} for {ex.name} on {symbol} FAILED ({how}: {last}) — closing the entry again")
    with broker.urgent():                                   # the reads of the resolution never queue behind polls
        try:
            msg = await _close_after_failed_stop(ex, symbol=symbol, action=action, qty=qty, tag=tag, what=what, last=last,
                                                 cancel_ids=cancel_ids, resting_entry_id=resting_entry_id)
        except _Unprotected:
            return None
    raise StopFailed(msg)


async def _close_after_failed_stop(ex: Any, *, symbol: str, action: str, qty: int, tag: str, what: str, last: Exception | None,
                                   cancel_ids: list[int] | None, resting_entry_id: int | None) -> str:
    """The resolution behind ``_place_stop_with_retry``: cancel, close, alert.
    Returns the ``StopFailed`` message; raises ``_Unprotected`` when the close
    failed too (the caller keeps the account tracked and reports it). A 429
    penalty is waited out here — the position is live and nothing can be
    sent until the penalty ends, so giving up would leave it unprotected."""
    errors: list[str] = []
    if isinstance(last, OrderOutcomeUnknown):
        # the stop may be working after all: it must not survive on a flat account
        await _cancel_working(ex, tag, errors, contract=symbol, wait_penalty=True)
    else:
        ids = [o for o in [*(cancel_ids or []), resting_entry_id] if o]
        results = await asyncio.gather(*(ex.cancel_order(oid) for oid in ids), return_exceptions=True)
        errors += [f"cancel {oid}: {r}" for oid, r in zip(ids, results) if isinstance(r, Exception)]
    close_qty = qty
    if resting_entry_id:
        # a limit entry: close only the part that filled (sign must be the entry's)
        try:
            rows = await ex.positions()
            net = sum(int(p.get("netPos") or 0) for p in rows or [] if str(p.get("symbol") or "") == symbol)
        except Exception as exc:  # noqa: BLE001 - unknown fill state → treat as filled (the safer error)
            state.log_event("warn", f"{tag}{ex.name}: position on {symbol} could not be read after the failed {what} ({exc}) — closing the full entry")
            net = qty if action == "Sell" else -qty
        filled = net if action == "Sell" else -net                 # exit Sell means the entry went long
        close_qty = max(0, min(qty, filled))
    if close_qty <= 0:
        state.log_event("error", f"{tag}{ex.name}: entry on {symbol} cancelled — the {what} could not be placed and nothing had filled")
        events.emit("execution.problem", title=f"Entry cancelled on {ex.name}", message=f"{symbol}: the {what} could not be placed ({last}); the unfilled entry was cancelled.")
        return f"{what} could not be placed ({last}); unfilled entry cancelled"
    try:
        try:
            await ex.place_order(symbol=symbol, action=action, qty=close_qty, order_type="Market")
        except RateLimited as exc:
            state.log_event("warn", f"{tag}{ex.name}: the close after the failed {what} is refused by a 429 penalty ({exc}) — waiting it out")
            await _penalty_wait(exc)
            await ex.place_order(symbol=symbol, action=action, qty=close_qty, order_type="Market")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        state.log_event("error", f"{tag}{ex.name}: the close after the failed {what} FAILED too ({exc}) — position on {symbol} is unprotected")
        events.emit("execution.problem", title=f"Unprotected position on {ex.name}",
                    message=f"{symbol}: the {what} could not be placed ({last}) and the position could not be closed ({exc}). "
                            "Set a stop by hand or close the position.")
        raise _Unprotected() from exc
    left = f"; {len(errors)} order(s) could not be cancelled: {'; '.join(errors)[:200]} — cancel them by hand" if errors else ""
    state.log_event("error", f"{tag}{ex.name}: {close_qty} × {symbol} closed again at market — the {what} could not be placed{left}")
    events.emit("execution.problem", title=f"Entry closed again on {ex.name}",
                message=f"{symbol}: the {what} could not be placed ({last}); the {close_qty}-lot entry was closed at market{left}.")
    return f"{what} could not be placed ({last}); entry closed again at market{left}"


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
    with broker.urgent():                                   # the order list is read on the close's own lane
        cancelled = await _cancel_working(ex, tag, errors, contract=contract)
        await ex.liquidate_position(contract)
        if errors:
            retry_errors: list[str] = []
            cancelled += await _cancel_working(ex, tag, retry_errors, contract=contract)
        else:
            retry_errors = []
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
        with broker.urgent():
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


async def _order_still_working(ex: Any, order_id: Any) -> bool | None:
    """Broker truth after an uncertain cancel: True / False when the working
    orders could be read, None when not even that. Reads only — no mutation is
    ever replayed on the strength of this answer."""
    try:
        rows = await ex.working_orders()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - unreadable is "unknown", not "gone"
        return None
    try:
        want = int(order_id)
    except (TypeError, ValueError):
        return None
    for o in rows or []:
        try:
            if int(o.get("id") or 0) == want:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _net_of(row: dict[str, Any]) -> int | None:
    """A position row's net quantity; ``None`` when it cannot be read (never
    guessed as flat, never invented as a quantity)."""
    try:
        return int(float(row.get("netPos") or 0))
    except (TypeError, ValueError):
        return None


async def _flatten_account(ex: Any, tag: str = "") -> tuple[int, int, list[str]]:
    """Emergency flatten of one account, shared by the SOS kill switch, the risk
    guard and the automations: cancel every working order first (stops and
    targets must not re-fill), then liquidate every open position — each symbol
    once, all liquidations in flight together. A liquidation whose call raised
    is judged by one broker re-read: a lost answer or a rejection that raced a
    fill with the position flat is a success, not an error. No settle waits and
    no second cancel pass — the kill switch is measured in milliseconds, and a
    blanket cancel after the liquidation could cancel the liquidation itself.
    The reads take the order lane (``broker.urgent``); a running 429 penalty is
    waited out once rather than reported as "nothing to flatten".
    Never raises for broker errors. Returns ``(cancelled, flattened, errors)``."""
    with broker.urgent():
        errors: list[str] = []
        try:
            cancelled = await _cancel_working(ex, tag, errors, wait_penalty=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the positions still get flattened
            cancelled = 0
            errors.append(f"cancel orders: {type(exc).__name__}: {exc}")
        positions: list[dict[str, Any]] = []
        for attempt in (1, 2):
            try:
                positions = await ex.positions() or []
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt == 1 and await _penalty_wait(exc):
                    continue                                # the penalty is over: one more read
                errors.append(f"list positions: {exc}")
                positions = []
        symbols = list(dict.fromkeys(str(p.get("symbol")) for p in positions if p.get("symbol")))
        if not symbols:
            return cancelled, 0, errors
        results = await asyncio.gather(*(ex.liquidate_position(sym) for sym in symbols), return_exceptions=True)
        flattened = 0
        failed: dict[str, BaseException] = {}
        for sym, r in zip(symbols, results):
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, BaseException):
                failed[sym] = r
            else:
                flattened += 1
        if not failed:
            return cancelled, flattened, errors
        # broker truth for the failed calls: one read, no wait
        residual: dict[str, int] | None
        unreadable: set[str] = set()
        try:
            residual = {}
            for p in await ex.positions() or []:
                sym = str(p.get("symbol") or "")
                if not sym:
                    continue
                net = _net_of(p)
                if net is None:
                    unreadable.add(sym)                     # a row we cannot read is not "flat"
                else:
                    residual[sym] = residual.get(sym, 0) + net
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            residual = None
        for sym, r in failed.items():
            if residual is not None and sym not in unreadable and residual.get(sym, 0) == 0:
                flattened += 1
                state.log_event("warn", f"{tag}{ex.name}: liquidation of {sym} raised {type(r).__name__} ({r}) but the broker reports the position flat")
                continue
            where = (f"broker still reports {residual[sym]:+d}" if residual is not None and sym in residual and sym not in unreadable
                     else "position could not be re-read")
            if isinstance(r, OrderOutcomeUnknown):
                errors.append(f"flatten {sym}: outcome unknown ({r}) — {where}; not re-sent, check the account")
            elif isinstance(r, TradovateError):
                errors.append(f"flatten {sym}: {r} — {where}")
            else:
                errors.append(f"flatten {sym}: {type(r).__name__}: {r} — {where}")
        return cancelled, flattened, errors


async def _cancel_working(ex: Any, tag: str, errors: list[str] | None = None,
                          contract: str | None = None, *, wait_penalty: bool = False) -> int:
    """Cancel working orders on one account with all cancels in flight at once.
    With ``contract`` only that contract's orders are cancelled (a symbol-scoped
    close); without it every working order goes, as the SOS flatten-all needs.
    ``wait_penalty`` (resolution paths) waits a 429 penalty on the listing read
    out once instead of reporting "nothing cancelled".
    Returns how many were cancelled. Tradovate rejections are collected
    (``errors``) or logged per order; anything else propagates, as in v4."""
    orders: list[dict[str, Any]] = []
    cid_task = asyncio.ensure_future(_contract_id_or_zero(ex, contract)) if contract else None   # alongside the order list
    listed = False
    try:
        for attempt in (1, 2):
            try:
                orders = await ex.working_orders()
                listed = True
                break
            except TradovateError as exc:
                if wait_penalty and attempt == 1 and await _penalty_wait(exc):
                    continue
                if errors is not None:
                    errors.append(f"list orders: {exc}")
                else:
                    state.log_event("error", f"{tag}Could not list working orders for {ex.name}: {exc} — nothing cancelled")
                return 0
    finally:
        if not listed and cid_task is not None and not cid_task.done():
            cid_task.cancel()                                # the list failed: the id is not needed
    if contract:
        orders = await _orders_for_contract(ex, orders, contract, tag, cid=await cid_task)
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

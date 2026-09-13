"""Translate incoming TradingView webhook signals into Tradovate orders.

Every signal arrives through a specific **webhook** (see ``config.py`` — each
webhook has its own secret URL, its own routed trade accounts + qty
multipliers, and a ``strategy`` that decides how the payload is executed:

* ``"simple"``  -> ``action: "buy" | "sell"`` places a single Market (or Limit)
  order sized by ``qty`` in the payload (falling back to the webhook's
  ``default_qty``), scaled per account by that account's multiplier. No TP/SL
  orders — just the execution. ``close_all`` flattens the tracked position.
* ``"bracket"`` -> the original TP/SL flow: ``buy``/``sell`` opens a market
  entry (webhook's ``default_qty`` contracts) + a TP limit order per
  ``tp1``/``tp2``/``tp3`` present (webhook's ``tp_qty`` contracts each) + a
  protective stop (``sl``) covering the full position. ``close_all`` cancels
  working orders and flattens; ``move_sl`` moves the tracked stop;
  ``trail_active`` resizes the stop to the remaining position.
* ``"ts_hunter"`` -> the TS-Hunter contract (``contract_version:
  at_execution_command_v5``): a payload with ``event: "signal"`` opens a
  market entry sized from ``risk.value`` contracts with a protective stop at
  ``sl.value``; ``event: "management"`` then drives it, correlated by
  ``trade_id`` (not symbol, so several concurrent trades on the same symbol
  never collide) — ``action: "partial_close_percent"`` market-closes
  ``percent``% of whatever remains (TP1/TP2/TP3 each shave off a slice,
  leaving a runner), resizing the stop to match the new remaining quantity
  each time; ``action: "full_close"`` cancels working orders and liquidates
  whatever remains, regardless of tracked quantity.

This module is the entry point: validation, routing to the strategy handlers
in :mod:`app.engine`, per-trade serialisation and active-trade tracking. The
same logic powers the **simulator**: passing ``simulate=True`` routes orders to
an in-memory executor (a synthetic bracket webhook + account) and uses a
separate trade-tracking map, so you can rehearse a full scenario without
credentials, risk, or a configured webhook.
"""
from __future__ import annotations

import asyncio
import logging
import random
import hmac
import threading
import time
from typing import Any

from . import config, context, events, news, state, trade_window
from .engine import bracket, manage, simple, ts_hunter
from .engine.common import (  # noqa: F401 - re-exported for callers/tests
    SignalError,
    _cancel_working,
    _flatten_account,
    _lock,
    _resolve_symbol,
    _trade_key,
)
from .simulator import client_for as _sim_for, sim_client  # noqa: F401 - sim_client re-exported for tests
from .tradovate import AccountExecutor, TradovateError, manager

# Per-trade async locks: serialise signals that touch the SAME position so two
# near-simultaneous events (e.g. two TP partial-closes) can't race on the shared
# active-trade state — otherwise both read the same "remaining qty" and one
# overwrites the other, so only one TP effectively executes. Signals for
# different trades/symbols still run in parallel.
_trade_locks: dict[str, asyncio.Lock] = {}
_trade_locks_guard = threading.Lock()


def _trade_lock(key: str) -> asyncio.Lock:
    with _trade_locks_guard:
        lk = _trade_locks.get(key)
        if lk is None:
            lk = _trade_locks[key] = asyncio.Lock()
        return lk


def _release_trade_lock(key: str) -> None:
    """Drop a trade's lock once its position is closed (v4 kept every lock for
    the life of the process — unbounded for TS-Hunter's per-trade ids). Only
    when nobody holds or awaits it: a late signal already queued on the Lock
    keeps using that object, so a second Lock must never appear beside it."""
    with _trade_locks_guard:
        lk = _trade_locks.get(key)
        if lk is not None and not lk.locked() and not getattr(lk, "_waiters", None):
            _trade_locks.pop(key, None)


# Active-trade records are isolated per area (user workspace). Each maps
# "<webhook_id>:<root>" (simple/bracket) or "<trade_id>" (TS-Hunter) -> trade
# record, so management signals can find the stop-loss order to modify. Reset
# when the position is closed. Live and simulated trades are tracked separately.
_active: dict[int, dict[str, dict[str, Any]]] = {}
_sim_active: dict[int, dict[str, dict[str, Any]]] = {}


ACTIVE_TTL_S = 14 * 24 * 3600     # a record whose position closed without a signal (stop hit) is forgotten after this
_sweep_n = 0


def _map_for(simulate: bool) -> dict[str, dict[str, Any]]:
    """The active-trade map for the current area (live or simulated)."""
    global _sweep_n
    reg = _sim_active if simulate else _active
    aid = context.get_area()
    with _lock:
        m = reg.get(aid)
        if m is None:
            m = reg[aid] = {}
        _sweep_n += 1
        if _sweep_n % 200 == 0 and len(m) > 50:
            cutoff = time.time() - ACTIVE_TTL_S
            for key in [k for k, rec in m.items() if 0 < float(rec.get("ts") or 0) < cutoff]:
                m.pop(key, None)
        return m


def _synthetic_bracket_webhook(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "sim", "name": "Simulator", "strategy": "bracket",
        "default_qty": s.get("default_qty", 3), "tp_qty": s.get("tp_qty", 1),
        "accounts": [],
    }


def _webhook_executors(webhook: dict[str, Any]) -> list[Any]:
    """Executors for a webhook's enabled (login, trade account) selections."""
    out = []
    for a in webhook.get("accounts") or []:
        if not a.get("enabled"):
            continue
        ex = manager().executor_for(
            a.get("token_idx"), a.get("spec"), a.get("qty_multiplier", 1), sizing=a.get("sizing"), lid=a.get("lid")
        )
        if ex is None:
            state.log_event("warn", f"Webhook '{webhook.get('name')}': account '{a.get('spec')}' not found (deleted login/account?)")
            continue
        out.append(ex)
    return out


# ------------------------------------------------------------- acceptance
log = logging.getLogger("signals")
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro: Any) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_done)
    return task


def _bg_done(task: asyncio.Task) -> None:
    _bg_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("background signal task failed: %r", task.exception())


def passphrase_ok(payload: dict[str, Any], settings: dict[str, Any] | None = None) -> bool:
    s = settings if settings is not None else config.load_settings()
    want = str(s.get("webhook_passphrase") or "")
    if not want:
        return True
    given = str(payload.get("passphrase") or "")
    return hmac.compare_digest(given.encode(), want.encode())


def accept(payload: dict[str, Any], webhook: dict[str, Any], *, forward: bool = True) -> None:
    name = webhook.get("name", "")
    s = config.load_settings()
    state.log_signal(payload, result="received", webhook=name, webhook_id=str(webhook.get("id") or ""))
    if not passphrase_ok(payload, s):
        state.log_event("error", "Signal rejected: invalid passphrase", payload=payload)
        state.log_signal(payload, result="error: Invalid passphrase", webhook=name, webhook_id=str(webhook.get("id") or ""))
        events.emit("signal.failed", webhook=name or "?", reason="Invalid passphrase", settings=s)
        return
    accepted = time.perf_counter()
    _spawn(process_background(payload, webhook, settings=s, accepted_at=accepted))
    if forward:
        forward_to_subscribers(payload, webhook, accepted_at=accepted)


def _publisher_entry(payload: dict[str, Any], webhook: dict[str, Any]) -> bool:
    """Whether this payload creates new exposure under the publisher strategy."""
    if webhook.get("strategy") == "ts_hunter":
        return str(payload.get("event") or "").lower().strip() == "signal"
    return str(payload.get("action") or "").lower().strip() in ("buy", "sell")


def forward_to_subscribers(payload: dict[str, Any], webhook: dict[str, Any],
                           publisher_area: int | None = None, accepted_at: float | None = None) -> int:
    """Fan out only to subscriptions authorised by the publisher *now*.

    Persisted subscription rows are not authorization leases: selected-user ACL
    changes take effect before the next financial action. The publisher's own
    entry window is also checked in the publisher workspace before fan-out;
    subscriber controls remain an additional gate in the subscriber workspace.
    """
    from . import db, marketplace

    sh = marketplace.sharing_of(webhook)
    if not sh["enabled"]:
        return 0
    if sh.get("paused"):
        state.log_event("info", f"[{webhook.get('name', '?')}] not forwarded: sharing is paused by the publisher")
        return 0
    aid = publisher_area if publisher_area is not None else context.get_area()

    if _publisher_entry(payload, webhook):
        publisher_settings = config.load_settings(area_id=aid)
        opened, why = trade_window.is_open(
            webhook.get("trade_window"),
            default_tz=str(publisher_settings.get("journal_timezone") or ""),
        )
        if not opened:
            state.log_event("info", f"[{webhook.get('name', '?')}] not forwarded: publisher trading window closed — {why}")
            return 0

    subs = db.active_subscriptions(aid, webhook.get("id", ""))
    random.shuffle(subs)
    shared = {k: v for k, v in payload.items() if not (isinstance(k, str) and "passphrase" in k.lower())}
    dispatched = 0
    revoked = 0
    for sub in subs:
        if not marketplace.subscription_allowed(webhook, sub):
            revoked += 1
            continue
        view = marketplace.subscription_view(webhook, sub, aid)
        with context.use_area(sub["area_id"]):
            state.log_signal(dict(shared), result="received", webhook=view.get("name", ""), webhook_id=str(view.get("id") or ""))
            _spawn(process_background(dict(shared), view, trusted=True, accepted_at=accepted_at))
        dispatched += 1
    if revoked:
        state.log_event("warn", f"[{webhook.get('name', '?')}] skipped {revoked} marketplace subscription(s) no longer authorised")
    if dispatched:
        state.log_event("info", f"[{webhook.get('name', '?')}] forwarded to {dispatched} subscriber(s)")
    return dispatched


async def process_background(payload: dict[str, Any], webhook: dict[str, Any], *, trusted: bool = False,
                             settings: dict[str, Any] | None = None, accepted_at: float | None = None) -> None:
    name = webhook.get("name", "?")
    wid = str(webhook.get("id") or "")
    started = accepted_at if accepted_at is not None else time.perf_counter()
    ms = lambda: int((time.perf_counter() - started) * 1000)  # noqa: E731
    try:
        result = await process(payload, webhook, trusted=trusted, settings=settings)
        state.log_signal(payload, result=result.get("status", "ok"), webhook=name, webhook_id=wid, latency_ms=ms())
        events.emit("signal.done", webhook=name, status=result.get("status", "ok"), reason=result.get("reason", ""), action=result.get("action", ""),
                    seconds=time.perf_counter() - started)
        await _after_subscription_outcome(webhook, None, result)
    except (SignalError, TradovateError) as exc:
        state.log_event("error", f"Signal error: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}", webhook=name, webhook_id=wid, latency_ms=ms())
        events.emit("signal.done", webhook=name, status="error", reason=str(exc)[:200], action="", seconds=time.perf_counter() - started)
        await events.emit_async("signal.failed", webhook=name, reason=str(exc), settings=settings)
        await _after_subscription_error(webhook, exc)
    except Exception as exc:  # noqa: BLE001
        state.log_event("error", f"Signal failed: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}", webhook=name, webhook_id=wid, latency_ms=ms())
        events.emit("signal.done", webhook=name, status="error", reason=str(exc)[:200], action="", seconds=time.perf_counter() - started)
        await events.emit_async("signal.failed", webhook=name, reason=str(exc), settings=settings)
        await _after_subscription_error(webhook, exc)


_sub_errors: dict[tuple[int, int], int] = {}


async def _after_subscription_error(webhook: dict[str, Any], exc: Exception) -> None:
    await _after_subscription_outcome(webhook, exc, None)


async def _after_subscription_outcome(webhook: dict[str, Any], exc: Exception | None, result: dict[str, Any] | None) -> None:
    try:
        coro = _note_subscription_outcome(webhook, exc, result)
        if coro is not None:
            await coro
    except Exception as err:  # noqa: BLE001
        state.log_event("warn", f"subscription bookkeeping failed: {err}")


def _note_subscription_outcome(webhook: dict[str, Any], exc: Exception | None, result: dict[str, Any] | None = None) -> Any:
    sub = webhook.get("subscription") if isinstance(webhook.get("subscription"), dict) else None
    if not sub or not sub.get("id"):
        return None
    limit = int((webhook.get("controls") or {}).get("pause_after_errors") or 0)
    key = (context.get_area(), int(sub["id"]))
    if not limit:
        _sub_errors.pop(key, None)
        return None
    why = ""
    if exc is not None:
        why = str(exc)[:160]
    elif result is not None:
        if result.get("status") == "skipped":
            return None
        if result.get("status") == "error":
            why = str(result.get("reason") or result.get("detail") or "error")[:160]
        elif str(result.get("action") or "") in ("buy", "sell", "signal") and isinstance(result.get("accounts"), list) and not result["accounts"]:
            why = "no account executed the entry"
    if not why:
        _sub_errors.pop(key, None)
        return None
    n = _sub_errors.get(key, 0) + 1
    _sub_errors[key] = n
    if len(_sub_errors) > 5000:
        _sub_errors.pop(next(iter(_sub_errors)))
    if n < limit:
        return None
    from . import db
    _sub_errors.pop(key, None)
    db.update_subscription(int(sub["id"]), key[0], enabled=False)
    title = str(webhook.get("name") or sub.get("webhook_id") or "subscription")
    reason = f"{n} consecutive signal errors (last: {why})"
    state.log_event("warn", f"Subscription '{title}' switched off: {reason}")
    return events.emit_async("subscription.paused", title=title, reason=reason, subscription_id=int(sub["id"]))


# ------------------------------------------------------------ entry point
async def process(
    payload: dict[str, Any], webhook: dict[str, Any] | None = None, *,
    simulate: bool = False, trusted: bool = False, settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    s = settings if settings is not None else config.load_settings()
    active_map = _map_for(simulate)

    if webhook is None:
        if not simulate:
            raise SignalError("No webhook context for this signal")
        webhook = _synthetic_bracket_webhook(s)

    if not simulate and not trusted:
        if not passphrase_ok(payload, s):
            raise SignalError("Invalid passphrase")

    if webhook.get("strategy") == "ts_hunter":
        return await _process_ts_hunter(payload, webhook, active_map, simulate, s)

    action = str(payload.get("action", "")).lower().strip()
    tv_symbol = str(payload.get("symbol", "")).strip()
    if not action or not tv_symbol:
        raise SignalError("Payload missing 'action' or 'symbol'")

    target, root, allowed = _resolve_symbol(s, tv_symbol)
    if not allowed:
        raise SignalError(f"Symbol '{tv_symbol}' not mapped / not in allowed list")

    if not simulate and not s.get("trading_enabled"):
        state.log_event("warn", f"Trading disabled — signal '{action}' for {root} not executed")
        return {"status": "skipped", "reason": "trading_disabled", "action": action}
    if webhook.get("subscription"):
        from . import marketplace
        ok, why, detail = marketplace.subscription_gate(webhook, root, action, area_id=context.get_area())
        if not ok:
            state.log_event("warn", f"Subscription '{webhook.get('name')}': '{action}' for {root} not executed — {detail}")
            return {"status": "skipped", "reason": why, "action": action, "detail": detail}
    if not simulate and action in ("buy", "sell"):
        lock = news.active_lock(settings=s)
        if lock:
            state.log_event("warn", f"News lock ({lock['title']}) — entry '{action}' for {root} not executed; closes and stop moves still run")
            return {"status": "skipped", "reason": "news_lock", "action": action, "event": lock["title"]}
        opened, why = trade_window.is_open(webhook.get("trade_window"), default_tz=str(s.get("journal_timezone") or ""))
        if not opened:
            state.log_event("warn", f"Webhook '{webhook.get('name')}': entry '{action}' for {root} not executed — {why}; closes and stop moves still run")
            return {"status": "skipped", "reason": "trade_window", "action": action, "detail": why}

    executors = [_sim_for(context.get_area())] if simulate else _webhook_executors(webhook)
    if not executors:
        state.log_event("warn", f"No enabled accounts on webhook '{webhook.get('name')}' — signal '{action}' ignored")
        return {"status": "skipped", "reason": "no_enabled_accounts", "action": action}

    tag = "[SIM] " if simulate else ""
    strategy = webhook.get("strategy", "simple")

    async def run() -> dict[str, Any]:
        if action in ("buy", "sell"):
            if not simulate and not config.setting("trading_enabled"):
                state.log_event("warn", f"Trading disabled — signal '{action}' for {root} not executed")
                return {"status": "skipped", "reason": "trading_disabled", "action": action}
            if strategy == "simple":
                return await simple.handle_entry(payload, action, root, target, executors, active_map, tag, webhook, settings=s)
            return await bracket.handle_entry(payload, action, root, target, executors, active_map, tag, webhook, settings=s)
        if action == "close_all":
            return await manage.handle_close_all(root, target, executors, active_map, tag, webhook)
        if action == "set_sl_tp":
            return await manage.handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook, settings=s)
        if action == "move_sl":
            if strategy == "simple":
                state.log_event("info", f"{tag}move_sl ignored for {root} — 'simple' strategy has no bracket to move")
                return {"status": "skipped", "reason": "move_sl_unsupported_simple", "action": action}
            return await bracket.handle_move_sl(payload, root, executors, active_map, tag, webhook, settings=s)
        if action == "trail_active":
            if strategy == "simple":
                state.log_event("info", f"{tag}Trailing active for {root} (no-op on 'simple' strategy)")
                return {"status": "ok", "action": action, "note": "acknowledged", "simulated": simulate}
            return await bracket.handle_trail_active(payload, root, executors, active_map, tag, webhook)
        raise SignalError(f"Unknown action '{action}'")

    lock_key = f"{context.get_area()}:{'sim' if simulate else 'live'}:{webhook['id']}:{root}"
    async with _trade_lock(lock_key):
        result = await run()
    if action == "close_all" or _trade_key(webhook["id"], root) not in active_map:
        _release_trade_lock(lock_key)
    return result


async def _process_ts_hunter(payload, webhook, active_map, simulate, s):
    tag = "[SIM] " if simulate else ""

    event = str(payload.get("event", "")).lower().strip()
    trade_id = str(payload.get("trade_id", "")).strip()
    if not trade_id:
        raise SignalError("Payload missing 'trade_id'")

    tv_symbol = str(payload.get("symbol") or payload.get("pair") or "").strip()
    if not tv_symbol:
        raise SignalError("Payload missing 'symbol'")

    target, root, allowed = _resolve_symbol(s, tv_symbol)
    if not allowed:
        raise SignalError(f"Symbol '{tv_symbol}' not mapped / not in allowed list")

    side = str(payload.get("side") or payload.get("direction") or "").lower().strip()
    if event == "signal" and side not in ("buy", "sell"):
        raise SignalError(f"Invalid/missing side '{side}'")

    if not simulate and not s.get("trading_enabled"):
        state.log_event("warn", f"Trading disabled — TS-Hunter signal for {root} (trade {trade_id}) not executed")
        return {"status": "skipped", "reason": "trading_disabled"}
    if webhook.get("subscription"):
        from . import marketplace
        ok, why, detail = marketplace.subscription_gate(webhook, root, side if event == "signal" else event, area_id=context.get_area())
        if not ok:
            state.log_event("warn", f"Subscription '{webhook.get('name')}': TS-Hunter {event} for {root} not executed — {detail}")
            return {"status": "skipped", "reason": why, "action": event, "detail": detail}
    if not simulate and event == "signal":
        lock = news.active_lock(settings=s)
        if lock:
            state.log_event("warn", f"News lock ({lock['title']}) — TS-Hunter entry for {root} (trade {trade_id}) not executed")
            return {"status": "skipped", "reason": "news_lock", "event": lock["title"]}
        opened, why = trade_window.is_open(webhook.get("trade_window"), default_tz=str(s.get("journal_timezone") or ""))
        if not opened:
            state.log_event("warn", f"Webhook '{webhook.get('name')}': TS-Hunter entry for {root} (trade {trade_id}) not executed — {why}")
            return {"status": "skipped", "reason": "trade_window", "detail": why}

    executors = [_sim_for(context.get_area())] if simulate else _webhook_executors(webhook)
    if not executors:
        state.log_event("warn", f"No enabled accounts on webhook '{webhook.get('name')}' — TS-Hunter signal ignored")
        return {"status": "skipped", "reason": "no_enabled_accounts"}

    mgmt_action = str(payload.get("action", "")).lower().strip() if event == "management" else ""

    async def run() -> dict[str, Any]:
        if event == "signal":
            if not simulate and not config.setting("trading_enabled"):
                state.log_event("warn", f"Trading disabled — TS-Hunter signal for {root} (trade {trade_id}) not executed")
                return {"status": "skipped", "reason": "trading_disabled"}
            return await ts_hunter.handle_entry(payload, side, root, target, trade_id, executors, active_map, tag, webhook, settings=s)
        if event == "management":
            if mgmt_action == "partial_close_percent":
                return await ts_hunter.handle_partial_close(payload, trade_id, executors, active_map, tag)
            if mgmt_action == "full_close":
                return await ts_hunter.handle_full_close(payload, trade_id, target, executors, active_map, tag)
            raise SignalError(f"Unknown TS-Hunter management action '{mgmt_action}'")
        raise SignalError(f"Unknown TS-Hunter event '{event}'")

    lock_key = f"{context.get_area()}:{'sim' if simulate else 'live'}:ts:{trade_id}"
    async with _trade_lock(lock_key):
        result = await run()
    if mgmt_action == "full_close" or trade_id not in active_map:
        _release_trade_lock(lock_key)
    return result


# --------------------------------------------------------------- flatten
async def flatten_all() -> dict[str, Any]:
    """Emergency kill-switch reconciled against broker truth.

    Every observed position is submitted for liquidation at most once. A lost
    broker answer is never blindly replayed; a position counts as flattened only
    after a broker position read confirms it is flat.
    """
    mgr = manager()
    mgr.reload()
    executors: list[AccountExecutor] = []
    for s in mgr.all():
        if not s.enabled:
            continue
        for a in s.accounts:
            executors.append(AccountExecutor(s, a))

    if not executors:
        state.log_event("warn", "🆘 SOS flatten-all: no trade accounts found")
        return {"status": "ok", "accounts": 0, "cancelled": 0, "flattened": 0, "errors": []}

    results = await asyncio.gather(*(_flatten_account(ex, "") for ex in executors), return_exceptions=True)

    cancelled = flattened = 0
    all_errors: list[str] = []
    for ex, result in zip(executors, results):
        if isinstance(result, Exception):
            all_errors.append(f"{ex.name}: {result}")
            state.log_event("error", f"🆘 SOS flatten failed for {ex.name}: {result}")
            continue
        c, f, errs = result
        cancelled += c
        flattened += f
        all_errors += [f"{ex.name}: {e}" for e in errs]

    state.log_event(
        "error" if all_errors else "warn",
        f"🆘 SOS flatten-all: {flattened} broker-confirmed-flat position(s), {cancelled} order(s) "
        f"cancelled across {len(executors)} account(s)"
        + (f"; {len(all_errors)} unresolved/error(s)" if all_errors else ""),
    )
    return {"status": "error" if all_errors else "ok", "accounts": len(executors),
            "cancelled": cancelled, "flattened": flattened, "errors": all_errors}


# ------------------------------------------------------------- inspection
def active_trades(simulate: bool = False) -> dict[str, Any]:
    src = _map_for(simulate)
    with _lock:
        return {k: dict(v) for k, v in src.items()}


def active_trades_for(area_id: int) -> dict[str, Any]:
    """The live active-trade map of one area (metrics; no context switch)."""
    with _lock:
        return {k: dict(v) for k, v in (_active.get(area_id) or {}).items()}


def reset_simulation() -> None:
    _sim_for(context.get_area()).reset()
    _map_for(True).clear()

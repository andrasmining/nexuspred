"""Legacy in-process backend. Existing locks, risk guards and broker semantics stay here.

Never call this backend from HTTP routers. The service authorizes an explicit
actor, selects its workspace context and coordinates durable manual commands.
"""
from __future__ import annotations

from typing import Any

from .. import config, context, db, history, signals, state, tradovate
from ..engine.common import _base_root, _close_contract
from ..tradovate import TradovateError
from .contracts import AccountTarget, ClosePosition, ExecutionError, ManualOrder


def resolve_account(target: AccountTarget) -> Any:
    ex = tradovate.manager().executor_for(target.token_idx if target.token_idx is not None else -1,
                                         target.spec, 1, lid=target.lid)
    if ex is None:
        raise ExecutionError("not_found", f"Account '{target.spec}' not found or its login is disabled")
    return ex


def mapped_symbol(symbol: str) -> str:
    mapping = config.peek("symbol_map") or {}
    return mapping.get(symbol) or mapping.get(symbol.upper()) or symbol.upper()


def binding(command: ManualOrder, ex: Any, contract: str) -> dict[str, Any]:
    """Remember the resolved target, not credentials or the raw broker response."""
    session = getattr(ex, "session", None)
    return {"spec": command.account.spec, "lid": getattr(session, "lid", None) or command.account.lid,
            "account_id": getattr(ex, "id", None), "contract": contract,
            "broker": getattr(session, "kind", "tradovate") if session is not None else None,
            "environment": getattr(session, "environment", None)}


def audit_manual(command: ManualOrder, ex: Any, target: str) -> None:
    user = db.get_user(command.actor.user_id) or {}
    detail = f"{command.action} {command.quantity} {target} {command.order_type}"
    if command.price is not None:
        detail += f" @ {command.price}"
    if command.stop_price is not None:
        detail += f" stop {command.stop_price}"
    history.defer(db.log_action, command.actor.user_id, user.get("email", ""), "manual_order", ex.name, detail)


async def close_position(command: ClosePosition) -> dict[str, Any]:
    user = db.get_user(command.actor.user_id) or {}
    contract = command.symbol
    ex = resolve_account(command.account)
    # Hold the trade locks of every tracked trade on this account + contract so an
    # in-flight signal (entry, stop placement, close) cannot interleave with the close.
    live = signals._map_for(False)
    root = _base_root(contract.upper())
    with signals._lock:
        trades = {k: t for k, t in live.items() if (t.get("accounts") or {}).get(ex.name)
                  and str(((t.get("accounts") or {}).get(ex.name) or {}).get("contract") or t.get("contract") or "") == contract}
    keys = sorted(trades)
    # Match signals.process exactly: TS-Hunter locks by trade id; other
    # strategies use the full webhook id and the signal's (possibly aliased)
    # root, not necessarily the broker contract's root.
    lock_keys = [f"{context.get_area()}:live:ts:{t['trade_id']}" if t.get("trade_id") else
                 f"{context.get_area()}:live:{t.get('webhook_id') or k.rsplit(':', 1)[0]}:{t.get('root') or root}"
                 for k, t in trades.items()]
    # An entry that is still running holds its lock but is not tracked yet: it
    # only lands in the map once every leg is placed. Without this the close
    # would cancel and liquidate between the entry fill and its protective stop.
    lock_keys += signals.inflight_lock_keys(context.get_area(), root)
    lock_keys = lock_keys or [f"{context.get_area()}:live:manual:{root}"]
    locks = [signals._trade_lock(k) for k in sorted(set(lock_keys))]
    acquired = []
    try:
        for lk in locks:
            await lk.acquire()
            acquired.append(lk)
        try:
            cancelled = await _close_contract(ex, "", contract)          # cancel → liquidate → retry cancels; raises when orders remain
        except TradovateError as exc:
            raise ExecutionError("broker_rejected", f"{ex.name}: close {contract} failed — {exc}") from exc
        # the bridge stops managing that trade on this account (other accounts stay tracked)
        with signals._lock:
            for key in keys:
                trade = live.get(key)
                if not trade:
                    continue
                accounts = trade.get("accounts") or {}
                accounts.pop(ex.name, None)
                if not accounts:
                    live.pop(key, None)
    finally:
        # Cancellation while waiting for another trade must not strand the
        # locks already acquired and block its subsequent management signals.
        for lk in reversed(acquired):
            lk.release()
        for key in lock_keys:
            signals._release_trade_lock(key)
    state.log_event("info", f"Position closed from the dashboard: {contract} on {ex.name} ({cancelled} order(s) cancelled) by {user.get('email', '?')}")
    if user:
        db.log_action(user["id"], user["email"], "close_position", ex.name, f"{contract}, {cancelled} order(s) cancelled")
    return {"status": "ok", "contract": contract, "account": ex.name, "cancelled": cancelled, "errors": []}



async def flatten(user_id: int) -> dict[str, Any]:
    # Preserve partial errors: status=ok from this helper is not proof of flatness.
    result = await signals.flatten_all()
    user = db.get_user(user_id) or {}
    db.log_action(user_id, user.get("email", ""), "flatten_all", "",
                  f"{result.get('flattened', 0)} flattened, "
                  f"{result.get('cancelled', 0)} cancelled, "
                  f"{result.get('accounts', 0)} account(s)")
    return result

"""First execution boundary: local calls today, an explicit port for later.

Only manual orders are ledger-backed in this slice. Legacy signal/copy engines
keep their existing lifecycle. Close and flatten never depend on commercial
entitlement reads or successful command-ledger writes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from .. import config, context, state
from ..db import execution as ledger
from ..platform import commercial_policy, entitlements, workspaces
from ..platform.workspaces import Actor, WorkspaceAccessDenied
from ..tradovate import OrderOutcomeUnknown, TradovateError
from . import local
from .contracts import ClosePosition, ExecutionError, ExecutionResult, ExecutionService, ManualOrder, Outcome, RiskEffect, command_key


async def _authorize(actor: Actor, *, write: bool = True) -> None:
    try:
        await asyncio.to_thread(workspaces.authorize, actor, write=write)
    except WorkspaceAccessDenied as exc:
        raise ExecutionError("forbidden", str(exc)) from exc


async def _manual_allowed(command: ManualOrder) -> None:
    if not config.peek("trading_enabled"):
        raise ExecutionError("trading_disabled", "Trading is disabled — switch it on in the top bar first")
    try:
        grants = await asyncio.to_thread(entitlements.for_workspace, command.actor.workspace_id)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ExecutionError("unavailable", "Manual trading entitlement unavailable; no order sent") from exc
    # A manual buy/sell can add, reverse or leave a working entry. Never infer
    # risk reduction from the side, order type, or a client 'reduce_only' flag.
    if not commercial_policy.permits(RiskEffect.OPEN_RISK, grants):
        raise ExecutionError("entitlement_denied", "Manual trading is not enabled for this workspace")


async def _finish(command: ManualOrder, outcome: Outcome, *, response: dict[str, Any] | None = None,
                  error_code: str = "") -> bool:
    try:
        await asyncio.to_thread(ledger.finish, command.actor.workspace_id, command.command_id, outcome.value,
                                response=response, error_code=error_code)
        return True
    except (sqlite3.Error, OSError, ValueError, TypeError):
        state.log_event("error", f"Execution command {command.command_id}: outcome could not be persisted; "
                                 "check the broker before any new instruction")
        return False


def _replay(command: ManualOrder, row: dict[str, Any]) -> ExecutionResult:
    if row["request_hash"] != command.fingerprint():
        raise ExecutionError("conflict", "Idempotency-Key was already used for a different instruction",
                             command_id=command.command_id)
    if row["outcome"] == Outcome.ACCEPTED.value:
        return ExecutionResult(command.command_id, Outcome.ACCEPTED, json.loads(row["response_json"]), replayed=True)
    if row["outcome"] in (Outcome.CLAIMED.value, Outcome.DISPATCHING.value):
        raise ExecutionError("in_progress", "Command is already in progress; do not submit a new instruction",
                             command_id=command.command_id)
    if row["outcome"] == Outcome.REJECTED.value:
        code = row["error_code"] or "broker_rejected"
        raise ExecutionError(code, "Previous instruction was rejected; no order was retried",
                             command_id=command.command_id)
    raise ExecutionError("unknown", "Order outcome unknown — check the broker; this instruction will not be retried",
                         command_id=command.command_id)


class LocalExecutionService:
    async def place_manual_order(self, command: ManualOrder) -> ExecutionResult:
        await _authorize(command.actor)
        with context.use_area(command.actor.workspace_id):
            return await self._manual(command)

    async def _manual(self, command: ManualOrder) -> ExecutionResult:
        try:
            fresh, row = await asyncio.to_thread(ledger.claim, command.actor.workspace_id, command.command_id,
                                                command.actor.user_id, command.fingerprint(), command.intent_json())
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ExecutionError("unavailable", "Execution ledger unavailable; no order sent",
                                 command_id=command.command_id) from exc
        if not fresh:
            return _replay(command, row)

        dispatched = False
        try:
            await _manual_allowed(command)
            ex = local.resolve_account(command.account)
            target = local.mapped_symbol(command.symbol)
            contract = await ex.resolve_contract(target)
            if not isinstance(contract, str) or not contract.strip():
                raise ExecutionError("unavailable", "Broker contract could not be resolved; no order sent")
            # Reads can await a broker: re-check permissions before dispatch.
            await _authorize(command.actor)
            await _manual_allowed(command)
            local.audit_manual(command, ex, target)
            await asyncio.to_thread(ledger.dispatch, command.actor.workspace_id, command.command_id,
                                    local.binding(command, ex, contract))
            dispatched = True
            # The adapter's risk guard remains the last execution chokepoint.
            order = await ex.place_order(symbol=contract, action=command.action.capitalize(), qty=command.quantity,
                                         order_type=command.order_type, price=command.price, stop_price=command.stop_price)
            if not isinstance(order, dict):
                raise OrderOutcomeUnknown("Broker did not return an order acknowledgement")
            status = order.get("status", "submitted")
            if isinstance(status, str) and status.lower() in ("rejected", "error", "failed"):
                raise TradovateError("Broker rejected the manual order")
            oid = order.get("order_id")
            valid_id = (type(oid) is int and oid > 0) or (isinstance(oid, str) and 0 < len(oid) <= 128)
            if not valid_id or not isinstance(status, str) or not status or status.lower() == "unknown":
                raise OrderOutcomeUnknown("Broker did not return a definite order acknowledgement")
            payload = {"status": order.get("status", "submitted"), "order_id": order.get("order_id"), "contract": contract,
                       "account": ex.name, "action": command.action, "qty": command.quantity, "order_type": command.order_type}
        except asyncio.CancelledError:
            # Cancellation is not evidence of broker rejection, even when the
            # HTTP caller disappeared. Pending work is never automatically run.
            await _finish(command, Outcome.UNKNOWN, error_code="interrupted")
            raise
        except OrderOutcomeUnknown as exc:
            await _finish(command, Outcome.UNKNOWN, error_code="broker_unknown")
            state.log_event("error", f"Manual order {command.command_id}: outcome unknown — check the broker")
            raise ExecutionError("unknown", f"Order outcome unknown — check the broker: {exc}",
                                 command_id=command.command_id) from exc
        except ExecutionError as exc:
            await _finish(command, Outcome.REJECTED, error_code=exc.code)
            exc.command_id = command.command_id
            raise
        except TradovateError as exc:
            await _finish(command, Outcome.REJECTED, error_code="broker_rejected")
            raise ExecutionError("broker_rejected", str(exc), command_id=command.command_id) from exc
        except Exception as exc:
            outcome = Outcome.UNKNOWN if dispatched else Outcome.REJECTED
            code = "unknown" if dispatched else "unavailable"
            await _finish(command, outcome, error_code=code)
            message = ("Order outcome unknown — check the broker; do not retry with a new key" if dispatched
                       else "Execution unavailable; no order sent")
            raise ExecutionError(code, message, command_id=command.command_id) from exc

        if not await _finish(command, Outcome.ACCEPTED, response=payload):
            raise ExecutionError("unknown", "Broker acknowledged the order but recording failed — check the broker; do not retry",
                                 command_id=command.command_id)
        state.log_event("info", f"Manual order: {command.action} {command.quantity} {contract} {command.order_type} "
                                f"on {ex.name} by user {command.actor.user_id}")
        return ExecutionResult(command.command_id, Outcome.ACCEPTED, payload)

    async def close_position(self, command: ClosePosition) -> dict[str, Any]:
        await _authorize(command.actor)
        with context.use_area(command.actor.workspace_id):
            return await local.close_position(command)

    async def flatten(self, actor: Actor) -> dict[str, Any]:
        await _authorize(actor)
        with context.use_area(actor.workspace_id):
            return await local.flatten(actor.user_id)

    async def status(self, actor: Actor, command_id: str) -> dict[str, Any]:
        await _authorize(actor, write=False)
        command_key(command_id)
        try:
            row = await asyncio.to_thread(ledger.get, actor.workspace_id, command_id)
        except (sqlite3.Error, OSError) as exc:
            raise ExecutionError("unavailable", "Execution ledger unavailable") from exc
        if row is None:
            raise ExecutionError("not_found", "Execution command not found")
        return {"command_id": row["command_id"], "outcome": row["outcome"], "source": "manual",
                "target": json.loads(row["target_json"]), "error_code": row["error_code"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}


execution: ExecutionService = LocalExecutionService()

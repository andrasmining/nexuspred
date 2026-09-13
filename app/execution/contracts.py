"""Versioned, serializable execution intent. No broker or HTTP dependencies."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from ..commercial.workspaces import Actor

MAX_MANUAL_QTY = 100
ORDER_TYPES = ("Market", "Limit", "Stop", "StopLimit")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class RiskEffect(str, Enum):
    OPEN_RISK = "open_risk"
    INCREASE_RISK = "increase_risk"
    REDUCE_RISK = "reduce_risk"
    PROTECT_RISK = "protect_risk"
    CLOSE_RISK = "close_risk"
    EMERGENCY = "emergency"


class Outcome(str, Enum):
    CLAIMED = "claimed"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"        # acknowledgement, NOT a fill or a protected position
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ExecutionError(Exception):
    def __init__(self, code: str, message: str, *, command_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.command_id = command_id


def command_key(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ExecutionError("invalid", "Idempotency-Key must be 1–128 letters, digits, '.', '_', ':' or '-'")
    return value


def _price(value: Any, key: str, *, required: bool) -> float | None:
    if value in (None, ""):
        if required:
            raise ExecutionError("invalid", f"{key} is required for this order type")
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ExecutionError("invalid", f"{key} must be a number") from None
    if not math.isfinite(result) or result <= 0:
        raise ExecutionError("invalid", f"{key} must be a positive number")
    return result


@dataclass(frozen=True)
class AccountTarget:
    spec: str
    lid: str | None = None
    token_idx: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, str) or not self.spec:
            raise ExecutionError("invalid", "spec (trade account) is required")
        if len(self.spec) > 128:
            raise ExecutionError("invalid", "spec must be at most 128 characters")
        if self.lid is None and self.token_idx is None:
            raise ExecutionError("invalid", "lid or token_idx (the login) is required")
        if self.lid is not None and (not isinstance(self.lid, str) or not self.lid):
            raise ExecutionError("invalid", "lid must identify a login")
        if self.lid is not None and len(self.lid) > 128:
            raise ExecutionError("invalid", "lid must be at most 128 characters")
        if self.token_idx is not None and type(self.token_idx) is not int:
            raise ExecutionError("invalid", "token_idx must be a number")

    @classmethod
    def from_payload(cls, body: dict[str, Any]) -> AccountTarget:
        spec = str(body.get("spec") or "").strip()
        lid = str(body.get("lid") or "").strip() or None
        try:
            idx = int(body["token_idx"]) if body.get("token_idx") not in (None, "") else None
        except (TypeError, ValueError, OverflowError):
            raise ExecutionError("invalid", "token_idx must be a number") from None
        return cls(spec, lid, idx)


@dataclass(frozen=True)
class ManualOrder:
    actor: Actor
    account: AccountTarget
    symbol: str
    action: str
    quantity: int
    order_type: str = "Market"
    price: float | None = None
    stop_price: float | None = None
    command_id: str = field(default_factory=lambda: str(uuid4()))
    version: int = field(default=1, init=False)
    source: str = field(default="manual", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", command_key(self.command_id))
        if not isinstance(self.actor, Actor) or not isinstance(self.account, AccountTarget):
            raise ExecutionError("invalid", "Explicit actor and account are required")
        if self.action not in ("buy", "sell"):
            raise ExecutionError("invalid", "action must be buy or sell")
        if type(self.quantity) is not int or not 1 <= self.quantity <= MAX_MANUAL_QTY:
            raise ExecutionError("invalid", f"qty must be between 1 and {MAX_MANUAL_QTY}")
        if not isinstance(self.symbol, str) or not self.symbol or len(self.symbol) > 20:
            raise ExecutionError("invalid", "symbol is required")
        if self.order_type not in ORDER_TYPES:
            raise ExecutionError("invalid", f"order_type must be one of {', '.join(ORDER_TYPES)}")
        object.__setattr__(self, "price", _price(self.price, "price", required=self.order_type in ("Limit", "StopLimit")))
        object.__setattr__(self, "stop_price", _price(self.stop_price, "stop_price", required=self.order_type in ("Stop", "StopLimit")))

    @classmethod
    def from_payload(cls, actor: Actor, body: dict[str, Any], key: str | None = None) -> ManualOrder:
        # Retain the legacy route's numeric coercion; no sizing-policy change.
        try:
            raw_qty = float(body.get("qty") or 0)
            qty = int(raw_qty) if math.isfinite(raw_qty) else -1
        except (TypeError, ValueError, OverflowError):
            raise ExecutionError("invalid", "qty must be a whole number") from None
        order_type = str(body.get("order_type") or "Market").strip()
        return cls(actor, AccountTarget.from_payload(body), str(body.get("symbol") or "").strip(),
                   str(body.get("action") or "").strip().lower(), qty, order_type,
                   _price(body.get("price"), "price", required=order_type in ("Limit", "StopLimit")),
                   _price(body.get("stop_price"), "stop_price", required=order_type in ("Stop", "StopLimit")),
                   command_key(key))

    def intent_json(self) -> str:
        # Explicit fields only: never persist the raw HTTP body or credentials.
        data = asdict(self)
        data.pop("command_id")
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.intent_json().encode()).hexdigest()


@dataclass(frozen=True)
class ClosePosition:
    actor: Actor
    account: AccountTarget
    symbol: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor, Actor) or not isinstance(self.account, AccountTarget):
            raise ExecutionError("invalid", "Explicit actor and account are required")
        if not isinstance(self.symbol, str) or not self.symbol or len(self.symbol) > 20:
            raise ExecutionError("invalid", "symbol is required")


@dataclass(frozen=True)
class ExecutionResult:
    command_id: str
    outcome: Outcome
    payload: dict[str, Any]
    replayed: bool = False


class ExecutionService(Protocol):
    """Application-facing port. Transport/authentication remain outside it."""

    async def place_manual_order(self, command: ManualOrder) -> ExecutionResult: ...
    async def close_position(self, command: ClosePosition) -> dict[str, Any]: ...
    async def flatten(self, actor: Actor) -> dict[str, Any]: ...
    async def status(self, actor: Actor, command_id: str) -> dict[str, Any]: ...

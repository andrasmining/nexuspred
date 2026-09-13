"""Commercial permission is not authorization and never replaces risk checks."""
from __future__ import annotations

from ..execution.contracts import RiskEffect
from .entitlements import Entitlements


def permits(effect: RiskEffect, grants: Entitlements, *, reduction_verified: bool = False) -> bool:
    """Only trusted close/flatten implementations may assert CLOSE/EMERGENCY.

    A sell can reverse a long; moving a stop can widen risk. Neither an action
    name nor a client-provided 'reduce_only' flag proves risk reduction.
    REDUCE/PROTECT require a backend's verification to bypass a commercial
    restriction. This predicate does not grant tenant access or bypass risk.
    """
    if not isinstance(effect, RiskEffect):
        return False
    if effect in (RiskEffect.CLOSE_RISK, RiskEffect.EMERGENCY):
        return True
    if effect in (RiskEffect.REDUCE_RISK, RiskEffect.PROTECT_RISK) and reduction_verified:
        return True
    return grants.manual_trading

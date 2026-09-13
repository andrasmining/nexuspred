"""Manual-order entitlement override; no global suspension or billing yet."""
from __future__ import annotations

from . import core


def manual_trading(area_id: int) -> bool:
    if type(area_id) is not int or area_id <= 0:
        raise ValueError("Explicit workspace id required")
    core.init()
    with core._connect() as c:
        # Distinguish an existing grandfathered workspace from a missing one.
        if c.execute("SELECT 1 FROM areas WHERE id=?", (area_id,)).fetchone() is None:
            raise ValueError("Workspace does not exist")
        row = c.execute("SELECT enabled FROM commercial_entitlements WHERE area_id=? AND capability='manual_trading'",
                        (area_id,)).fetchone()
    return True if row is None else bool(row["enabled"])


def set_manual_trading(area_id: int, enabled: bool) -> None:
    """Internal operator integration point. Callers must authorize the operator.

    Scope is deliberately manual orders only. Never use this as a subscription
    cancellation implementation until all entry paths share the same policy.
    """
    if type(area_id) is not int or area_id <= 0 or type(enabled) is not bool:
        raise ValueError("Explicit workspace id and boolean entitlement required")
    core.init()
    with core._connect() as c:
        c.execute("INSERT INTO commercial_entitlements(area_id, capability, enabled, updated_at) VALUES(?,'manual_trading',?,?) "
                  "ON CONFLICT(area_id,capability) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                  (area_id, int(enabled), core._now()))

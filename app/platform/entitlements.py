"""Commercial entitlements, separate from identity/RBAC and trading risk locks.

Only the ``manual_trading`` capability has an override in this first slice.
It is NOT a global account suspension: signal/copy ingress is unchanged.
Missing override means legacy behavior; unreadable storage never means allowed.
There is deliberately no customer-facing entitlement mutation endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db import entitlements as store
from .workspaces import WorkspaceId, workspace_id


@dataclass(frozen=True)
class Entitlements:
    manual_trading: bool = True

    def __post_init__(self) -> None:
        if type(self.manual_trading) is not bool:
            raise ValueError("manual_trading must be a boolean")


def for_workspace(area_id: WorkspaceId) -> Entitlements:
    return Entitlements(manual_trading=store.manual_trading(workspace_id(area_id)))

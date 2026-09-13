"""Explicit workspace identity; the storage name remains ``area_id``.

An actor is supplied by a trusted authentication boundary, not deserialized
from a customer payload. Membership is rechecked by the execution service.
Platform administrators do not implicitly own other workspaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

WorkspaceId = NewType("WorkspaceId", int)


class WorkspaceAccessDenied(PermissionError):
    pass


def workspace_id(value: int) -> WorkspaceId:
    if type(value) is not int or value <= 0:
        raise WorkspaceAccessDenied("An explicit workspace is required")
    return WorkspaceId(value)


@dataclass(frozen=True)
class Actor:
    workspace_id: WorkspaceId
    user_id: int

    def __post_init__(self) -> None:
        workspace_id(self.workspace_id)
        if type(self.user_id) is not int or self.user_id <= 0:
            raise WorkspaceAccessDenied("An authenticated user is required")


def current_actor(user_id: int) -> Actor:
    """Bridge the authenticated legacy context without falling back to area 1."""
    from .. import context
    return Actor(workspace_id(context.get_area_optional()), user_id)


def authorize(actor: Actor, *, write: bool = True) -> None:
    """Resolve membership from storage, never from ``is_admin`` or request data."""
    from .. import db
    db.init()
    with db._connect() as c:
        row = c.execute(
            "SELECT m.role FROM memberships m JOIN areas a ON a.id=m.area_id "
            "JOIN users u ON u.id=m.user_id WHERE m.area_id=? AND m.user_id=?",
            (actor.workspace_id, actor.user_id),
        ).fetchone()
    if row is None or (write and row["role"] not in ("owner", "admin", "trader")):
        raise WorkspaceAccessDenied("Workspace membership does not permit this operation")

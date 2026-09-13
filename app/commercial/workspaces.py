"""Explicit workspace identity; the storage name remains ``area_id``.

An actor is supplied by a trusted authentication boundary, not deserialized
from a customer payload. Membership is rechecked by the execution service.
Platform administrators do not implicitly own other workspaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NewType

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
    # An admin inside a support view the workspace owner granted write access to.
    # Set only by the gate from the signed support cookie, never from a payload.
    support: bool = False

    def __post_init__(self) -> None:
        workspace_id(self.workspace_id)
        if type(self.user_id) is not int or self.user_id <= 0:
            raise WorkspaceAccessDenied("An authenticated user is required")
        if type(self.support) is not bool:
            raise WorkspaceAccessDenied("support must be a boolean")


def current_actor(user_id: int, support: Any = None) -> Actor:
    """Bridge the authenticated legacy context without falling back to area 1.

    ``support`` is the gate's own support state (``request.state.support``): its
    ``write`` flag is set only after the workspace owner granted a time-boxed
    write window, so it is an authorization the gate already established.
    """
    from .. import context
    granted = bool(isinstance(support, dict) and support.get("write"))
    return Actor(workspace_id(context.get_area_optional()), user_id, granted)


def authorize(actor: Actor, *, write: bool = True) -> None:
    """Resolve membership from storage, never from ``is_admin`` or request data.

    The one path that is not a membership is assisted support: the workspace
    owner grants it explicitly, it expires, and every write it makes is audited.
    The grant is re-read here so a revoked one stops the next order.
    """
    from .. import db
    db.init()
    with db._connect() as c:
        row = c.execute(
            "SELECT m.role FROM memberships m JOIN areas a ON a.id=m.area_id "
            "JOIN users u ON u.id=m.user_id WHERE m.area_id=? AND m.user_id=?",
            (actor.workspace_id, actor.user_id),
        ).fetchone()
    if row is not None and (not write or row["role"] in ("owner", "admin", "trader")):
        return
    if actor.support:
        from .. import web
        if web.support_grant_active(actor.workspace_id):
            return
        raise WorkspaceAccessDenied("The support write window has expired — ask the workspace owner to grant it again")
    raise WorkspaceAccessDenied("Workspace membership does not permit this operation")

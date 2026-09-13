"""HTTP adaptation of execution errors; domain services never import FastAPI."""
from fastapi import HTTPException

from ..execution.contracts import ExecutionError

_STATUS = {"invalid": 400, "forbidden": 403, "entitlement_denied": 403,
           "not_found": 404, "trading_disabled": 409, "conflict": 409, "in_progress": 409,
           "broker_rejected": 502, "unknown": 502, "unavailable": 503}


def http_error(exc: ExecutionError) -> HTTPException:
    headers = {"X-Execution-Command-Id": exc.command_id} if exc.command_id else None
    return HTTPException(status_code=_STATUS.get(exc.code, 503), detail=str(exc), headers=headers)

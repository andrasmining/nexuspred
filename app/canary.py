"""Canary signal (alpha.99): the whole signal path, end to end, every few minutes.

A synthetic bracket signal is pushed through :func:`app.signals.process` in
simulation mode inside the operator's workspace — receipt, parsing, sizing,
order placement against the in-memory broker, bookkeeping — and timed. A
canary that fails or slows beyond ``canary_max_ms`` is the earliest sign of
trouble, before a real trade is affected. The admins hear about a change of
state once; the readiness check carries the last result.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, context, db, state

log = logging.getLogger("nexuspred.canary")

_results: deque[dict[str, Any]] = deque(maxlen=200)
_state: Optional[str] = None            # "ok" | "slow" | "failed"
LOOP_TICK_S = 30.0
_last_run = 0.0


def _webhook() -> dict[str, Any]:
    return {"id": "canary", "name": "canary", "strategy": "bracket", "enabled": True, "accounts": [], "default_qty": 1, "tp_qty": 1}


async def run_once() -> dict[str, Any]:
    """One canary round trip: open long, then close, in the simulator."""
    from . import platform, signals
    cfg = platform.get_config()
    started = time.perf_counter()
    res: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "ok": False, "ms": None, "detail": ""}
    with context.use_area(context.DEFAULT_AREA_ID):
        try:
            r1 = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1, "sl": 100, "tp": 100, "canary": True}, None, simulate=True, trusted=True)
            r2 = await signals.process({"action": "close_all", "symbol": "MNQ1!", "canary": True}, None, simulate=True, trusted=True)
            ms = round((time.perf_counter() - started) * 1000, 1)
            ok = str(r1.get("status") or "ok") not in ("error",) and str(r2.get("status") or "ok") not in ("error",)
            res.update({"ok": ok, "ms": ms, "detail": f"{r1.get('status') or 'ok'} / {r2.get('status') or 'ok'}"})
        except Exception as exc:  # noqa: BLE001
            res.update({"ok": False, "ms": round((time.perf_counter() - started) * 1000, 1), "detail": f"{type(exc).__name__}: {exc}"[:200]})
    res["slow"] = bool(res["ok"] and res["ms"] is not None and res["ms"] > cfg["canary_max_ms"])
    _results.append(res)
    await _notify(res)
    return res


async def _notify(res: dict[str, Any]) -> None:
    global _state
    st = "failed" if not res["ok"] else ("slow" if res["slow"] else "ok")
    if _state is None:
        _state = st
        return
    if st == _state:
        return
    prev, _state = _state, st
    from . import alerts
    if st == "ok":
        await alerts.notify_admins("notice", {"title": "Canary healthy again", "message": f"The canary signal passes again ({res['ms']} ms; was {prev})."},
                                   inbox=("canary.recovered", "info", "Canary healthy again", "/#/settings/platform"), mail=False)
    else:
        await alerts.notify_admins("notice", {"title": f"Canary {st}", "message": f"The synthetic signal {'failed' if st == 'failed' else 'was slow'}: {res['detail']} ({res['ms']} ms).",
                                              "button": "Open Platform", "url": (config.PUBLIC_URL or "") + "/#/settings/platform"},
                                   inbox=("canary.alarm", "critical" if st == "failed" else "warn", f"Canary {st}: {res['detail']}", "/#/settings/platform"))
        with context.use_area(context.DEFAULT_AREA_ID):
            state.log_event("error" if st == "failed" else "warn", f"Canary {st}: {res['detail']} ({res['ms']} ms)")


def status() -> dict[str, Any]:
    from . import platform
    cfg = platform.get_config()
    last = _results[-1] if _results else None
    recent = list(_results)[-20:]
    return {"enabled": cfg["canary_enabled"], "every_minutes": cfg["canary_minutes"], "max_ms": cfg["canary_max_ms"], "last": last,
            "state": _state or ("ok" if last and last["ok"] and not last["slow"] else None), "runs": len(_results),
            "ok_share": round(sum(1 for r in recent if r["ok"]) / len(recent), 3) if recent else None,
            "p95_ms": sorted(r["ms"] for r in recent if r["ms"] is not None)[max(0, int(0.95 * (len([r for r in recent if r["ms"] is not None]) - 1)))] if any(r["ms"] is not None for r in recent) else None}


async def loop() -> None:
    global _last_run
    from . import platform
    while True:
        await asyncio.sleep(LOOP_TICK_S)
        try:
            cfg = platform.get_config()
            if cfg["canary_enabled"] and time.time() - _last_run >= cfg["canary_minutes"] * 60 and db.user_count() > 0:
                _last_run = time.time()
                await run_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("canary failed: %s", exc)


def reset() -> None:
    global _state, _last_run
    _results.clear()
    _state = None
    _last_run = 0.0

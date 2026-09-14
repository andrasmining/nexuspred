"""Durable history of signals and orders.

:mod:`app.state` keeps a 200-entry ring buffer per area for the live UI; every
entry is *also* handed to this module, which writes it to SQLite
(``signal_log`` / ``order_log``) so a deploy or restart no longer wipes the
record. On startup the ring buffers are re-filled from the tables.

Writes go through a single background thread (batched, one connection) so a
burst of alerts never blocks the event loop on disk I/O. When the writer is not
running — tests, one-off scripts — writes happen inline instead, so the data is
always durable either way.

Retention: rows older than ``NEXUSPRED_HISTORY_DAYS`` (default 90) are pruned
at startup and once a day.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import db

log = logging.getLogger(__name__)
RETENTION_DAYS = int(os.environ.get("NEXUSPRED_HISTORY_DAYS") or 90)
HYDRATE_ROWS = 200  # matches state._MAX

# Bounded: a writer that cannot keep up must slow its producers down rather
# than grow without limit in memory. A full queue drops the oldest pending row
# and says so — the trade itself never waits on the history log.
QUEUE_MAX = int(os.environ.get("NEXUSPRED_HISTORY_QUEUE") or 50000)
STOP_POLL_S = 0.25          # how often an idle writer checks whether it should stop
STOP_TIMEOUT_MIN_S = 5.0
STOP_TIMEOUT_MAX_S = 60.0
_q: "queue.Queue[Optional[tuple[str, int, dict[str, Any]]]]" = queue.Queue(maxsize=QUEUE_MAX)
_dropped = 0
_thread: Optional[threading.Thread] = None
_running = False
_idle = threading.Event()
_idle.set()


# ------------------------------------------------------------------ writer
def _write(kind: str, area_id: int, entry: dict[str, Any]) -> None:
    if kind == "signal":
        db.insert_signal(area_id, entry)
    else:
        db.insert_order(area_id, entry)


BATCH_MAX = 50


def _run_one(item: Any) -> None:
    try:
        if item[0] == "call":
            item[1](*item[2], **item[3])
        else:
            _write(*item)
    except Exception as exc:  # noqa: BLE001 - history must never kill the writer
        log.error("history write failed (%s row dropped): %s", item[0], exc)


def _worker() -> None:
    while True:
        try:
            # A poll rather than a blocking get: the stop marker cannot be queued
            # when the (bounded) queue is full, so the flag has to be reachable.
            item = _q.get(timeout=STOP_POLL_S)
        except queue.Empty:
            if not _running:
                _idle.set()
                return
            continue
        if item is None:
            _idle.set()
            return
        _idle.clear()
        items = [item]
        while len(items) < BATCH_MAX:                 # everything already queued lands in one transaction
            try:
                nxt = _q.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                _q.put(None)                          # the stop marker stays last
                break
            items.append(nxt)
        try:
            with db.core.batch():
                for it in items:
                    _run_one(it)
        except Exception as exc:  # noqa: BLE001 - a broken batch is retried row by row, nothing is lost silently
            log.error("history batch failed (%s rows): %s — retrying one by one", len(items), exc)
            for it in items:
                _run_one(it)
        finally:
            if _q.empty():
                _idle.set()


def backlog() -> int:
    """Rows waiting for the writer thread (alpha.96: a readiness signal)."""
    return _q.qsize()


def dropped() -> int:
    """Rows the writer could not take because the queue was full."""
    return _dropped


def start() -> None:
    """Start the background writer (idempotent)."""
    global _thread, _running
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_worker, name="history-writer", daemon=True)
    _thread.start()


def stop(timeout: Optional[float] = None) -> None:
    """Drain the queue and stop the writer.

    A signal or order row that never lands is a gap in the trade record, so the
    drain is given time proportional to what is actually queued (a batch writes
    thousands of rows a second) instead of a flat few seconds. ``timeout=None``
    computes it from :func:`backlog`; the wait is still bounded so a wedged disk
    cannot hold the shutdown open forever. Rows left over are reported.
    """
    global _thread, _running
    if not _running:
        return
    _running = False
    left = backlog()
    if timeout is None:
        timeout = min(STOP_TIMEOUT_MAX_S, max(STOP_TIMEOUT_MIN_S, left / 500.0))
    try:
        _q.put_nowait(None)          # best effort: a full queue is drained first, then the flag stops the worker
    except queue.Full:
        pass
    if _thread is not None:
        _thread.join(timeout)
        _thread = None
    if backlog():
        log.error("shutdown: %s history row(s) were still queued after %.0f s and are lost", backlog(), timeout)


def flush(timeout: float = 5.0) -> None:
    """Block until every queued write has landed (tests / shutdown)."""
    if _running:
        _idle.wait(timeout)


def defer(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Run a small database write on the writer thread (in order with the
    history rows) instead of on the event loop; synchronous when the writer is
    not running (tests, shutdown). The copy engine's event and state rows go
    through here so a WAL commit never stalls an order."""
    if _running:
        _idle.clear()
        _q.put(("call", fn, args, kwargs))
    else:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.error("deferred write failed (%s): %s", getattr(fn, "__name__", fn), exc)


def _submit(kind: str, area_id: int, entry: dict[str, Any]) -> None:
    global _dropped
    if _running:
        _idle.clear()
        try:
            _q.put_nowait((kind, area_id, dict(entry)))
        except queue.Full:
            # The order path never blocks on bookkeeping: the row is dropped and
            # counted, and readiness reports the backlog that caused it.
            _dropped += 1
            if _dropped % 100 == 1:
                log.error("history queue full (%s rows): %s row dropped, %s dropped in total", _q.qsize(), kind, _dropped)
    else:
        try:
            _write(kind, area_id, entry)
        except Exception as exc:  # noqa: BLE001
            log.error("history write failed (%s row dropped): %s", kind, exc)


def record_signal(area_id: int, entry: dict[str, Any]) -> None:
    _submit("signal", area_id, entry)


def record_order(area_id: int, entry: dict[str, Any]) -> None:
    _submit("order", area_id, entry)


# --------------------------------------------------------- startup helpers
def hydrate(area_ids: list[int]) -> int:
    """Refill every area's live ring buffers from the tables. Returns rows loaded."""
    from . import state
    loaded = 0
    for aid in area_ids:
        signals = db.list_signals(aid, limit=HYDRATE_ROWS)["items"]
        orders = db.list_orders(aid, limit=HYDRATE_ROWS)["items"]
        state.hydrate(aid, signals=signals, orders=orders)
        loaded += len(signals) + len(orders)
    return loaded


def prune(days: Optional[int] = None) -> int:
    """Delete rows older than the retention window. Returns rows removed."""
    days = RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return db.prune_history(cutoff)

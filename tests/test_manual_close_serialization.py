"""A dashboard close must use the same locks as the signal it is closing."""
from __future__ import annotations

import asyncio

import pytest

from app import context, signals
from app.execution import local
from tests.test_alpha76 import ticket  # noqa: F401 - broker executor fixture


@pytest.mark.parametrize("key,fields,lock_key", [
    ("trade:123", {"trade_id": "trade:123", "webhook_id": "wh_ts", "root": "MNQ"}, "1:live:ts:trade:123"),
    ("sub:5:MNQ", {"webhook_id": "sub:5", "root": "MNQ"}, "1:live:sub:5:MNQ"),
    ("wh_alias:NASDAQ", {"webhook_id": "wh_alias", "root": "NASDAQ"}, "1:live:wh_alias:NASDAQ"),
])
async def test_close_waits_for_the_actual_signal_lock(client, ticket, monkeypatch, key, fields, lock_key):
    with context.use_area(1):
        signals._map_for(False)[key] = {**fields, "contract": "MNQZ6", "accounts": {"DEMO11": {"contract": "MNQZ6"}}}
    called = asyncio.Event()

    async def close(*args):
        called.set()
        return 0

    monkeypatch.setattr(local, "_close_contract", close)
    lock = signals._trade_lock(lock_key)
    await lock.acquire()
    task = asyncio.create_task(client.post("/api/positions/close", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQZ6"}))
    try:
        await asyncio.sleep(0.02)
        assert not called.is_set(), "manual close raced an in-flight signal by taking the wrong lock"
    finally:
        lock.release()
        response = await task
    assert response.status_code == 200 and called.is_set()


async def test_cancelled_close_releases_locks_already_acquired(client, ticket, monkeypatch):
    with context.use_area(1):
        for wid in ("wh_a", "wh_b"):
            signals._map_for(False)[f"{wid}:MNQ"] = {"webhook_id": wid, "root": "MNQ", "contract": "MNQZ6",
                                                     "accounts": {"DEMO11": {"contract": "MNQZ6"}}}
    first = signals._trade_lock("1:live:wh_a:MNQ")
    second = signals._trade_lock("1:live:wh_b:MNQ")
    await second.acquire()
    task = asyncio.create_task(client.post("/api/positions/close", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQZ6"}))
    try:
        for _ in range(100):
            if first.locked():
                break
            await asyncio.sleep(0.001)
        assert first.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not first.locked(), "cancelling a queued close stranded another trade's lock"
    finally:
        second.release()
        if first.locked():
            first.release()
        if not task.done():
            task.cancel()

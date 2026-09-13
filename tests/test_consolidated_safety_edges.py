"""Focused regressions for cleanup reconciliation and publisher-side gating."""
from __future__ import annotations

from types import SimpleNamespace

from app import signals
from app.engine import ts_hunter
from app.tradovate import TradovateError


class _CancelReconcileExecutor:
    name = "A"

    def __init__(self, *, still_working: bool) -> None:
        self.still_working = still_working
        self.cancel_calls = 0
        self.place_calls = []

    async def cancel_order(self, _order_id):
        self.cancel_calls += 1
        if self.cancel_calls == 1:
            raise TradovateError("transport answer lost")
        return {"status": "cancelled"}

    async def working_orders(self):
        return [{"id": 9}] if self.still_working else []

    async def place_order(self, **kw):
        self.place_calls.append(dict(kw))
        return {"status": "submitted", "order_id": 42}

    async def positions(self):
        return []


async def _full_close(ex):
    active = {
        "T1": {
            "side": "buy",
            "accounts": {
                "A": {
                    "contract": "MNQ",
                    "remaining_qty": 1,
                    "qty": 1,
                    "sl_order_id": 9,
                }
            },
        }
    }
    return await ts_hunter.handle_full_close(
        {"reason": "test"}, "T1", "MNQ", [ex], active, ""
    )


async def test_ts_full_close_does_not_retry_cancel_when_broker_says_order_is_gone(admin):
    ex = _CancelReconcileExecutor(still_working=False)
    result = await _full_close(ex)
    assert result["status"] == "ok"
    assert ex.cancel_calls == 1
    assert len(ex.place_calls) == 1


async def test_ts_full_close_retries_cancel_only_after_broker_confirms_it_is_working(admin):
    ex = _CancelReconcileExecutor(still_working=True)
    result = await _full_close(ex)
    assert result["status"] == "ok"
    assert ex.cancel_calls == 2
    assert len(ex.place_calls) == 1


def test_publisher_window_blocks_marketplace_fanout_before_subscriber_dispatch(admin, monkeypatch):
    webhook = {
        "id": "wh_pub",
        "name": "published",
        "strategy": "simple",
        "sharing": {"enabled": True, "visibility": "all"},
        "trade_window": {"enabled": True},
    }
    monkeypatch.setattr(signals.trade_window, "is_open", lambda *_a, **_kw: (False, "publisher window closed"))

    def should_not_dispatch(*_a, **_kw):
        raise AssertionError("subscriber lookup must not happen when publisher window is closed")

    from app import db
    monkeypatch.setattr(db, "active_subscriptions", should_not_dispatch)
    assert signals.forward_to_subscribers({"action": "buy", "symbol": "MNQ"}, webhook) == 0

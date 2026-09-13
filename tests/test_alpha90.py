"""alpha.90: review round 7 — engine edge cases, adapter classification, copy
exposure memory, marketplace billing, platform limits."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import automations, config, context, db, events, marketplace, payments, projectx, security, signals, state, tradovate
from app import copy as cp
from app.engine import bracket, common, manage, ts_hunter
from app.tradovate import OrderOutcomeUnknown, TradovateError
from tests.helpers import FakeExecutor
from tests.test_alpha88 import mixed  # noqa: F401 - the cross-broker fixture
from tests.test_copy import _placed
from tests.test_strategies import ENTRY, active, live, wh  # noqa: F401 - fixtures


# ================================================================ engine
async def test_ts_full_close_with_a_lost_answer_is_not_replayed(admin):
    class Lost(FakeExecutor):
        closes = 0

        async def place_order(self, **kw):
            Lost.closes += 1
            raise OrderOutcomeUnknown("answer lost")
    a = Lost("A")
    am = {"T1": {"side": "buy", "accounts": {"A": {"name": "A", "contract": "MNQZ6", "remaining_qty": 2, "sl_order_id": 9}}}}
    r = await ts_hunter.handle_full_close({}, "T1", "MNQ", [a], am, "")
    assert r["status"] == "error" and "T1" in am                       # stays tracked
    assert am["T1"]["accounts"]["A"]["remaining_qty"] is None          # the quantity is unknown now
    # the retry flattens what the broker shows instead of sending "Sell 2" again
    b = FakeExecutor("A", positions=[{"symbol": "MNQZ6", "netPos": 2}])
    r = await ts_hunter.handle_full_close({}, "T1", "MNQ", [b], am, "")
    assert Lost.closes == 1 and r["status"] == "ok" and "T1" not in am
    assert b.of("liquidate") and not b.of("place")


async def test_ts_partial_close_refuses_an_unknown_quantity(admin):
    a = FakeExecutor("A")
    am = {"T1": {"side": "buy", "accounts": {"A": {"name": "A", "contract": "MNQZ6", "remaining_qty": None, "sl_order_id": 9}}}}
    r = await ts_hunter.handle_partial_close({"percent": 50}, "T1", [a], am, "")
    assert r["status"] == "error" and not a.of("place")


async def test_close_all_closes_the_tracked_contract_not_the_current_mapping(admin):
    a = FakeExecutor("A", working=[{"id": 41, "symbol": "MNQU6"}])
    am = {"wh:MNQ": {"webhook_id": "wh", "root": "MNQ", "side": "buy", "ts": 0.0,
                     "accounts": {"A": {"name": "A", "contract": "MNQU6", "qty": 1}}}}
    r = await manage.handle_close_all("MNQ", "MNQZ6", [a], am, "", {"id": "wh", "name": "w"})
    assert r["status"] == "ok"
    assert a.of("liquidate")[-1]["symbol"] == "MNQU6"                   # the month the trade was placed in
    assert [c["order_id"] for c in a.of("cancel")] == [41]


def test_remaining_qty_uses_the_slices_the_entry_placed():
    info = {"entry_qty": 3, "tp_qty": 1, "tp_slices": [[1, 1], [3, 1]]}   # tp1 + tp3 only: two slices
    assert bracket._remaining_qty(info, 1) == 2
    assert bracket._remaining_qty(info, 3) == 1                        # one contract is still open — the stop is resized, not retired
    assert bracket._remaining_qty({"qty": 2, "sl_order_id": 5}, 1) == 1   # a set_sl_tp record without entry_qty


async def test_bracket_stop_leaves_with_the_targets_and_records_slices(live):
    a = FakeExecutor("A")
    live.use(a)
    await signals.process({**ENTRY, "tp2": None, "sl": 110.0}, wh("bracket", default_qty=3, tp_qty=1, id="wh_s"))
    kinds = [p["order_type"] for p in a.of("place")]
    assert kinds == ["Market", "Stop", "Limit", "Limit"]               # entry first, then the stop ahead of the targets
    info = active("wh_s:MNQ")["accounts"]["A"]
    assert info["tp_slices"] == [[1, 1], [3, 1]]


async def test_fractional_qty_is_rejected_on_strict_strategies():
    with pytest.raises(common.SignalError):
        common._signal_qty(1.5, 1, strict=True)
    assert common._signal_qty(1.5, 2, strict=False) == 2.0


async def test_automation_cooldown_is_reserved_before_the_action_runs(admin, monkeypatch):
    ran = []

    async def slow(area_id, rule, kind, data, aliases=None):
        ran.append(rule["id"])
        await asyncio.sleep(0.05)
        return "ok"
    monkeypatch.setattr(automations, "_run_action", slow)
    config.save_settings({"automations": automations.normalize_rules([
        {"id": "au_1", "name": "once", "event": "position.closed", "action": "notify", "cooldown_s": 60}])}, area_id=1)
    with context.use_area(1):
        await asyncio.gather(*(events.emit_async("position.closed", account="A", symbol="MNQ", pnl=-1.0) for _ in range(3)))
    assert ran == ["au_1"]                                             # three events in one tick: one run


def test_automation_account_filter_ignores_events_without_an_account():
    rule = automations.normalize_rule({"event": "execution.problem", "action": "notify", "accounts": ["A"]})
    assert automations.matches(rule, "execution.problem", {"title": "x"}, 1)


async def test_active_trades_survive_a_restart(admin):
    with context.use_area(1):
        m = signals._map_for(False)
        m["wh:MNQ"] = {"webhook_id": "wh", "root": "MNQ", "contract": "MNQZ6", "side": "buy", "ts": time.time(),
                       "accounts": {"A": {"name": "A", "contract": "MNQZ6", "qty": 1, "sl_order_id": 9}}}
        m["old"] = {"webhook_id": "wh", "root": "MNQ", "ts": time.time() - signals.ACTIVE_TTL_S - 1, "accounts": {"A": {}}}
        assert signals.persist_active() == 1
        assert signals.persist_active() == 0                           # unchanged: nothing written
        m.clear()
        assert signals.hydrate_active([1]) == 1                        # the expired one stays gone
        assert signals._map_for(False)["wh:MNQ"]["accounts"]["A"]["sl_order_id"] == 9


async def test_shutdown_waits_for_signals_in_flight(admin):
    done = []

    async def slow():
        await asyncio.sleep(0.05)
        done.append(1)
    signals._spawn(slow())
    assert await signals.drain_background(timeout=1.0) == 0 and done == [1]


# ================================================================ adapters
async def test_a_follower_of_a_cancelled_shared_read_runs_its_own(admin, monkeypatch):
    sess = tradovate.TradovateSession(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"id": 11, "spec": "A", "enabled": True}]}, area_id=1)
    calls = []

    async def raw(method, path, **kw):
        calls.append(path)
        await asyncio.sleep(0.05)
        return [{"n": len(calls)}]
    monkeypatch.setattr(sess, "_request_raw", raw)
    leader = asyncio.ensure_future(sess._request("GET", "/position/list"))
    await asyncio.sleep(0.001)
    follower = asyncio.ensure_future(sess._request("GET", "/position/list"))
    await asyncio.sleep(0.01)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert await follower == [{"n": 2}]                                # not cancelled with the leader: a read of its own


async def test_a_pasted_token_brings_its_own_expiry(admin):
    import base64
    exp = int(time.time()) + 3600
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    token = f"h.{payload}.s"
    sess = tradovate.TradovateSession(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": token,
                                          "token_expires": "2020-01-01T00:00:00+00:00", "accounts": []}, area_id=1)
    assert sess._token_valid()                                          # the stale stored expiry does not win over the token's exp


async def test_gateway_502_on_an_order_path_is_an_unknown_outcome(admin, monkeypatch):
    sess = tradovate.TradovateSession(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t", "accounts": []}, area_id=1)
    monkeypatch.setattr(sess, "_get_token", lambda: asyncio.sleep(0, result="t"))

    class Bad:
        status_code, text = 502, "<html>bad gateway</html>"
    monkeypatch.setattr(httpx.AsyncClient, "request", lambda self, *a, **k: asyncio.sleep(0, result=Bad()))
    with pytest.raises(OrderOutcomeUnknown):
        await sess._request_raw("POST", "/order/placeorder", json={})
    with pytest.raises(TradovateError):
        await sess._request_raw("GET", "/position/list")


async def test_projectx_lost_answers_on_cancel_and_modify_are_unknown(admin, monkeypatch):
    sess = projectx.ProjectXSession(0, {"name": "PX", "broker": "projectx", "px_user": "u", "px_api_key": "k", "enabled": True,
                                        "accounts": [{"id": 1, "spec": "P1", "enabled": True}]}, area_id=1)

    async def post(path, body, **kw):
        raise httpx.ReadTimeout("gone")
    monkeypatch.setattr(sess, "_post", post)
    with pytest.raises(OrderOutcomeUnknown):
        await sess.cancel_order(5, account_id=1)
    with pytest.raises(OrderOutcomeUnknown):
        await sess.modify_order(5, qty=1, order_type="Stop", stop_price=1.0, account_id=1)
    assert [o["status"] for o in state.recent_orders()][:1] == ["unknown"]


def test_projectx_retry_after_is_clamped(admin):
    sess = projectx.ProjectXSession(0, {"name": "PX", "broker": "projectx", "px_user": "u", "px_api_key": "k", "enabled": True, "accounts": []}, area_id=1)
    assert projectx.MAX_PENALTY_S == 120.0 and sess.penalty_until == 0.0


def test_projectx_unusable_gateway_disables_the_login(admin):
    sess = projectx.ProjectXSession(0, {"name": "PX", "broker": "projectx", "px_user": "u", "px_api_key": "k", "enabled": True,
                                        "px_firm": "http://evil.example", "accounts": []}, area_id=1)
    assert sess.enabled is False and "disabled" in str(state.session_status("PX").get("last_error"))


# ================================================================ copy trading
async def test_memory_dropped_after_a_news_flatten_is_read_before_the_next_delta(mixed):
    r, px, ex = mixed["r"], mixed["px"], mixed["ex"]
    r.contract_names[901] = "MNQZ6"
    r.leader_net[901] = r.unit[901] = 2
    r.follower_pos[("PX-1", 901)] = 2                                  # what the mirror remembers
    px.positions = []                                                  # what the broker shows after the flatten
    r._mark_feed(True)
    r.follower_pos.pop(("PX-1", 901))                                  # a lock skip forgot it
    await r._mirror_follower(r.followers[0], 901, "MNQZ6", 0, 2, True, "position", None)
    assert _placed(ex) == []                                           # leader flat, follower flat: nothing to sell


async def test_seed_keeps_memory_inside_the_settle_window(mixed, monkeypatch):
    r, px = mixed["r"], mixed["px"]
    monkeypatch.setattr(cp.group_runner, "ORDER_SETTLE_S", 5.0)
    r.contract_names[901] = "MNQZ6"
    r.follower_pos[("PX-1", 901)] = 2
    r.last_order_at["PX-1"] = time.monotonic()                          # a fill may still be landing
    px.positions = []
    await r._seed_followers(force=True)
    assert r.follower_pos[("PX-1", 901)] == 2


async def test_external_follower_errors_stay_generic_for_the_publisher(mixed):
    r = mixed["r"]
    r.followers[0].update(external=True, sub_id=5, area_id=2)
    raw = r._ferr("PX-1", TradovateError("[Apex Eval - John Doe] /order/placeorder: rejected"))
    assert "John Doe" in raw and "John Doe" not in r.follower_err["PX-1"]
    assert r._redact("[Apex Eval - John Doe] PX-1 rejected", "PX-1", "subscriber #5") == "[login] subscriber #5 rejected"


async def test_kicking_a_subscriber_cancels_their_stripe_subscription(client, monkeypatch):
    cancelled = []

    async def fake_cancel(p):
        cancelled.append(p["stripe_subscription"])
        return True
    monkeypatch.setattr(payments, "cancel_stripe_subscription", fake_cancel)
    db.upsert_payment(1, 1, "copy:g1", stripe_subscription="sub_1", status="active")
    assert await payments.cancel_for_listing(1, "copy:g1", area_id=1) == 1 and cancelled == ["sub_1"]
    assert await payments.cancel_for_listing(1, "copy:g1", area_id=7) == 0             # another subscriber's row is not touched


async def test_a_refund_is_not_undone_by_a_routine_subscription_update(admin, monkeypatch):
    monkeypatch.setattr(payments, "_apply", lambda p: asyncio.sleep(0))
    db.upsert_payment(2, 1, "wh_1", stripe_subscription="sub_9", stripe_customer="cus_1", status="active",
                      current_period_end="2026-10-01T00:00:00+00:00")
    invoice = {"object": "invoice", "id": "in_1", "subscription": "sub_9"}
    assert "withdrawn" in await payments.handle_event({"id": "evt_1", "created": 100, "type": "invoice.marked_uncollectible", "data": {"object": invoice}})
    sub = {"object": "subscription", "id": "sub_9", "customer": "cus_1", "status": "active", "current_period_end": 1790812800}   # the same period
    await payments.handle_event({"id": "evt_2", "created": 101, "type": "customer.subscription.updated", "data": {"object": sub}})
    assert db.payment_by("stripe_subscription", "sub_9")["status"] == "unpaid"
    sub["current_period_end"] = 1793491200                             # the next, paid period
    await payments.handle_event({"id": "evt_3", "created": 102, "type": "customer.subscription.updated", "data": {"object": sub}})
    assert db.payment_by("stripe_subscription", "sub_9")["status"] == "active"


def test_daily_cap_is_reserved_in_the_gate(admin):
    marketplace.reset_daily_counts()
    view = {"id": "sub_wh", "controls": {"max_signals_per_day": 1}}
    with context.use_area(1):
        assert marketplace.subscription_gate(view, "MNQ", "buy", area_id=1)[0]
        assert not marketplace.subscription_gate(view, "MNQ", "buy", area_id=1)[0]          # the slot is taken before the order ran
        marketplace.note_outcome(view, 1, "skipped", "buy")                                  # a skipped entry gives it back
        assert marketplace.subscription_gate(view, "MNQ", "buy", area_id=1)[0]


def test_own_group_refuses_an_account_that_follows_a_subscription():
    g = {"id": "g2", "name": "mine", "leader": {"token_idx": 0, "spec": "L"}, "followers": [{"token_idx": 0, "spec": "F1", "enabled": True}]}
    accounts = [{"token_idx": 0, "spec": "L", "lid": "a"}, {"token_idx": 0, "spec": "F1", "lid": "a"}]
    with pytest.raises(ValueError, match="marketplace"):
        cp.validate_group(g, [], accounts, {"F1"})


# ================================================================ platform
def test_nat64_mapped_internal_addresses_are_not_public():
    assert not security._is_public_ip("64:ff9b::7f00:1")               # 127.0.0.1 behind NAT64
    assert security._is_public_ip("64:ff9b::0808:0808")                 # 8.8.8.8 behind NAT64
    assert security._is_public_ip("2606:4700::1111")


async def test_live_streams_are_capped_per_area(admin):
    subs = []
    try:
        for _ in range(state.MAX_STREAMS_PER_AREA):
            subs.append(state.subscribe(1))
        with pytest.raises(state.TooManyStreams):
            state.subscribe(1)
    finally:
        for s in subs:
            state.unsubscribe(s, 1)


async def test_a_deeply_nested_body_is_a_400_on_every_json_endpoint(client):
    body = "[" * 20000 + "]" * 20000
    r = await client.post("/api/settings", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "deeply" in r.text


def test_config_view_is_the_cache_itself(admin):
    with context.use_area(1):
        config.save_settings({"trading_enabled": True})
        v = config.view()
        assert v is config.view() and v.get("trading_enabled") is True


# ================================================================ account size (alpha.91)
def test_size_tier_rounds_to_the_usual_prop_sizes():
    from app import sizing
    assert sizing.size_tier(51240) == {"tier": "50K", "size": 50000, "exact": True}      # an eval account drifting with its P&L
    assert sizing.size_tier(148000) == {"tier": "150K", "size": 150000, "exact": True}
    assert sizing.size_tier(12340) == {"tier": "≈12K", "size": 12000, "exact": False}    # a live account: coarse, and marked so
    assert sizing.size_tier(0) is None and sizing.size_tier(None) is None and sizing.size_tier("x") is None


def test_sizing_suggestion_keeps_the_same_risk_share():
    from app import sizing
    assert sizing.suggest_sizing(50000, 150000) == {"ratio": 0.33, "multiplier": 0.25, "fixed": 1}
    assert sizing.suggest_sizing(100000, 50000) == {"ratio": 2.0, "multiplier": 2.0, "fixed": 2}
    assert sizing.suggest_sizing(50000, None) is None


async def test_trade_accounts_carry_a_size_tier_and_the_listing_the_leaders(client, monkeypatch):
    from app.routers.accounts import trade_accounts_overview
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L", "environment": "demo", "enabled": True, "access_token": "t", "lid": "l1",
                                                  "accounts": [{"id": 11, "spec": "APEX-1", "enabled": True}, {"id": 12, "spec": "APEX-2", "enabled": True}]}]})
        state.set_pnl({"accounts": [{"account_id": 11, "spec": "APEX-1", "realized": 0, "open": 0, "week": 0, "cash": 150400.0},
                                    {"account_id": 12, "spec": "APEX-2", "realized": 0, "open": 0, "week": 0, "cash": 49800.0}],
                       "realized": 0, "open": 0, "week": 0, "cash": 200200.0, "error": ""}, area_id=1)
        rows = {a["spec"]: a for a in trade_accounts_overview()}
    assert rows["APEX-1"]["tier"]["tier"] == "150K" and rows["APEX-1"]["balance"] == 150400.0
    assert rows["APEX-2"]["tier"] == {"tier": "50K", "size": 50000, "exact": True}
    r = await client.get("/api/trade-accounts")
    assert r.status_code == 200 and [a["tier"]["tier"] for a in r.json()] == ["150K", "50K"]
    g = {**cp.new_group("g"), "leader": {"token_idx": 0, "lid": "l1", "spec": "APEX-1", "account_id": 11}, "followers": []}
    view = cp.public_view(g, 1, email="pub@example.com")
    assert view["leader_tier"] == "150K" and view["leader_size"] == 150000     # coarse, never the balance itself
    assert "balance" not in view and "150400" not in str(view)


# ================================================================ sized-for reference (alpha.92)
async def test_webhook_sized_for_reaches_the_listing_and_the_record(client, monkeypatch):
    r = await client.post("/api/webhooks", json={"name": "S", "strategy": "simple", "default_qty": 2, "sized_for_k": 50})
    wid = r.json()["id"]
    assert r.status_code == 200 and r.json()["sized_for_k"] == 50
    r = await client.put(f"/api/webhooks/{wid}", json={"sized_for_k": "abc"})
    assert r.status_code == 400
    r = await client.put(f"/api/webhooks/{wid}", json={"sized_for_k": 100})
    assert r.json()["sized_for_k"] == 100
    with context.use_area(1):
        wh = next(w for w in config.load_settings()["webhooks"] if w["id"] == wid)
        assert marketplace.public_view(wh, 1, publisher_email="p@x")["sized_for_k"] == 100
        from app import track_record
        rec = track_record.webhook_record(1, wh, detail=False)
    assert rec["size"] == 100000                                       # the figures can be shown as a share of it


async def test_copy_record_carries_the_leaders_coarse_size(admin):
    from app import track_record
    with context.use_area(1):
        state.set_pnl({"accounts": [{"account_id": 11, "spec": "L", "realized": 0, "open": 0, "week": 0, "cash": 49900.0}],
                       "realized": 0, "open": 0, "week": 0, "cash": 49900.0, "error": ""}, area_id=1)
        g = {**cp.new_group("g"), "id": "grec", "leader": {"token_idx": 0, "spec": "L", "account_id": 11}, "followers": []}
        assert track_record.copy_record(1, g, detail=False)["size"] == 50000
        state.set_pnl({"accounts": [{"account_id": 11, "spec": "L", "realized": 0, "open": 0, "week": 0, "cash": 12340.0}],
                       "realized": 0, "open": 0, "week": 0, "cash": 12340.0, "error": ""}, area_id=1)
        g["id"] = "grec2"
        assert track_record.copy_record(1, g, detail=False)["size"] is None      # a drifting live balance is no basis for a percentage

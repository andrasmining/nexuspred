"""alpha.84: review round 6 — red-team fixes (tenant-safe copy status, a
throttled Rithmic gateway lookup, ACL removal releases followers, one update
at a time, the subscribing user on the subscription row)."""
from __future__ import annotations

import asyncio

import pytest

from app import config, context, db, rithmic, updater
from app import copy as cp
from app.routers import accounts as accounts_router
from app.tradovate import TradovateError
from tests.helpers import FakeExecutor


# ---------------------------------------------------------------- copy: no account names across tenants
class _Sess:
    def __init__(self, rows):
        self.rows = rows

    async def positions_snapshot(self):
        return [dict(x) for x in self.rows]


async def _runner_with_external(monkeypatch):
    g = cp.new_group("G")
    g.update({"enabled": True, "leader": {"token_idx": 0, "spec": "L", "account_id": 1},
              "followers": [cp.normalize_follower({"token_idx": 1, "spec": "OWN-1", "account_id": 2})]})
    ext = [{**cp.normalize_follower({"token_idx": 0, "spec": "APEX-999", "account_id": 3}), "external": True, "area_id": 7, "sub_id": 42}]
    monkeypatch.setattr(cp.groups, "external_followers", lambda *a, **k: ext)
    r = cp.GroupRunner(1, g)
    r.refresh_followers  # exists
    r.external, r.followers = ext, cp.effective_followers(1, g, ext)
    r._area_by_spec = {"OWN-1": 1, "APEX-999": 7}
    execs = {}
    for spec, aid in (("OWN-1", 2), ("APEX-999", 3)):
        ex = FakeExecutor(spec); ex.id, ex.session = aid, _Sess([{"accountId": aid, "contractId": 5, "netPos": 1}])
        execs[spec] = ex
    monkeypatch.setattr(r, "_executor", lambda f: execs[str(f["spec"])])
    monkeypatch.setattr(r, "_persist", lambda cid: None)
    monkeypatch.setattr(cp.group_runner, "FLATTEN_VERIFY_DELAY_S", 0.0)

    async def cancel_all(**kw):
        return 0
    monkeypatch.setattr(r.orders, "cancel_all", cancel_all)
    r.leader_net[5], r.unit[5], r.contract_names[5] = 1, 1, "MNQ"
    r.follower_pos[("OWN-1", 5)] = r.follower_pos[("APEX-999", 5)] = 1
    return r


async def test_copy_flatten_status_never_shows_a_subscribers_account_to_others(admin, monkeypatch):
    r = await _runner_with_external(monkeypatch)
    await r.flatten_followers(reason="feed lost")
    r.last_frame, r.feed_ok, r.tasks = 1.0, False, [object()]
    monkeypatch.setattr(r, "flatten_followers", lambda **kw: asyncio.sleep(0, result=2))
    await r.watchdog()
    st = r.status()
    publisher_view = cp.masked_status(st)
    assert "APEX-999" not in str(publisher_view) and "APEX-999" not in r.pause_reason
    assert publisher_view["flatten_unresolved"] == ["OWN-1 MNQ: broker still shows +1 after the close",
                                                    "subscriber #42 MNQ: broker still shows +1 after the close"]
    assert "not confirmed flat" in r.pause_reason and "OWN-1" not in r.pause_reason
    # the subscriber sees their own account only
    assert r.unresolved_for(7, {"APEX-999"}) == ["APEX-999 MNQ: broker still shows +1 after the close"]
    assert r.unresolved_for(1, {"OWN-1"}) == ["OWN-1 MNQ: broker still shows +1 after the close"]
    assert r.unresolved_for(7, {"OTHER"}) == []


async def test_acl_removal_releases_the_accounts_that_left_the_mirror(admin, monkeypatch):
    released = []

    async def rec(area, gid, specs):
        released.append(sorted(specs)); return 0
    monkeypatch.setattr(cp.manager, "release_followers", rec)
    g = cp.new_group("G"); g.update({"enabled": True, "leader": {"token_idx": 0, "spec": "L", "account_id": 1}})
    config.save_settings({"copy_groups": [g]}, area_id=1)
    ext_now = [[{"token_idx": 0, "spec": "S1", "enabled": True, "external": True, "area_id": 7, "sub_id": 1, "mode": "multiplier", "multiplier": 1},
                {"token_idx": 0, "spec": "S2", "enabled": True, "external": True, "area_id": 8, "sub_id": 2, "mode": "multiplier", "multiplier": 1}]]
    monkeypatch.setattr(cp.manager, "external_followers", lambda *a, **k: [dict(x) for x in ext_now[0]])

    class R:
        def __init__(self):
            self.fingerprint = ("x", "old"); self.external = [dict(x) for x in ext_now[0]]; self.refreshed = []
            self.group = g

        async def refresh_followers(self, external):
            self.refreshed.append([f["spec"] for f in external]); self.fingerprint = (self.fingerprint[0], "new")

        async def stop(self):
            pass
    r = R()
    import json
    r.fingerprint = (json.dumps(g, sort_keys=True), "old")
    cp._runners[(1, g["id"])] = r
    try:
        ext_now[0] = ext_now[0][:1]                                    # S2 was removed from the listing's user list
        await cp.sync_area(1)
    finally:
        cp._runners.pop((1, g["id"]), None)
    assert released == [["S2"]] and r.refreshed == [["S1"]]


# ---------------------------------------------------------------- rithmic gateway lookup hardening
def test_gateway_url_is_a_host_not_a_probe():
    ok = rithmic.gateway_allowed
    assert ok("wss://rprotocol.rithmic.com:443") and ok("wss://rprotocol.rithmic.com") and ok("wss://rprotocol.rithmic.com:443/")
    assert not ok("wss://rprotocol.rithmic.com:22") and not ok("wss://rprotocol.rithmic.com:443/probe")
    assert not ok("wss://rprotocol.rithmic.com:443/?q=1") and not ok("wss://u:p@rprotocol.rithmic.com:443") and not ok("wss://rprotocol.rithmic.com:443#f")


async def test_systems_lookup_is_throttled_per_user_and_reports_generically(client, admin, monkeypatch):
    calls = {"n": 0}

    async def fake(gateway, *, environment="demo", fresh=False):
        calls["n"] += 1
        raise TradovateError("Rithmic gateway wss://x not reachable: OSError: internal detail")
    monkeypatch.setattr(rithmic, "list_systems", fake)
    monkeypatch.setattr(accounts_router, "_SYSTEMS_LIMIT", accounts_router.security.RateLimiter(2, 60))
    r = await client.get("/api/rithmic/systems?gateway=chicago")
    assert r.status_code == 502 and "internal detail" not in r.json()["detail"] and "type the system name" in r.json()["detail"]
    await client.get("/api/rithmic/systems?gateway=chicago")
    r = await client.get("/api/rithmic/systems?gateway=chicago")
    assert r.status_code == 429 and r.headers.get("retry-after") == "60" and calls["n"] == 2


async def test_systems_cache_is_bounded_and_sockets_are_limited(monkeypatch):
    from async_rithmic.protocol_buffers import response_rithmic_system_info_pb2 as rs
    rithmic._systems_cache.clear()
    monkeypatch.setattr(rithmic, "SYSTEMS_CACHE_MAX", 3)
    monkeypatch.setattr(rithmic, "_systems_sem", asyncio.Semaphore(2))
    live = {"now": 0, "peak": 0}

    class WS:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            live["now"] += 1; live["peak"] = max(live["peak"], live["now"]); await asyncio.sleep(0.02)

        async def recv(self):
            resp = rs.ResponseRithmicSystemInfo(); resp.template_id = 17; resp.rp_code.append("0"); resp.system_name.append("Apex")
            body = resp.SerializeToString(); live["now"] -= 1
            return len(body).to_bytes(4, "big", signed=True) + body

        async def close(self):
            pass

    async def connect(url):
        return WS()
    monkeypatch.setattr(rithmic, "_ws_connect", connect)
    hosts = [f"wss://gw{i}.rithmic.com:443" for i in range(5)]
    await asyncio.gather(*(rithmic.list_systems(h) for h in hosts))
    assert live["peak"] <= 2 and len(rithmic._systems_cache) == 3            # never more sockets than the semaphore allows; bounded cache


# ---------------------------------------------------------------- updater
async def test_only_one_update_runs_at_a_time(admin, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    started = asyncio.Event()

    async def slow(*a, **k):
        started.set(); await asyncio.sleep(0.2); return {"success": True, "message": "done"}
    monkeypatch.setattr(updater, "_apply_update", slow)
    first = asyncio.ensure_future(updater.apply_update())
    await started.wait()
    second = await updater.apply_update()
    assert second["success"] is False and "already running" in second["message"]
    assert (await first)["success"] is True


async def test_update_check_is_admin_only(admin):
    from app import auth
    from tests.conftest import _make_client, enrolled
    u = db.create_user("plain@example.com", "password123", is_admin=False)
    enrolled(u["id"])
    async with _make_client(auth.make_session(u["id"])) as c:
        assert (await c.get("/api/update/check")).status_code == 403


# ---------------------------------------------------------------- the subscribing user
async def test_subscription_row_stores_the_subscribing_user(admin):
    db.upsert_subscription(1, 1, "wh_p", [], True, user_id=1)
    rows = db.active_subscriptions(1, "wh_p")
    assert rows[0]["user_id"] == 1
    db.upsert_subscription(1, 1, "wh_p", [], True)                       # an update without a user keeps the stored one
    assert db.active_subscriptions(1, "wh_p")[0]["user_id"] == 1
    assert "user_id" not in db.list_subscribers(1, "wh_p")[0]            # the publisher's list carries the email, not the id


# ================================================================ engine review round 6
import time as _time

from app import broker, events, signals, tradovate
from app.engine import bracket, common, manage
from app.tradovate import OrderOutcomeUnknown, RateLimited
from tests.test_strategies import ENTRY, active, live, wh  # noqa: F401 - fixtures


def _limited(retry_after=0.05):
    return RateLimited("/order/placeorder", "", retry_after)


async def test_failed_stop_resolution_waits_a_penalty_out_and_closes(live, monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "execution_problem", lambda *a, **k: asyncio.sleep(0))

    class Penalised(FakeExecutor):
        markets = 0

        async def place_order(self, **kw):
            if kw.get("order_type") == "Stop":
                raise _limited()                                     # the stop is refused by a running penalty
            if kw.get("order_type") == "Market" and kw.get("action") == "Buy":
                Penalised.markets += 1
                if Penalised.markets == 1:
                    raise _limited()                                 # the first close attempt is refused too
            return await super().place_order(**kw)
    a = Penalised("A")
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 110.0}, wh("bracket", id="wh_pen"))
    assert Penalised.markets == 2                                     # waited the penalty out, then closed
    assert r["failed"] == ["A"] and "unprotected" not in r and "wh_pen:MNQ" not in active()


async def test_an_unprotected_position_is_named_in_the_result_and_counts_as_an_error(live, monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "execution_problem", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(common, "RESOLUTION_PENALTY_WAIT_S", 0.01)

    class Naked(FakeExecutor):
        async def place_order(self, **kw):
            if kw.get("order_type") == "Stop" or (kw.get("order_type") == "Market" and kw.get("action") == "Buy"):
                raise TradovateError("rejected")
            return await super().place_order(**kw)
    a = Naked("A")
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 110.0}, wh("bracket", id="wh_nkd"))
    assert r["status"] == "ok" and r["unprotected"] == ["A"] and "failed" not in r
    assert active("wh_nkd:MNQ")["accounts"]["A"]["sl_order_id"] is None and active("wh_nkd:MNQ")["accounts"]["A"]["unprotected"] is True
    # the marketplace error streak sees it
    with context.use_area(1):
        note = signals._note_subscription_outcome({"subscription": {"id": 5}, "controls": {"pause_after_errors": 2}}, None, r)
        assert note is None and signals._sub_errors[(1, 5)] == 1


async def test_set_sl_tp_fallback_confirms_a_refused_old_stop_against_the_broker(admin):
    class Stale(FakeExecutor):                                      # the tracked id is stale: modify and cancel are refused, the broker no longer lists it
        async def modify_order(self, order_id, **kw):
            raise TradovateError("order not modifiable")

        async def cancel_order(self, order_id):
            if order_id == 7:
                raise TradovateError("order 7 not found")
            return await super().cancel_order(order_id)
    ex = Stale("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    from tests.test_alpha82 import WH, _tracked
    am = _tracked()
    r = await manage.handle_set_sl_tp({"stop_price": 95.0}, "MNQ", "MNQ", [ex], am, "", WH)
    assert r["status"] == "ok" and "extra_stop_ids" not in am["wh:MNQ"]["accounts"]["A"]   # one stop, correctly protected: no error

    class Alive(Stale):                                             # the old stop is still working: remembered, retired by the next change
        async def working_orders(self):
            return [{"id": 7, "symbol": "MNQ"}]
    ex = Alive("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    am = _tracked()
    r = await manage.handle_set_sl_tp({"stop_price": 95.0}, "MNQ", "MNQ", [ex], am, "", WH)
    assert r["status"] == "error" and r["failed"] == ["A"] and am["wh:MNQ"]["accounts"]["A"]["extra_stop_ids"] == [7]


async def test_move_sl_retires_a_stop_a_repair_left_behind(admin):
    ex = FakeExecutor("A")
    am = {"wh:MNQ": {"webhook_id": "wh", "root": "MNQ", "side": "sell", "ts": 0.0,
                     "accounts": {"A": {"name": "A", "contract": "MNQ", "entry_qty": 2, "tp_qty": 1, "qty": 2, "sl_order_id": 9, "sl_stop": 110.0, "extra_stop_ids": [7]}}}}
    r = await bracket.handle_move_sl({"new_sl": 105.0, "symbol": "MNQ1!"}, "MNQ", [ex], am, "", {"id": "wh", "name": "t"}, settings={"breakeven_to_entry": False})
    assert r["status"] == "ok" and ex.of("cancel") == [{"order_id": 7}] and "extra_stop_ids" not in am["wh:MNQ"]["accounts"]["A"]


async def test_trail_active_after_the_last_target_untracks_the_flat_trade(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=2, tp_qty=1, id="wh_flat")
    await signals.process({**ENTRY, "qty": 2, "tp1": 97.0, "tp2": 94.0, "sl": 110.0}, w)
    r = await signals.process({"action": "trail_active", "symbol": "MNQ1!", "event": "tp2_hit"}, w)
    assert r["status"] == "ok" and "wh_flat:MNQ" not in active()


async def test_flatten_waits_a_penalty_on_the_position_read_out(admin):
    class Ex:
        name = "A"

        def __init__(self):
            self.reads = 0
            self.liquidates = []

        async def working_orders(self):
            return []

        async def positions(self):
            self.reads += 1
            if self.reads == 1:
                raise _limited()
            return [{"symbol": "MNQ", "netPos": 1}]

        async def liquidate_position(self, symbol):
            self.liquidates.append(symbol)
            return {}
    ex = Ex()
    cancelled, flattened, errors = await common._flatten_account(ex)
    assert flattened == 1 and errors == [] and ex.liquidates == ["MNQ"] and ex.reads == 2


async def test_flatten_never_guesses_an_unreadable_row_as_flat_or_as_a_quantity(admin):
    class Ex:
        name = "A"

        def __init__(self):
            self.reads = 0

        async def working_orders(self):
            return []

        async def positions(self):
            self.reads += 1
            return [{"symbol": "MNQ", "netPos": 1}] if self.reads == 1 else [{"symbol": "MNQ", "netPos": "n/a"}]

        async def liquidate_position(self, symbol):
            raise TradovateError("nope")
    _, flattened, errors = await common._flatten_account(Ex())
    assert flattened == 0 and errors == ["flatten MNQ: nope — position could not be re-read"]


async def test_background_tasks_never_inherit_the_urgent_lane(admin):
    seen = {}

    async def handler(e):
        seen["events"] = broker.is_urgent()
    events.subscribe("lane.test", handler)

    async def bg():
        seen["spawn"] = broker.is_urgent()
    with broker.urgent():
        events.emit("lane.test", x=1)
        t = signals._spawn(bg())
        assert broker.is_urgent()
    await asyncio.sleep(0.01)
    await t
    assert seen == {"events": False, "spawn": False} and not broker.is_urgent()


async def test_login_wide_reads_are_coalesced_and_never_stale(admin, monkeypatch):
    sess = tradovate.TradovateSession(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"id": 11, "spec": "A", "enabled": True}]}, area_id=1)
    calls = []

    async def raw(method, path, **kw):
        calls.append(path); await asyncio.sleep(0.02); return [{"id": len(calls)}]
    monkeypatch.setattr(sess, "_request_raw", raw)
    monkeypatch.setattr(tradovate, "PRIORITY_SPACING_S", 0.0)
    with broker.urgent():
        rows = await asyncio.gather(*(sess._request("GET", "/position/list") for _ in range(5)))
    assert calls == ["/position/list"] and all(r == [{"id": 1}] for r in rows)         # five callers, one read
    calls.clear()

    async def late():
        await asyncio.sleep(0.005)                                   # arrives while the first read is in flight
        return await sess._request("GET", "/order/list")
    with broker.urgent():
        first, second = await asyncio.gather(sess._request("GET", "/order/list"), late())
    assert calls == ["/order/list", "/order/list"] and first == [{"id": 1}] and second == [{"id": 2}]   # a fresh read for the late caller


async def test_projectx_positions_of_one_account_cost_one_request(admin, monkeypatch):
    from app import projectx
    sess = projectx.ProjectXSession(0, {"name": "PX", "broker": "projectx", "px_user": "u", "px_api_key": "k", "enabled": True,
                                        "accounts": [{"id": 1, "spec": "P1", "enabled": True}, {"id": 2, "spec": "P2", "enabled": True}, {"id": 3, "spec": "P3", "enabled": True}]}, area_id=1)
    posts = []

    async def post(path, body, **kw):
        posts.append((path, body.get("accountId")))
        return {"positions": [{"contractId": "CON.F.US.MNQ.Z25", "size": 2, "type": 1, "averagePrice": 100.0}]}
    monkeypatch.setattr(sess, "_post", post)
    monkeypatch.setattr(sess, "_contract_name", lambda gid: asyncio.sleep(0, result="MNQZ5"))
    monkeypatch.setattr(sess, "contract_info", lambda cid: asyncio.sleep(0, result={"name": "MNQZ5"}))
    rows = await sess.positions(account_id=2)
    assert posts == [("/api/Position/searchOpen", 2)] and rows[0]["netPos"] == 2 and rows[0]["symbol"] == "MNQZ5"

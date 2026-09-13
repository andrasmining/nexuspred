"""alpha.82: the parts of PR #21 adopted without its policy reversals, and the
parallel execution path (urgent read lane, concurrent flattens and closes)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app import automations, broker, config, context, db, marketplace, projectx, risk, signals, state, tradovate, updater
from app import copy as cp
from app.engine import common, manage, ts_hunter
from app.routers import settings_io
from app.tradovate import OrderOutcomeUnknown, RateLimited, TradovateError
from tests.helpers import FakeExecutor, settle
from tests.test_strategies import ENTRY, active, live, wh  # noqa: F401 - fixtures


# ================================================================ engine
async def test_unknown_stop_outcome_is_not_retried_but_resolved_by_cancel_and_close(live, monkeypatch):
    class Unknown(FakeExecutor):
        stops = 0

        async def place_order(self, **kw):
            if kw.get("order_type") == "Stop":
                Unknown.stops += 1
                raise OrderOutcomeUnknown("answer lost")
            return await super().place_order(**kw)

    async def rec(*a, **k):
        pass
    from app import alerts
    monkeypatch.setattr(alerts, "execution_problem", rec)
    a = Unknown("A", working=[{"id": 41, "symbol": "MNQU6"}])
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 90.0}, wh("bracket", id="wh_u82"))
    await settle()
    assert Unknown.stops == 1                                        # never a second stop that could double the protection
    assert [c["order_id"] for c in a.of("cancel")] == [41]           # the contract's orders go (the stop may be working)
    assert a.of("place")[-1]["order_type"] == "Market"               # and the entry is closed again (alpha.73 policy)
    assert "wh_u82:MNQ" not in active() and r["failed"] == ["A"] and r["status"] == "ok"


class _Flat:
    name = "A"

    def __init__(self, snapshots, *, raise_=None):
        self.snapshots = list(snapshots)
        self.raise_ = raise_
        self.liquidates: list[str] = []
        self.reads = 0

    async def working_orders(self):
        return []

    async def positions(self):
        self.reads += 1
        return [dict(x) for x in (self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0])]

    async def liquidate_position(self, symbol):
        self.liquidates.append(symbol)
        if self.raise_:
            raise self.raise_
        return {}


async def test_flatten_account_liquidates_each_symbol_once_and_trusts_the_broker_after_a_lost_answer(admin):
    ex = _Flat([[{"symbol": "MNQ", "netPos": 1}, {"symbol": "MNQ", "netPos": 2}], []], raise_=OrderOutcomeUnknown("lost"))
    cancelled, flattened, errors = await common._flatten_account(ex)
    assert ex.liquidates == ["MNQ"] and flattened == 1 and errors == [] and cancelled == 0


async def test_flatten_account_reports_the_residual_after_a_rejected_liquidation(admin):
    ex = _Flat([[{"symbol": "MNQ", "netPos": 1}]], raise_=TradovateError("nope"))
    _, flattened, errors = await common._flatten_account(ex)
    assert flattened == 0 and errors == ["flatten MNQ: nope — broker still reports +1"]


async def test_flatten_account_reads_positions_once_on_the_happy_path(admin):
    ex = _Flat([[{"symbol": "MNQ", "netPos": 1}, {"symbol": "ES", "netPos": -1}]])
    cancelled, flattened, errors = await common._flatten_account(ex)
    assert flattened == 2 and errors == [] and ex.reads == 1 and sorted(ex.liquidates) == ["ES", "MNQ"]


def _tracked(stop_id=7, tps=None):
    return {"wh:MNQ": {"webhook_id": "wh", "root": "MNQ", "side": "buy", "ts": 0.0,
                       "accounts": {"A": {"name": "A", "contract": "MNQ", "qty": 2, "sl_order_id": stop_id, "sl_stop": 90.0,
                                          "tp_order_ids": list(tps or [])}}}}


WH = {"id": "wh", "name": "t"}


async def test_set_sl_tp_modifies_the_tracked_stop_in_place(admin):
    ex = FakeExecutor("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    am = _tracked()
    r = await manage.handle_set_sl_tp({"stop_price": 95.0}, "MNQ", "MNQ", [ex], am, "", WH)
    assert r["status"] == "ok" and r["accounts"] == 1
    assert ex.of("modify") == [{"order_id": 7, "qty": 2, "order_type": "Stop", "stop_price": 95.0}]
    assert ex.of("place") == [] and ex.of("cancel") == []
    assert am["wh:MNQ"]["accounts"]["A"]["sl_order_id"] == 7 and am["wh:MNQ"]["accounts"]["A"]["sl_stop"] == 95.0


async def test_set_sl_tp_falls_back_to_a_fresh_stop_when_the_modify_is_rejected(admin):
    class Rejects(FakeExecutor):
        async def modify_order(self, order_id, **kw):
            raise TradovateError("order not modifiable")
    ex = Rejects("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    am = _tracked()
    r = await manage.handle_set_sl_tp({"stop_price": 95.0}, "MNQ", "MNQ", [ex], am, "", WH)
    assert r["status"] == "ok"
    placed = ex.of("place")[-1]
    assert placed["order_type"] == "Stop" and placed["stop_price"] == 95.0 and placed["action"] == "Sell"
    assert ex.of("cancel") == [{"order_id": 7}]                       # the old one is retired after the new one works
    assert am["wh:MNQ"]["accounts"]["A"]["sl_order_id"] == placed["order_id"]


async def test_set_sl_tp_keeps_the_old_stop_when_the_modify_answer_is_lost(admin):
    class Lost(FakeExecutor):
        async def modify_order(self, order_id, **kw):
            raise OrderOutcomeUnknown("timeout")
    ex = Lost("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    am = _tracked()
    r = await manage.handle_set_sl_tp({"stop_price": 95.0}, "MNQ", "MNQ", [ex], am, "", WH)
    assert r["status"] == "error" and r["failed"] == ["A"] and r["reason"] == "protection_update_failed"
    assert ex.of("place") == [] and am["wh:MNQ"]["accounts"]["A"]["sl_order_id"] == 7   # never a second stop


async def test_set_sl_tp_reports_an_account_whose_positions_cannot_be_read(admin):
    class Blind(FakeExecutor):
        async def positions(self):
            raise TradovateError("503")
    r = await manage.handle_set_sl_tp({"stop_price": 95.0}, "MNQ", "MNQ", [Blind("A")], {}, "", WH)
    assert r["status"] == "error" and r["failed"] == ["A"] and r["reason"] == "protection_update_failed"


async def test_set_sl_tp_rolls_back_the_replacement_target_when_the_old_one_will_not_cancel(admin):
    class OldSticks(FakeExecutor):
        async def cancel_order(self, order_id):
            if order_id == 5:
                raise TradovateError("cancel refused")
            return await super().cancel_order(order_id)
    ex = OldSticks("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    am = _tracked(stop_id=None, tps=[5])
    r = await manage.handle_set_sl_tp({"target_price": 120.0}, "MNQ", "MNQ", [ex], am, "", WH)
    new_id = ex.of("place")[-1]["order_id"]
    assert r["status"] == "error" and r["failed"] == ["A"]
    assert [c["order_id"] for c in ex.of("cancel")] == [new_id]        # old refused (not recorded) → the replacement is taken back
    assert am["wh:MNQ"]["accounts"]["A"]["tp_order_ids"] == [5]        # one target working, the old one


async def test_trail_active_retires_the_stop_when_the_last_target_filled(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=2, tp_qty=1, id="wh_tr82")
    await signals.process({**ENTRY, "qty": 2, "tp1": 97.0, "tp2": 94.0, "sl": 110.0}, w)
    sl_id = [p for p in a.of("place") if p["order_type"] == "Stop"][0]["order_id"]
    r = await signals.process({"action": "trail_active", "symbol": "MNQ1!", "event": "tp2_hit"}, w)
    assert r["status"] == "ok" and r["accounts"] == 1
    assert a.of("cancel")[-1] == {"order_id": sl_id} and not any(m["order_id"] == sl_id and m["qty"] == 0 for m in a.of("modify"))
    assert active("wh_tr82:MNQ")["accounts"]["A"]["sl_order_id"] is None


async def test_entry_partial_failure_stays_ok_and_names_the_failed_account(live):
    a, b = FakeExecutor("A"), FakeExecutor("B", fail_place=True)
    live.use(a, b)
    r = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh(id="wh_pf82"))
    assert r["status"] == "ok" and r["failed"] == ["B"] and r["accounts"] == [{"account": "A", "qty": 1}]
    r = await signals.process({**ENTRY, "sl": 90.0}, wh("bracket", id="wh_pf82b"))
    assert r["status"] == "ok" and r["failed"] == ["B"] and [x["account"] for x in r["accounts"]] == ["A"]
    r = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh(id="wh_pf82c"))
    live.use(a)
    r = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh(id="wh_ok82"))
    assert "failed" not in r


async def test_move_sl_failure_is_reported_per_account(live):
    class Stuck(FakeExecutor):
        async def modify_order(self, order_id, **kw):
            raise TradovateError("modify rejected")
    a, b = FakeExecutor("A"), Stuck("B")
    live.use(a, b)
    w = wh("bracket", id="wh_mv82")
    await signals.process({**ENTRY, "sl": 110.0}, w)
    r = await signals.process({"action": "move_sl", "symbol": "MNQ1!", "new_sl": 105.0}, w)
    assert r["status"] == "error" and r["failed"] == ["B"] and r["accounts"] == 1 and r["new_sl"] == 105.0


async def test_partial_close_marks_an_account_whose_stop_could_not_follow(admin):
    class Stuck(FakeExecutor):
        async def modify_order(self, order_id, **kw):
            raise TradovateError("modify rejected")
    ex = Stuck("A")
    am = {"T1": {"side": "buy", "accounts": {"A": {"contract": "MNQ", "remaining_qty": 2, "qty": 2, "sl_order_id": 9, "sl_stop": 100.0}}}}
    r = await ts_hunter.handle_partial_close({"percent": 50}, "T1", [ex], am, "")
    assert r["status"] == "error" and r["failed"] == ["A"] and len(r["orders"]) == 1      # the close went out, the stop did not follow
    assert am["T1"]["accounts"]["A"]["remaining_qty"] == 1


# ================================================================ signals / marketplace
def _forwarding(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(signals, "_spawn", lambda coro: (coro.close(), None)[1])
    monkeypatch.setattr(state, "log_signal", lambda *a, **k: seen.append(context.get_area()) or {})
    return seen


def test_fan_out_rechecks_a_selected_listing_against_the_current_user_list(admin, monkeypatch):
    webhook = {"id": "wh_sel", "name": "sel", "strategy": "simple",
               "sharing": {"enabled": True, "visibility": "selected", "allowed_user_ids": [7]}}
    subs = [{"id": 1, "area_id": 101, "publisher_area_id": 1, "webhook_id": "wh_sel", "enabled": True, "accounts": [], "status": "active", "controls": {}, "user_id": 7},
            {"id": 2, "area_id": 102, "publisher_area_id": 1, "webhook_id": "wh_sel", "enabled": True, "accounts": [], "status": "active", "controls": {}, "user_id": 8},
            {"id": 3, "area_id": 103, "publisher_area_id": 1, "webhook_id": "wh_sel", "enabled": True, "accounts": [], "status": "active", "controls": {}}]
    monkeypatch.setattr(db, "active_subscriptions", lambda pa, wid: [dict(s) for s in subs])
    seen = _forwarding(monkeypatch)
    with context.use_area(1):
        assert signals.forward_to_subscribers({"action": "buy", "symbol": "MNQ1!"}, webhook) == 1
    assert seen == [101]                                              # removed (8) and unknown (none) users get nothing
    webhook["sharing"]["visibility"] = "all"
    seen.clear()
    with context.use_area(1):
        assert signals.forward_to_subscribers({"action": "buy", "symbol": "MNQ1!"}, webhook) == 3
    assert sorted(seen) == [101, 102, 103]


def test_subscription_allowed_is_read_free_and_fails_closed_on_an_unknown_user():
    sel = {"visibility": "selected", "allowed_user_ids": [7]}
    assert marketplace.subscription_allowed(sel, {"user_id": 7}) and not marketplace.subscription_allowed(sel, {"user_id": 8})
    assert not marketplace.subscription_allowed(sel, {}) and not marketplace.subscription_allowed(sel, {"user_id": "x"})
    assert marketplace.subscription_allowed({"visibility": "all"}, {}) is True


def test_active_subscriptions_carry_the_subscribers_user_id(admin):
    db.upsert_subscription(1, 1, "wh_uid", [], True)
    rows = db.active_subscriptions(1, "wh_uid")
    assert len(rows) == 1 and rows[0]["user_id"] == 1                  # the owner of workspace 1


def test_copy_followers_removed_from_a_selected_listing_leave_the_mirror(admin, monkeypatch):
    g = cp.new_group("Lead")
    g["sharing"] = {"enabled": True, "title": "Lead", "visibility": "selected", "allowed_user_ids": [7]}
    acc = [{"token_idx": 0, "lid": "l", "spec": "S1", "enabled": True, "mode": "multiplier", "multiplier": 1}]
    subs = [{"id": 1, "area_id": 101, "publisher_area_id": 1, "webhook_id": f"copy:{g['id']}", "enabled": True, "accounts": acc, "status": "active", "user_id": 7},
            {"id": 2, "area_id": 102, "publisher_area_id": 1, "webhook_id": f"copy:{g['id']}", "enabled": True, "accounts": acc, "status": "active", "user_id": 8}]
    monkeypatch.setattr(db, "active_subscriptions", lambda pa, wid: [dict(s) for s in subs])
    ext = cp.external_followers(1, g["id"], group=g)
    assert [f["area_id"] for f in ext] == [101]


async def test_fan_out_runs_only_after_the_publishers_task_took_its_first_step(admin, monkeypatch):
    order: list[str] = []

    async def fake_process(payload, webhook, **kw):
        order.append("publisher")
        await asyncio.sleep(0)
        order.append("publisher-2")

    monkeypatch.setattr(signals, "process_background", fake_process)
    monkeypatch.setattr(signals, "forward_to_subscribers", lambda *a, **k: order.append("fan-out") or 0)
    with context.use_area(1):
        config.save_settings({"trading_enabled": True})
        signals.accept({"action": "buy", "symbol": "MNQ1!"}, {"id": "wh_o", "name": "o", "enabled": True, "strategy": "simple", "accounts": []})
    await settle()
    assert order == ["publisher", "fan-out", "publisher-2"]


# ================================================================ copy feed-loss flatten
class _CopySess:
    def __init__(self, rows):
        self.rows = rows

    async def positions_snapshot(self):
        return [dict(x) for x in self.rows]


async def _runner(monkeypatch, after_rows):
    g = cp.new_group("G")
    g.update({"enabled": True, "leader": {"token_idx": 0, "spec": "L", "account_id": 1},
              "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F", "account_id": 2})]})
    ex = FakeExecutor("F")
    ex.id, ex.session = 2, _CopySess([{"accountId": 2, "contractId": 5, "netPos": 1}])
    r = cp.GroupRunner(1, g)
    monkeypatch.setattr(r, "_executor", lambda f: ex)
    monkeypatch.setattr(r, "_persist", lambda cid: None)
    monkeypatch.setattr(cp.group_runner, "FLATTEN_VERIFY_DELAY_S", 0.0)

    async def cancel_all(**kw):
        return 0
    monkeypatch.setattr(r.orders, "cancel_all", cancel_all)
    r.leader_net[5], r.unit[5], r.contract_names[5], r.follower_pos[("F", 5)] = 1, 1, "MNQ", 1
    real = ex.place_order

    async def place(**kw):
        ex.session.rows = after_rows                                     # the broker after the close
        return await real(**kw)
    ex.place_order = place
    return r, ex


async def test_copy_flatten_confirms_flat_followers_and_baselines_the_contract(admin, monkeypatch):
    r, ex = await _runner(monkeypatch, [])
    assert await r.flatten_followers(reason="feed lost") == 1
    assert r.flatten_unresolved == [] and 5 in r.baseline and r.follower_pos[("F", 5)] == 0
    assert r.status()["flatten_unresolved"] == []


async def test_copy_flatten_lists_a_follower_the_broker_still_shows_open(admin, monkeypatch):
    r, ex = await _runner(monkeypatch, [{"accountId": 2, "contractId": 5, "netPos": 1}])
    assert await r.flatten_followers(reason="feed lost") == 1           # one close, never a second one
    assert r.flatten_unresolved == ["F MNQ: broker still shows +1 after the close"]
    assert 5 in r.baseline and r.follower_pos[("F", 5)] == 0 and "MNQ" in r.follower_err["F"]
    assert len(ex.of("place")) == 1


# ================================================================ platform
def test_import_neutralises_publication_and_acl_while_the_export_keeps_them(admin):
    wh_ = config.new_webhook(name="shared")
    wh_["sharing"] = {"enabled": True, "visibility": "selected", "allowed_user_ids": [12, 34], "title": "T",
                      "published_at": "2026-01-01T00:00:00+00:00", "paused": True}
    config.save_settings({"webhooks": [wh_]})
    doc = settings_io.export_settings(1)
    exported = doc["settings"]["webhooks"][0]["sharing"]
    assert exported["enabled"] is True and exported["allowed_user_ids"] == [12, 34]      # the operator's own backup stays complete
    imported = settings_io._import_webhook(doc["settings"]["webhooks"][0], config.load_settings(), 1)["sharing"]
    assert imported["enabled"] is False and imported["allowed_user_ids"] == [] and imported["published_at"] == "" and imported["paused"] is False
    assert imported["title"] == "T" and imported["visibility"] == "selected"


async def test_updater_restores_the_checkout_and_schedules_no_restart_when_dependencies_fail(admin, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    monkeypatch.setattr(config, "get_version", lambda force=False: "1.0.0")
    restarts: list[int] = []
    monkeypatch.setattr(updater, "_restart", lambda: restarts.append(1))
    sha = "a" * 40
    calls: list[list[str]] = []
    pips = {"n": 0}

    def fake_run(cmd, timeout=120):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return True, sha + "\n"
        if "pip" in cmd:
            pips["n"] += 1
            assert timeout == updater.PIP_TIMEOUT_S
            return (False, "timed out after 900 s") if pips["n"] == 1 else (True, "")
        return True, "ok"
    monkeypatch.setattr(updater, "_run", fake_run)
    r = await updater.apply_update()
    assert r["success"] is False and "restored to the previous revision" in r["message"] and "timed out" in r["message"]
    assert ["git", "reset", "--hard", sha] in calls and pips["n"] == 2
    await asyncio.sleep(0)
    assert restarts == []


async def test_updater_refuses_to_start_without_a_verified_revision(admin, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=120: (calls.append(list(cmd)), (True, "fatal: not a commit"))[1])
    r = await updater.apply_update()
    assert r["success"] is False and "revision" in r["message"] and not any(c[:2] == ["git", "fetch"] for c in calls)


def test_run_reports_a_timeout_as_such(admin):
    ok, out = updater._run(["sleep", "3"], timeout=0.2)
    assert ok is False and out.startswith("timed out")


# ================================================================ the parallel path
def _tv_session():
    return tradovate.TradovateSession(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"id": 11, "spec": "DEMO11", "enabled": True}]}, area_id=1)


async def test_urgent_reads_take_the_order_lane_at_tradovate(admin, monkeypatch):
    sess = _tv_session()
    paths: list[str] = []

    async def raw(method, path, **kw):
        paths.append(path)
        return []
    monkeypatch.setattr(sess, "_request_raw", raw)
    sess._last_sent = time.monotonic()                                # a poll just went out: the poll lane waits 0.2 s
    t0 = time.monotonic()
    with broker.urgent():
        await sess._request("GET", "/order/list")
    assert time.monotonic() - t0 < 0.1 and paths == ["/order/list"] and not broker.is_urgent()
    sess.penalty_until = time.monotonic() + 30                        # a long 429 penalty: refused at once, not waited out
    t0 = time.monotonic()
    with broker.urgent():
        try:
            await sess._request("GET", "/position/list")
            raise AssertionError("expected RateLimited")
        except RateLimited:
            pass
    assert time.monotonic() - t0 < 0.1


async def test_projectx_orders_never_queue_behind_the_poll_spacing(admin, monkeypatch):
    sess = projectx.ProjectXSession(0, {"name": "PX", "broker": "projectx", "px_user": "u", "px_api_key": "k", "enabled": True,
                                        "accounts": [{"id": 1, "spec": "PX1", "enabled": True}]}, area_id=1)

    class Resp:
        status_code, content, headers, text = 200, b'{"success": true}', {}, ""

        def json(self):
            return {"success": True}

    class Client:
        async def post(self, *a, **k):
            return Resp()

    async def tok(*a, **k):
        return "t"
    monkeypatch.setattr(sess, "_get_token", tok)
    monkeypatch.setattr(sess, "_client", lambda: Client())
    sess._last_sent = time.monotonic()                                # a poll just went out
    t0 = time.monotonic()
    await sess._post("/api/Order/place", {})
    assert time.monotonic() - t0 < 0.1
    t0 = time.monotonic()
    with broker.urgent():
        await sess._post("/api/Position/searchOpen", {})              # a close path's read: same lane
    assert time.monotonic() - t0 < 0.2
    t0 = time.monotonic()
    await sess._post("/api/Position/searchOpen", {})                  # a plain poll waits its 0.3 s behind the orders
    assert time.monotonic() - t0 >= 0.25


async def test_risk_guard_flattens_every_tripped_account_at_once(admin, monkeypatch):
    from app import alerts
    flat: list[str] = []

    async def slow_flatten(sess, acc):
        flat.append(acc["spec"])
        await asyncio.sleep(0.2)
        return 1, 1, []

    async def rec(*a, **k):
        pass
    monkeypatch.setattr(risk, "flatten_account", slow_flatten)
    monkeypatch.setattr(alerts, "risk_triggered", rec)
    sess = SimpleNamespace(name="L", accounts=[{"id": 11, "spec": "A", "risk": {"loss_limit": 100}}, {"id": 12, "spec": "B", "risk": {"loss_limit": 100}}])
    snaps = [{"login": "L", "account_id": 11, "spec": "A", "realized": -200, "open": 0}, {"login": "L", "account_id": 12, "spec": "B", "realized": -300, "open": 0}]
    with context.use_area(1):
        t0 = time.monotonic()
        fired = await risk.check_area(1, [sess], snaps)
    assert [f["spec"] for f in fired] == ["A", "B"] and flat == ["A", "B"]
    assert time.monotonic() - t0 < 0.35                               # 2 × 0.2 s in flight together, not one after the other
    assert risk.is_locked(1, "A") and risk.is_locked(1, "B")


async def test_automation_flattens_several_accounts_at_once(admin, monkeypatch):
    async def slow_flatten(sess, acc):
        await asyncio.sleep(0.2)
        return 0, 1, []
    monkeypatch.setattr(risk, "flatten_account", slow_flatten)
    monkeypatch.setattr(automations, "_find_account", lambda aid, spec: (None, {"spec": spec}) if spec != "GHOST" else None)
    t0 = time.monotonic()
    detail = await automations._flatten_accounts(1, ["A", "B", "GHOST"])
    assert time.monotonic() - t0 < 0.35
    assert detail == "A: 0 cancelled, 1 flattened; B: 0 cancelled, 1 flattened; GHOST: not found"


async def test_close_all_looks_at_untracked_accounts_while_the_tracked_ones_close(admin):
    class SlowClose(FakeExecutor):
        async def liquidate_position(self, symbol):
            await asyncio.sleep(0.2)
            return await super().liquidate_position(symbol)

    class SlowRead(FakeExecutor):
        async def positions(self):
            await asyncio.sleep(0.2)
            return await super().positions()
    a, b = SlowClose("A"), SlowRead("B", positions=[{"symbol": "MNQ", "netPos": 1}])
    am = {"wh:MNQ": {"webhook_id": "wh", "root": "MNQ", "side": "buy", "ts": 0.0, "accounts": {"A": {"name": "A", "contract": "MNQ", "qty": 1}}}}
    t0 = time.monotonic()
    r = await manage.handle_close_all("MNQ", "MNQ", [a, b], am, "", WH)
    assert time.monotonic() - t0 < 0.35 and r["status"] == "ok" and r["accounts"] == 2
    assert a.of("liquidate") and b.of("liquidate")                     # the untracked holder was closed too

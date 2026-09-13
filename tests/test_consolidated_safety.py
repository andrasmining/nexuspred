"""Regression coverage for the consolidated execution/admin safety hardening."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config, context, marketplace, updater, watchdog
from app.copy import feed as copy_feed
from app.copy import safety_runner
from app.engine import common, manage, simple
from app.tradovate import OrderOutcomeUnknown
from tests.helpers import FakeExecutor


class _UnknownStop:
    name = "A"

    def __init__(self) -> None:
        self.calls = 0

    async def place_order(self, **_kw):
        self.calls += 1
        raise OrderOutcomeUnknown("answer lost")


async def test_unknown_protective_stop_is_never_blindly_retried(admin):
    ex = _UnknownStop()
    with pytest.raises(OrderOutcomeUnknown):
        await common._place_stop_with_retry(
            ex, symbol="MNQ", action="Sell", qty=1,
            order_type="Stop", stop_price=100.0, tag="",
        )
    assert ex.calls == 1


class _FlattenExecutor:
    name = "A"

    def __init__(self, snapshots, *, outcome=None):
        self.snapshots = list(snapshots)
        self.last_snapshot = list(self.snapshots[-1]) if self.snapshots else []
        self.outcome = outcome
        self.liquidates = []

    async def working_orders(self):
        return []

    async def cancel_order(self, _oid):
        return {}

    async def positions(self):
        if self.snapshots:
            self.last_snapshot = list(self.snapshots.pop(0))
        return [dict(x) for x in self.last_snapshot]

    async def liquidate_position(self, symbol):
        self.liquidates.append(symbol)
        if self.outcome is not None:
            raise self.outcome
        return {"status": "submitted"}


async def test_unknown_liquidation_reconciles_to_broker_flat_without_retry(admin, monkeypatch):
    monkeypatch.setattr(common, "FLATTEN_VERIFY_DELAY_S", 0)
    ex = _FlattenExecutor(
        [[{"symbol": "MNQ", "netPos": 1}], []],
        outcome=OrderOutcomeUnknown("answer lost"),
    )
    cancelled, flattened, errors = await common._flatten_account(ex)
    assert cancelled == 0 and flattened == 1 and errors == []
    assert ex.liquidates == ["MNQ"]


async def test_submitted_liquidation_with_residual_exposure_is_not_success(admin, monkeypatch):
    monkeypatch.setattr(common, "FLATTEN_VERIFY_DELAY_S", 0)
    row = {"symbol": "MNQ", "netPos": 2}
    ex = _FlattenExecutor([[row], [row], [row], [row]])
    _cancelled, flattened, errors = await common._flatten_account(ex)
    assert flattened == 0
    assert ex.liquidates == ["MNQ"]
    assert any("broker still reports position" in err for err in errors)


async def test_flatten_deduplicates_same_symbol_rows(admin, monkeypatch):
    monkeypatch.setattr(common, "FLATTEN_VERIFY_DELAY_S", 0)
    ex = _FlattenExecutor(
        [[{"symbol": "MNQ", "netPos": 1}, {"symbol": "MNQ", "netPos": 2}], []]
    )
    _cancelled, flattened, errors = await common._flatten_account(ex)
    assert flattened == 1 and errors == []
    assert ex.liquidates == ["MNQ"]


async def test_set_sl_tp_reports_failed_open_account(admin):
    ex = FakeExecutor("A", positions=[{"symbol": "MNQ", "netPos": 2}], fail_place=True)
    active = {}
    result = await manage.handle_set_sl_tp(
        {"stop_price": 100.0}, "MNQ", "MNQ", [ex], active, "",
        {"id": "wh", "name": "test"},
    )
    assert result["status"] == "error"
    assert result["failed"] == ["A"]
    assert "wh:MNQ" in active
    assert active["wh:MNQ"]["accounts"]["A"]["qty"] == 2


async def test_simple_partial_entry_failure_remains_compatible_but_visible(admin):
    a, b = FakeExecutor("A"), FakeExecutor("B", fail_place=True)
    config.save_settings({"entry_order_type": "Market"})
    active = {}
    result = await simple.handle_entry(
        {"qty": 1}, "buy", "MNQ", "MNQ", [a, b], active, "",
        {"id": "wh", "name": "simple", "default_qty": 1},
    )
    assert result["status"] == "ok"
    assert result["failed"] == ["B"]
    assert result["accounts"] == [{"account": "A", "qty": 1}]
    assert list(active["wh:MNQ"]["accounts"]) == ["A"]


def test_marketplace_acl_is_rechecked_from_current_workspace_owner(admin, monkeypatch):
    webhook = {"sharing": {"enabled": True, "visibility": "selected", "allowed_user_ids": [7]}}
    monkeypatch.setattr(marketplace.db, "area_owner", lambda _aid: 7)
    assert marketplace.subscription_allowed(webhook, {"area_id": 3}) is True
    webhook["sharing"]["allowed_user_ids"] = []
    assert marketplace.subscription_allowed(webhook, {"area_id": 3}) is False


def test_copy_execution_acl_fails_closed_after_revocation(admin, monkeypatch):
    group = {
        "id": "cg_acl", "name": "acl", "enabled": True,
        "leader": {"token_idx": 0, "spec": "L", "account_id": 1},
        "followers": [], "symbols": [], "copy_adds": True, "copy_orders": True,
        "feed_loss_flatten_s": 30, "on_feed_loss": "flatten",
        "sharing": {"enabled": True, "visibility": "selected", "allowed_user_ids": [7]},
    }
    runner = safety_runner.GroupRunner(context.get_area(), group)
    follower = {"token_idx": 0, "spec": "F", "account_id": 2, "enabled": True,
                "mode": "multiplier", "multiplier": 1, "fixed": 1, "max_contracts": 0,
                "direction": "both", "lid": "", "external": True,
                "area_id": 3, "sub_id": 99}
    sub = {"id": 99, "area_id": 3, "publisher_area_id": context.get_area(),
           "webhook_id": "copy:cg_acl", "enabled": True, "status": "active"}
    monkeypatch.setattr(safety_runner.db, "get_subscription", lambda *_a, **_kw: sub)
    monkeypatch.setattr(safety_runner, "load_groups", lambda _aid: [group])
    monkeypatch.setattr(safety_runner.marketplace, "subscription_allowed", lambda *_a, **_kw: False)
    assert runner._external_authorized(follower) is False


def test_settings_export_strips_installation_local_marketplace_acl(admin):
    from app.routers import settings_io

    wh = config.new_webhook(name="shared")
    wh["sharing"] = {
        "enabled": True,
        "visibility": "selected",
        "allowed_user_ids": [123, 456],
        "title": "Portable title",
        "description": "Portable description",
        "published_at": "2026-01-01T00:00:00+00:00",
    }
    config.save_settings({"webhooks": [wh]})
    doc = settings_io.export_settings(context.get_area())
    exported = doc["settings"]["webhooks"][0]["sharing"]
    assert exported["enabled"] is False
    assert exported["allowed_user_ids"] == []
    assert exported["published_at"] == ""
    assert exported["title"] == "Portable title"


def test_shared_feed_key_changes_when_session_instance_is_replaced(admin):
    a = SimpleNamespace(lid="same-login")
    b = SimpleNamespace(lid="same-login")
    assert copy_feed.key_of(1, a) != copy_feed.key_of(1, b)


async def test_heartbeat_revalidates_destination_before_request(admin, monkeypatch):
    called = False

    def blocked(_url):
        return "resolved address is private"

    class Client:
        async def get(self, *_a, **_kw):
            nonlocal called
            called = True
            raise AssertionError("network request must not happen")

    monkeypatch.setattr(watchdog.security, "check_outbound_url", blocked)
    monkeypatch.setattr(watchdog.http, "client", lambda _name: Client())
    assert await watchdog.ping(1, "https://heartbeat.example/ping") is False
    assert called is False


async def test_heartbeat_disables_redirects(admin, monkeypatch):
    seen = {}

    class Response:
        status_code = 204

    class Client:
        async def get(self, _url, **kw):
            seen.update(kw)
            return Response()

    monkeypatch.setattr(watchdog.security, "check_outbound_url", lambda _url: None)
    monkeypatch.setattr(watchdog.http, "client", lambda _name: Client())
    assert await watchdog.ping(1, "https://heartbeat.example/ping") is True
    assert seen["follow_redirects"] is False


class _CopySession:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.last = list(self.snapshots[-1]) if self.snapshots else []

    async def positions_snapshot(self):
        if self.snapshots:
            self.last = list(self.snapshots.pop(0))
        return [dict(x) for x in self.last]


class _CopyExecutor:
    def __init__(self, snapshots):
        self.id = 2
        self.session = _CopySession(snapshots)
        self.orders = []

    async def place_order(self, **kw):
        self.orders.append(dict(kw))
        return {"status": "submitted", "order_id": len(self.orders)}


async def _copy_runner(monkeypatch, broker_nets):
    group = {
        "id": "cg_test", "name": "test", "enabled": True,
        "leader": {"token_idx": 0, "spec": "L", "account_id": 1},
        "followers": [{"token_idx": 0, "spec": "F", "account_id": 2, "enabled": True,
                       "mode": "multiplier", "multiplier": 1, "fixed": 1,
                       "max_contracts": 0, "direction": "both", "lid": ""}],
        "symbols": [], "copy_adds": True, "copy_orders": True,
        "feed_loss_flatten_s": 30, "on_feed_loss": "flatten",
    }
    snapshots = [
        ([{"accountId": 2, "contractId": 5, "netPos": net}] if net else [])
        for net in broker_nets
    ]
    ex = _CopyExecutor(snapshots)
    runner = safety_runner.GroupRunner(context.get_area(), group)
    monkeypatch.setattr(runner, "_executor", lambda _f: ex)
    monkeypatch.setattr(runner, "_persist", lambda _cid: None)

    async def cancel_all(**_kw):
        return 0

    monkeypatch.setattr(runner.orders, "cancel_all", cancel_all)
    runner.leader_net[5] = 1
    runner.unit[5] = 1
    runner.contract_names[5] = "MNQ"
    runner.follower_pos[("F", 5)] = 1
    return runner, ex


async def test_copy_flatten_baselines_only_after_broker_confirms_flat(admin, monkeypatch):
    monkeypatch.setattr(safety_runner, "FLATTEN_VERIFY_DELAY_S", 0)
    runner, ex = await _copy_runner(monkeypatch, [1, 0])
    sent = await runner.flatten_followers(reason="feed lost")
    assert sent == 1 and runner.flatten_unresolved == []
    assert 5 in runner.baseline and runner.follower_pos[("F", 5)] == 0
    assert len(ex.orders) == 1


async def test_copy_flatten_keeps_unconfirmed_exposure_out_of_baseline(admin, monkeypatch):
    monkeypatch.setattr(safety_runner, "FLATTEN_VERIFY_DELAY_S", 0)
    runner, ex = await _copy_runner(monkeypatch, [1, 1, 1, 1])
    sent = await runner.flatten_followers(reason="feed lost")
    assert sent == 1 and runner.flatten_unresolved
    assert 5 not in runner.baseline and runner.follower_pos[("F", 5)] == 1
    assert len(ex.orders) == 1


async def test_updater_rolls_back_when_dependency_install_fails(admin, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    monkeypatch.setattr(config, "get_version", lambda force=False: "1.0.0")
    calls = []
    pip_calls = 0

    def fake_run(cmd):
        nonlocal pip_calls
        calls.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return True, "oldsha"
        if cmd[:2] == ["git", "fetch"]:
            return True, "fetched"
        if cmd[:3] == ["git", "reset", "--hard"]:
            return True, "reset"
        if "pip" in cmd and "install" in cmd:
            pip_calls += 1
            return (False, "dependency failed") if pip_calls == 1 else (True, "restored")
        return True, "ok"

    monkeypatch.setattr(updater, "_run", fake_run)
    result = await updater.apply_update()
    assert result["success"] is False and "rolled back" in result["message"]
    assert ["git", "reset", "--hard", "oldsha"] in calls
    assert pip_calls == 2

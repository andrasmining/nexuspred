"""Regression tests for cross-broker exposure reconciliation at alpha.88."""
from __future__ import annotations

from app import db
from app.tradovate import TradovateError
from tests.test_alpha88 import mixed  # noqa: F401 - shared broker fixtures
from tests.test_copy import _placed


async def test_unresolved_contract_does_not_turn_existing_exposure_into_flat(mixed, monkeypatch):
    r, px, ex = mixed["r"], mixed["px"], mixed["ex"]
    r.contract_names[901] = "MNQZ6"
    r.leader_net[901] = r.unit[901] = 2
    r.follower_pos[("PX-1", 901)] = 2
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 2}]
    r._mark_feed(True)

    async def unavailable(name):
        raise TradovateError("temporary contract lookup failure")

    monkeypatch.setattr(px, "contract_id", unavailable)
    await r.reconcile()
    assert _placed(ex) == [], "unreadable contract mapping must not authorize another entry"
    assert r.follower_pos[("PX-1", 901)] == 2


async def test_restart_partial_close_uses_cross_broker_follower_exposure(mixed):
    r, lead, px, ex = mixed["r"], mixed["leader"], mixed["px"], mixed["ex"]
    db.save_copy_state(1, r.id, 901, "MNQZ6", 2, 2)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 2}]

    # The production _feed_main order, rather than the reversed order used in
    # the original alpha.88 tests: follower seed runs before leader names exist.
    await r._seed_followers(force=True)
    await r._seed_leader(lead, 1)
    assert _placed(ex) == [("Sell", 1, "MNQZ6")]
    assert r.follower_pos[("PX-1", 901)] == 1


async def test_new_leader_contract_accounts_for_existing_cross_broker_position(mixed):
    r, lead, px, ex = mixed["r"], mixed["leader"], mixed["px"], mixed["ex"]
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 1}]
    await r._seed_followers(force=True)
    await r._seed_leader(lead, 1)
    r._mark_feed(True)

    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)
    assert _placed(ex) == [("Buy", 1, "MNQZ6")]


async def test_failed_refresh_preserves_exposure_and_recovers_when_mapping_returns(mixed, monkeypatch):
    r, px, ex = mixed["r"], mixed["px"], mixed["ex"]
    r.contract_names[901] = "MNQZ6"
    r.leader_net[901] = r.unit[901] = 2
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 2}]
    await r._seed_followers(force=True)
    r._mark_feed(True)
    key = (r._login_key(px), 901)
    r._fcid[key] = (5001, -1e9)
    original = px.contract_id

    async def unavailable(name):
        raise TradovateError("temporary lookup failure")

    monkeypatch.setattr(px, "contract_id", unavailable)
    await r._seed_followers(force=True)
    await r.reconcile()
    assert r.follower_pos[("PX-1", 901)] == 2
    assert _placed(ex) == []

    monkeypatch.setattr(px, "contract_id", original)
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 1}]
    await r.reconcile()
    assert _placed(ex) == [("Buy", 1, "MNQZ6")]


async def test_unreadable_contract_does_not_block_another_readable_contract(mixed):
    r, px, ex = mixed["r"], mixed["px"], mixed["ex"]
    r.contract_names.update({901: "RTYZ6", 902: "ESZ6"})
    r.leader_net.update({901: 2, 902: 2})
    r.unit.update(r.leader_net)
    r.follower_pos[("PX-1", 901)] = 2
    px.positions = [{"accountId": 2, "contractId": 5002, "netPos": 1}]
    r._mark_feed(True)
    await r.reconcile()
    assert _placed(ex) == [("Buy", 1, "ESZ6")]
    assert r.follower_pos[("PX-1", 901)] == 2


async def test_first_cross_broker_delta_is_blocked_when_positions_are_unreadable(mixed, monkeypatch):
    r, lead, px, ex = mixed["r"], mixed["leader"], mixed["px"], mixed["ex"]
    await r._seed_leader(lead, 1)
    r._mark_feed(True)

    async def unavailable(**kwargs):
        raise TradovateError("positions temporarily unavailable")

    monkeypatch.setattr(px, "positions_snapshot", unavailable)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)
    assert _placed(ex) == []
    assert ("PX-1", 901) not in r.follower_pos
    assert "unreadable" in r.follower_err["PX-1"]


async def test_mapping_failure_does_not_expose_private_login_in_publisher_status(mixed, monkeypatch):
    from app.copy.groups import masked_status

    r, px = mixed["r"], mixed["px"]
    r.contract_names[901] = "MNQZ6"
    px.name = "subscriber-private-login"
    r.followers[0].update(external=True, sub_id=5, area_id=2)

    async def unavailable(name):
        raise TradovateError("rejected account subscriber-private-account")

    monkeypatch.setattr(px, "contract_id", unavailable)
    assert await r._follower_cid(px, 901) is None
    status = masked_status(r.status())
    assert "subscriber-private" not in str(status)
    assert status["error"]

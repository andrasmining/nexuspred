"""alpha.88: copy trading across brokers — a leader on one broker, followers on
any other; contracts are keyed by the leader's ids and translated by name per
follower login."""
from __future__ import annotations

import pytest

from app import alerts, config, context, tradovate
from app import copy as cp
from tests.helpers import FakeExecutor
from tests.test_copy import Sess, _group, _placed


class PxSess:
    """A follower login on another broker: its own contract ids, names resolved by the adapter."""
    kind, lid, name, enabled, environment = "projectx", "px1", "PX", True, "demo"

    def __init__(self):
        self.accounts = [{"id": 2, "spec": "PX-1", "enabled": True}]
        self.positions: list[dict] = []
        self.ids = {"MNQZ6": 5001, "ESZ6": 5002}
        self.lookups: list[str] = []

    def has_token(self):
        return True

    async def positions_snapshot(self, **kw):
        return [dict(p) for p in self.positions]

    async def contract_id(self, name):
        self.lookups.append(name)
        return self.ids[name]


class Manager:
    def __init__(self, sessions, executors):
        self.sessions, self.executors = sessions, executors

    def all(self):
        return list(self.sessions)

    def session_for(self, lid, token_idx=None):
        if lid:
            return next((s for s in self.sessions if getattr(s, "lid", "") == lid), None)
        return self.sessions[token_idx] if token_idx is not None and 0 <= token_idx < len(self.sessions) else None

    def executor_for(self, idx, spec, mult=1, sizing=None, lid=None):
        return self.executors.get(spec)


@pytest.fixture
def mixed(monkeypatch, admin):
    """A Tradovate leader (contract 901 = MNQZ6) and one follower on a ProjectX login (MNQZ6 = 5001)."""
    leader = Sess(0, "L", [{"id": 1, "spec": "LEAD"}])
    px = PxSess()
    ex = FakeExecutor("PX-1")
    ex.session, ex.id = px, 2
    mgr = Manager([leader, px], {"PX-1": ex})
    monkeypatch.setattr(tradovate, "manager_for", lambda area_id: mgr)
    monkeypatch.setattr(cp.group_runner, "RECONCILE_INTERVAL_S", 3600)
    monkeypatch.setattr(cp.group_runner, "ORDER_SETTLE_S", 0.0)
    monkeypatch.setattr(alerts, "copy_alert", lambda *a, **k: None)
    with context.use_area(1):
        config.save_settings({"trading_enabled": True})
    g = _group(followers=[cp.normalize_follower({"token_idx": 1, "lid": "px1", "spec": "PX-1", "account_id": 2})])
    r = cp.GroupRunner(1, g)
    return {"leader": leader, "px": px, "ex": ex, "r": r}


async def test_follower_positions_are_read_through_the_leaders_contract_ids(mixed):
    r, lead, px = mixed["r"], mixed["leader"], mixed["px"]
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._seed_leader(lead, 1)                                         # names 901 → MNQZ6 (baseline: held before the group)
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 1}, {"accountId": 2, "contractId": 7777, "netPos": 4}]
    await r._seed_followers(force=True)
    assert r.follower_pos == {("PX-1", 901): 1}                            # 5001 is the leader's 901; 7777 is nothing the leader holds
    assert px.lookups == ["MNQZ6"]
    assert await r._broker_net(mixed["ex"], 901) == 1
    assert px.lookups == ["MNQZ6"]                                          # cached per login


async def test_mirror_and_drift_correction_place_by_name_on_the_other_broker(mixed):
    r, lead, px, ex = mixed["r"], mixed["leader"], mixed["px"], mixed["ex"]
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)
    assert _placed(ex) == [("Buy", 2, "MNQZ6")]                            # the follower's adapter resolves the name
    assert r.follower_pos[("PX-1", 901)] == 2
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 3}]     # a manual add at the follower's broker
    await r.reconcile()
    assert _placed(ex)[-1] == ("Sell", 1, "MNQZ6")


async def test_feed_loss_flatten_closes_the_other_brokers_position(mixed, monkeypatch):
    r, lead, px, ex = mixed["r"], mixed["leader"], mixed["px"], mixed["ex"]
    monkeypatch.setattr(cp.group_runner, "FLATTEN_VERIFY_DELAY_S", 0.0)
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)
    px.positions = [{"accountId": 2, "contractId": 5001, "netPos": 2}]
    real = ex.place_order

    async def place(**kw):
        px.positions = []                                                  # the close fills at the follower's broker
        return await real(**kw)
    ex.place_order = place
    assert await r.flatten_followers(reason="feed lost") == 1
    assert _placed(ex)[-1] == ("Sell", 2, "MNQZ6") and r.unresolved_for() == [] and 901 in r.baseline


async def test_an_unresolvable_contract_is_unreadable_never_flat(mixed):
    r, lead, ex = mixed["r"], mixed["leader"], mixed["ex"]
    await r._seed_leader(lead, 1)
    r.contract_names[903] = "RTYZ6"                                        # a product the follower's broker does not list
    assert await r._broker_net(ex, 903) is None

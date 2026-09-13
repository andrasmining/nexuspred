"""Regression tests for must-have maintenance fixes on top of current upstream."""
from __future__ import annotations

from app import config, context, state, track_record
from app import copy as cp
from app.routers.accounts import trade_accounts_overview
from tests.test_alpha88 import mixed  # noqa: F401 - pytest fixture
from tests.test_copy import _placed


async def test_feed_loss_flatten_never_trades_from_cached_position_when_broker_unreadable(mixed, monkeypatch):
    r, ex = mixed["r"], mixed["ex"]
    monkeypatch.setattr(cp.group_runner, "FLATTEN_VERIFY_DELAY_S", 0.0)
    r.contract_names[901] = "MNQZ6"
    r.leader_net[901] = 2
    r.unit[901] = 2
    r.follower_pos[("PX-1", 901)] = 2

    async def unreadable(_ex, _cid):
        return None

    monkeypatch.setattr(r, "_broker_net", unreadable)

    assert await r.flatten_followers(reason="feed lost") == 0
    assert _placed(ex) == []
    assert ("PX-1", 901) not in r.follower_pos
    assert any("position unreadable before close" in msg for msg in r.unresolved_for())


def _pnl(account_id: int, spec: str, cash: float) -> dict:
    return {"account_id": account_id, "spec": spec, "realized": 0, "open": 0, "week": 0, "cash": cash}


def _summary(accounts: list[dict]) -> dict:
    return {"accounts": accounts, "realized": 0, "open": 0, "week": 0,
            "cash": sum(float(a["cash"]) for a in accounts), "error": ""}


def test_account_size_never_crosses_broker_local_account_ids(admin):
    """Different brokers may reuse numeric account ids; sizing suggestions must
    bind the live balance to the intended account instead of the bare id."""
    with context.use_area(1):
        config.save_settings({"token_accounts": [
            {"name": "Leader login", "broker": "tradovate", "environment": "demo", "enabled": True, "lid": "l1",
             "accounts": [{"id": 7, "spec": "LEAD", "enabled": True}]},
            {"name": "Follower login", "broker": "projectx", "environment": "demo", "enabled": True, "lid": "l2",
             "accounts": [{"id": 7, "spec": "FOLLOW", "enabled": True}]},
        ]})
        state.set_pnl(_summary([_pnl(7, "LEAD", 49900), _pnl(7, "FOLLOW", 150100)]), area_id=1)
        rows = {a["spec"]: a for a in trade_accounts_overview()}
        assert rows["LEAD"]["balance"] == 49900
        assert rows["LEAD"]["tier"]["tier"] == "50K"
        assert rows["FOLLOW"]["balance"] == 150100
        assert rows["FOLLOW"]["tier"]["tier"] == "150K"

        # The public copy listing used the same bare-id lookup. Reverse the P&L
        # rows so an id-only `next(...)` would borrow the follower's 150K size.
        state.set_pnl(_summary([_pnl(7, "FOLLOW", 150100), _pnl(7, "LEAD", 49900)]), area_id=1)
        g = {**cp.new_group("g"), "leader": {"token_idx": 0, "lid": "l1", "spec": "LEAD", "account_id": 7}, "followers": []}
        view = cp.public_view(g, 1, email="pub@example.com")
        assert view["leader_tier"] == "50K"
        assert view["leader_size"] == 50000


def test_copy_track_record_size_never_crosses_broker_local_account_ids(admin):
    """Public performance percentages must use the actual leader's size, not a
    different broker account that happens to reuse its numeric account id."""
    with context.use_area(1):
        track_record.reset()
        state.set_pnl(_summary([_pnl(7, "FOLLOW", 150100), _pnl(7, "LEAD", 49900)]), area_id=1)
        g = {**cp.new_group("g"), "id": "track-size-collision",
             "leader": {"token_idx": 0, "lid": "l1", "spec": "LEAD", "account_id": 7}, "followers": []}
        assert track_record.copy_record(1, g, detail=False)["size"] == 50000

        # Ambiguous snapshots fail closed: omitting the percentage denominator is
        # safer than publishing performance against an account we cannot identify.
        state.set_pnl(_summary([_pnl(7, "LEAD", 49900), _pnl(7, "LEAD", 150100)]), area_id=1)
        g["id"] = "track-size-ambiguous"
        assert track_record.copy_record(1, g, detail=False)["size"] is None

"""Regression tests for must-have maintenance fixes on top of current upstream."""
from __future__ import annotations

from app import config, context, db, payments, state, track_record, web
from app import copy as cp
from app.routers.accounts import trade_accounts_overview
from tests.test_alpha88 import mixed  # noqa: F401 - pytest fixture
from tests.test_alpha93 import _client, trio  # noqa: F401 - pytest fixture
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


async def test_broadcaster_demotion_retains_failed_stripe_subscription_for_retry(trio, monkeypatch):
    """A role change must not orphan a still-live Stripe subscription by deleting
    the local subscription row. Unpublish immediately, retain retry identity,
    and keep the Broadcaster role until Stripe confirms cancellation."""
    admin, broadcaster, subscriber = trio
    publisher_area = db.user_primary_area(broadcaster["id"])
    subscriber_area = db.user_primary_area(subscriber["id"])

    with context.use_area(publisher_area):
        wh = config.new_webhook(name="Paid signal")
        wh["sharing"] = {"enabled": True, "mode": "public", "price_cents": 1000}
        config.save_settings({"webhooks": [wh]})
    sub = db.upsert_subscription(subscriber_area, publisher_area, wh["id"], [], user_id=subscriber["id"])
    db.upsert_payment(subscriber_area, publisher_area, wh["id"],
                      stripe_subscription="sub_still_live", status="active", price_cents=1000, currency="usd")

    async def cancellation_fails(_publisher_area, _key, *, area_id=None):
        return 0

    monkeypatch.setattr(payments, "cancel_for_listing", cancellation_fails)
    async with _client(admin["id"]) as c:
        r = await c.post(f"/api/users/{broadcaster['id']}/role", json={"role": "user"})
    assert r.status_code == 502
    assert db.get_user(broadcaster["id"])["role"] == "broadcaster"
    with context.use_area(publisher_area):
        assert config.load_settings()["webhooks"][0]["sharing"]["enabled"] is False
    assert [s["id"] for s in db.list_subscriptions(subscriber_area)] == [sub["id"]]
    assert db.get_payment(subscriber_area, publisher_area, wh["id"])["status"] == "active"

    async def cancellation_recovers(_publisher_area, key, *, area_id=None):
        row = db.get_payment(subscriber_area, publisher_area, key)
        db.update_payment(row["id"], status="canceled")
        return 1

    monkeypatch.setattr(payments, "cancel_for_listing", cancellation_recovers)
    async with _client(admin["id"]) as c:
        r = await c.post(f"/api/users/{broadcaster['id']}/role", json={"role": "user"})
    assert r.status_code == 200, r.text
    assert db.get_user(broadcaster["id"])["role"] == "user"
    assert db.list_subscriptions(subscriber_area) == []
    assert db.get_payment(subscriber_area, publisher_area, wh["id"])["status"] == "canceled"


def test_support_cookie_rechecks_target_after_role_change(trio):
    """A support cookie must not remain an authorization lease after its target
    becomes another admin, whose workspace a non-bootstrap admin may not inspect."""
    bootstrap, helper, target = trio
    db.set_role(helper["id"], "admin")
    target_area = db.user_primary_area(target["id"])
    cookie = web.make_support_cookie(helper["id"], target_area, target["email"])
    assert web.read_support_cookie(cookie, helper["id"])["area_id"] == target_area

    db.set_role(target["id"], "admin")
    assert web.read_support_cookie(cookie, helper["id"]) is None
    # The bootstrap admin is explicitly allowed to support other admins.
    root_cookie = web.make_support_cookie(bootstrap["id"], target_area, target["email"])
    assert web.read_support_cookie(root_cookie, bootstrap["id"])["area_id"] == target_area

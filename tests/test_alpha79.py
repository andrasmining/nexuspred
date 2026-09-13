"""alpha.79 — paid subscriptions through Stripe: operator config, listing price,
Checkout, webhook signature and state mirroring, the admin switch, trials."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app import config, context, db, marketplace, payments, signals
from tests.helpers import settle
from tests.test_marketplace import PAYLOAD, S1, execs, sub_client, two_areas  # noqa: F401


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    return f"t={ts},v1={hmac.new(secret.encode(), f'{ts}.'.encode() + payload, hashlib.sha256).hexdigest()}"


@pytest.fixture
def stripe(monkeypatch, admin):
    """Payments enabled with a fake Stripe: every API call is recorded and answered."""
    payments.save_config({"enabled": True, "stripe_secret_key": "sk_test_123", "stripe_webhook_secret": "whsec_abc", "currency": "chf", "trial_days_default": 0})
    calls: list[tuple[str, str, dict]] = []

    async def fake(method, path, data=None):
        calls.append((method, path, dict(data or {})))
        if path == "/checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}
        if path == "/billing_portal/sessions":
            return {"url": "https://billing.stripe.test/p"}
        return {}
    monkeypatch.setattr(payments, "_stripe", fake)
    return calls


# ------------------------------------------------------------------ config
def test_config_roundtrip_masks_and_encrypts_secrets(admin):
    assert payments.public_config()["enabled"] is False and payments.configured() is False
    cfg = payments.save_config({"enabled": True, "stripe_secret_key": "sk_live_x", "stripe_webhook_secret": "whsec_y", "currency": "EUR", "trial_days_default": "7"})
    assert cfg["currency"] == "eur" and cfg["trial_days_default"] == 7 and payments.configured()
    stored = json.loads(db.meta_get("payments"))
    assert stored["stripe_secret_key"] != "sk_live_x" and stored["stripe_webhook_secret"] != "whsec_y"     # encrypted at rest
    pub = payments.public_config()
    assert pub["stripe_secret_key"] == "********" and pub["webhook_configured"] is True
    payments.reset()
    assert payments.get_config()["stripe_secret_key"] == "sk_live_x"                                     # decrypts after a restart
    assert payments.save_config({"stripe_secret_key": "********"})["stripe_secret_key"] == "sk_live_x"   # the mask never overwrites
    for bad in ({"currency": "xxx"}, {"trial_days_default": 400}, {"stripe_secret_key": "nope"}, {"stripe_webhook_secret": "abc"}, {"trial_days_default": "x"})[:4]:
        with pytest.raises(ValueError):
            payments.save_config(bad)
    assert payments.save_config({"enabled": False})["enabled"] is False and payments.configured() is False


def test_price_and_trial_normalisation():
    assert payments.normalize_price("2900") == 2900 and payments.normalize_price(0) == 0 and payments.normalize_trial("14") == 14
    for bad in ("abc", 50, -1, 10 ** 9):
        with pytest.raises(ValueError):
            payments.normalize_price(bad)
    with pytest.raises(ValueError):
        payments.normalize_trial(91)
    sh = marketplace.normalize_sharing({"price_cents": "1500", "trial_days": 7})
    assert sh["price_cents"] == 1500 and sh["trial_days"] == 7


def test_signature_verification():
    body = b'{"id":"evt_1"}'
    assert payments.verify_signature(body, _sign(body, "whsec_a"), "whsec_a")
    assert not payments.verify_signature(body, _sign(body, "whsec_b"), "whsec_a")
    assert not payments.verify_signature(body + b" ", _sign(body, "whsec_a"), "whsec_a")
    assert not payments.verify_signature(body, _sign(body, "whsec_a", ts=int(time.time()) - 1000), "whsec_a")     # stale
    assert not payments.verify_signature(body, "", "whsec_a") and not payments.verify_signature(body, "garbage", "whsec_a")
    assert not payments.verify_signature(body, _sign(body, ""), "")


# ------------------------------------------------------------- admin routes
async def test_admin_config_routes(client, sub_client):
    assert (await sub_client.get("/api/payments/config")).status_code == 403
    r = await client.put("/api/payments/config", json={"enabled": True, "stripe_secret_key": "sk_test_1", "stripe_webhook_secret": "whsec_1", "currency": "usd"})
    assert r.status_code == 200 and r.json()["configured"] is True and r.json()["stripe_secret_key"] == "********"
    assert (await client.put("/api/payments/config", json={"currency": "btc"})).status_code == 400
    st = (await client.get("/api/status")).json()["payments"]
    assert st == {"enabled": True, "currency": "usd"}
    assert any(a["action"] == "payments_config" for a in db.list_audit())


# ---------------------------------------------------------- subscribe flow
async def test_paid_listing_waits_for_payment_then_forwards(two_areas, sub_client, client, execs, stripe):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    r = await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 2900, "trial_days": 7})
    assert r.status_code == 200 and r.json()["sharing"]["price_cents"] == 2900
    items = (await sub_client.get("/api/marketplace")).json()
    assert items[0]["paid"] is True and items[0]["price_cents"] == 2900 and items[0]["currency"] == "chf" and items[0]["trial_days"] == 7
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})
    assert r.status_code == 200 and r.json()["status"] == "unpaid" and r.json()["active"] is False
    sid = r.json()["id"]
    with context.use_area(1):
        signals.accept(PAYLOAD, wh)
    await settle(20)
    assert [e for area, _w, e in execs if area == a2] == []                                              # unpaid: nothing forwarded
    # checkout
    r = await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test/")
    method, path, data = stripe[-1]
    assert path == "/checkout/sessions" and data["line_items[0][price_data][unit_amount]"] == "2900" and data["line_items[0][price_data][currency]"] == "chf"
    assert data["subscription_data[trial_period_days]"] == "7" and data["client_reference_id"] == f"{a2}:1:{wh['id']}" and data["customer_email"] == "sub@example.com"
    mine = (await sub_client.get("/api/payments/mine")).json()
    assert mine[0]["status"] == "pending" and mine[0]["checkout_session"] == "cs_1" and "email" not in mine[0]
    # Stripe confirms
    event = json.dumps({"id": "evt_1", "created": 1_700_000_001, "type": "checkout.session.completed",
                        "data": {"object": {"id": "cs_1", "customer": "cus_1", "subscription": "sub_1", "payment_status": "paid",
                                            "client_reference_id": f"{a2}:1:{wh['id']}"}}}).encode()
    r = await sub_client.post("/api/payments/webhook", content=event, headers={"stripe-signature": _sign(event, "whsec_abc"), "content-type": "application/json"})
    assert r.status_code == 200
    assert db.get_subscription(sid, a2)["status"] == "active"
    with context.use_area(1):
        signals.accept(PAYLOAD, wh)
    await settle(20)
    assert [e for area, _w, e in execs if area == a2]                                                    # now it flows
    assert (await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})).status_code == 409   # already paid
    # trial → active → past_due → canceled
    for i, (status, expect) in enumerate((("trialing", "active"), ("past_due", "unpaid"), ("active", "active"))):
        ev = json.dumps({"id": f"evt_s{i}", "created": 1_700_000_010 + i, "type": "customer.subscription.updated",
                         "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": status, "current_period_end": 1_800_000_000, "trial_end": 0}}}).encode()
        await sub_client.post("/api/payments/webhook", content=ev, headers={"stripe-signature": _sign(ev, "whsec_abc")})
        assert db.get_subscription(sid, a2)["status"] == expect, status
    ev = json.dumps({"id": "evt_inv", "created": 1_700_000_020, "type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_1"}}}).encode()
    await sub_client.post("/api/payments/webhook", content=ev, headers={"stripe-signature": _sign(ev, "whsec_abc")})
    assert db.get_subscription(sid, a2)["status"] == "unpaid" and db.payment_by("stripe_subscription", "sub_1")["status"] == "past_due"
    ev = json.dumps({"id": "evt_del", "created": 1_700_000_030, "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled"}}}).encode()
    await sub_client.post("/api/payments/webhook", content=ev, headers={"stripe-signature": _sign(ev, "whsec_abc")})
    assert db.payment_by("stripe_subscription", "sub_1")["status"] == "canceled" and db.get_subscription(sid, a2)["status"] == "unpaid"
    # the publisher sees their payments, the admin everything
    rows = (await client.get("/api/payments")).json()
    assert rows and rows[0]["email"] == "sub@example.com" and rows[0]["webhook_id"] == wh["id"]
    # portal
    r = await sub_client.post("/api/payments/portal")
    assert r.status_code == 200 and r.json()["url"].startswith("https://billing.stripe.test/") and stripe[-1][2]["customer"] == "cus_1"


async def test_webhook_rejects_bad_signatures_and_ignores_unknown_events(sub_client, stripe):
    body = b'{"type":"ping"}'
    assert (await sub_client.post("/api/payments/webhook", content=body)).status_code == 400
    assert (await sub_client.post("/api/payments/webhook", content=body, headers={"stripe-signature": _sign(body, "whsec_wrong")})).status_code == 400
    r = await sub_client.post("/api/payments/webhook", content=body, headers={"stripe-signature": _sign(body, "whsec_abc")})
    assert r.status_code == 200
    bad = b"not json"
    assert (await sub_client.post("/api/payments/webhook", content=bad, headers={"stripe-signature": _sign(bad, "whsec_abc")})).status_code == 400
    ev = json.dumps({"type": "customer.subscription.updated", "data": {"object": {"id": "sub_zzz", "status": "active"}}}).encode()
    assert (await sub_client.post("/api/payments/webhook", content=ev, headers={"stripe-signature": _sign(ev, "whsec_abc")})).status_code == 200


async def test_admin_switch_off_makes_every_listing_free(two_areas, sub_client, client, stripe):
    wh = two_areas["wh"]
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 999})
    payments.save_config({"enabled": False})
    items = (await sub_client.get("/api/marketplace")).json()
    assert items[0]["paid"] is False and items[0]["price_cents"] == 0
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})
    assert r.json()["status"] == "active"
    assert (await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})).status_code == 409


async def test_checkout_validation(two_areas, sub_client, client, stripe):
    wh = two_areas["wh"]
    assert (await sub_client.post("/api/payments/checkout", json={"key": wh["id"]})).status_code == 400
    assert (await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": "wh_nope"})).status_code == 404
    assert (await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})).status_code == 400      # free listing
    assert (await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 5})).status_code == 400                     # below the minimum
    assert (await sub_client.post("/api/payments/portal")).status_code == 502                                                       # no billing account yet


async def test_paid_copy_group_and_approval_after_payment(two_areas, sub_client, client, stripe, monkeypatch):
    from app import copy as cp
    a2 = two_areas["a2"]
    g = cp.new_group("Lead"); g["leader"] = {"token_idx": 0, "spec": "L1", "account_id": 1}
    g["sharing"] = {"enabled": True, "title": "Lead", "visibility": "all", "price_cents": 4900, "approval": True}
    config.save_settings({"copy_groups": [g]}, area_id=1)

    async def fake_sync(area):
        return None
    monkeypatch.setattr(cp, "sync_area", fake_sync)
    monkeypatch.setattr(cp, "clean_subscriber_accounts", lambda raw, area, **kw: [{"token_idx": 0, "lid": "", "spec": "S1", "enabled": True, "mode": "multiplier", "multiplier": 1.0, "fixed": 1, "max_contracts": 0, "direction": "both"}])
    r = await sub_client.post(f"/api/marketplace/1/copy/{g['id']}/subscribe", json={"accounts": [{"token_idx": 0, "spec": "S1", "enabled": True}]})
    assert r.status_code == 200 and r.json()["status"] == "unpaid"
    sid = r.json()["id"]
    r = await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": f"copy:{g['id']}"})
    assert r.status_code == 200 and stripe[-1][2]["line_items[0][price_data][product_data][name]"] == "Lead"
    ev = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_1", "customer": "cus_2", "subscription": "sub_2", "payment_status": "paid",
                                                                                "metadata": {"area_id": str(a2), "publisher_area_id": "1", "key": f"copy:{g['id']}"}}}}).encode()
    await sub_client.post("/api/payments/webhook", content=ev, headers={"stripe-signature": _sign(ev, "whsec_abc")})
    assert db.get_subscription(sid, a2)["status"] == "pending"                                          # paid, now waiting for the publisher's approval


# ----------------------------------------------------- review round 5 fixes
def _ev(kind: str, obj: dict, *, eid: str, created: int) -> bytes:
    return json.dumps({"id": eid, "created": created, "type": kind, "data": {"object": obj}}).encode()


async def _post(client, body: bytes):
    return await client.post("/api/payments/webhook", content=body, headers={"stripe-signature": _sign(body, "whsec_abc")})


async def test_unpaid_checkout_session_grants_nothing_and_events_apply_in_order(two_areas, sub_client, client, stripe):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 1000})
    sid = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()["id"]
    ref = f"{a2}:1:{wh['id']}"
    # a delayed payment method: completed but not paid → still unpaid
    await _post(sub_client, _ev("checkout.session.completed", {"id": "cs_1", "customer": "cus_1", "subscription": "sub_1", "payment_status": "unpaid", "client_reference_id": ref}, eid="e1", created=100))
    assert db.get_subscription(sid, a2)["status"] == "unpaid"
    # the subscription becomes active (created=200), then a STALE 'canceled' from before arrives (created=150): ignored
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "active"}, eid="e2", created=200))
    assert db.get_subscription(sid, a2)["status"] == "active"
    await _post(sub_client, _ev("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1", "status": "canceled"}, eid="e3", created=150))
    assert db.get_subscription(sid, a2)["status"] == "active" and db.payment_by("stripe_subscription", "sub_1")["status"] == "active"
    # the same event delivered twice is applied once; an unknown status never counts as paid
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "incomplete"}, eid="e4", created=300))
    assert db.get_subscription(sid, a2)["status"] == "unpaid"
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "active"}, eid="e5", created=400))
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "incomplete"}, eid="e5", created=400))   # duplicate of e5
    assert db.get_subscription(sid, a2)["status"] == "active"
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1"}, eid="e6", created=500))                       # no status → fail closed
    assert db.get_subscription(sid, a2)["status"] == "unpaid"


async def test_refund_withdraws_access_via_the_invoice(two_areas, sub_client, client, stripe, monkeypatch):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 1000})
    sid = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()["id"]
    await _post(sub_client, _ev("checkout.session.completed", {"id": "cs_1", "customer": "cus_1", "subscription": "sub_1", "payment_status": "paid", "client_reference_id": f"{a2}:1:{wh['id']}"}, eid="e1", created=100))
    assert db.get_subscription(sid, a2)["status"] == "active"

    async def fake(method, path, data=None):
        assert path == "/invoices/in_1"
        return {"id": "in_1", "subscription": "sub_1"}
    monkeypatch.setattr(payments, "_stripe", fake)
    await _post(sub_client, _ev("charge.refunded", {"id": "ch_1", "customer": "cus_1", "invoice": "in_1"}, eid="e2", created=200))
    assert db.get_subscription(sid, a2)["status"] == "unpaid" and db.payment_by("stripe_subscription", "sub_1")["status"] == "unpaid"


async def test_listing_turning_paid_demotes_free_riders_and_publisher_cannot_activate_unpaid(two_areas, sub_client, client, stripe):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    sid = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()["id"]
    assert db.get_subscription(sid, a2)["status"] == "active"
    r = await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 2900})
    assert r.status_code == 200
    assert db.get_subscription(sid, a2)["status"] == "unpaid"                      # subscribed while free: now waits for payment
    assert db.active_subscriptions(1, wh["id"]) == []
    r = await client.put(f"/api/webhooks/{wh['id']}/subscribers/{sid}", json={"status": "active"})
    assert r.status_code == 409                                                     # the publisher cannot hand it out for free
    # the admin switch coming on later demotes too
    payments.save_config({"enabled": False})
    sid2 = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()["id"]
    db.set_subscription_status(sid2, 1, "active")
    r = await client.put("/api/payments/config", json={"enabled": True})
    assert r.status_code == 200 and db.get_subscription(sid2, a2)["status"] == "unpaid"


async def test_no_double_checkout_one_trial_and_unsubscribe_cancels_stripe(two_areas, sub_client, client, stripe):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 1000, "trial_days": 7})
    sid = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()["id"]
    r1 = await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})
    r2 = await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})
    assert r1.json()["url"] == r2.json()["url"] and len([c for c in stripe if c[1] == "/checkout/sessions"]) == 1   # the pending session is reused
    assert stripe[-1][2]["subscription_data[trial_period_days]"] == "7"
    await _post(sub_client, _ev("checkout.session.completed", {"id": "cs_1", "customer": "cus_1", "subscription": "sub_1", "payment_status": "paid", "client_reference_id": f"{a2}:1:{wh['id']}"}, eid="e1", created=100))
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "trialing", "trial_end": 1_800_000_000}, eid="e2", created=200))
    assert (await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})).status_code == 409   # a live subscription: no second one
    # unsubscribe cancels at Stripe
    r = await sub_client.delete(f"/api/subscriptions/{sid}")
    assert r.status_code == 200 and stripe[-1][:2] == ("DELETE", "/subscriptions/sub_1")
    assert db.get_payment(a2, 1, wh["id"])["status"] == "canceled"
    # re-subscribing gets no second trial
    await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})
    r = await sub_client.post("/api/payments/checkout", json={"publisher_area_id": 1, "key": wh["id"]})
    assert r.status_code == 200 and "subscription_data[trial_period_days]" not in stripe[-1][2]


async def test_paused_by_publisher_survives_a_payment_lapse_and_return(two_areas, sub_client, client, stripe):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"price_cents": 1000})
    sid = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()["id"]
    await _post(sub_client, _ev("checkout.session.completed", {"id": "cs_1", "customer": "cus_1", "subscription": "sub_1", "payment_status": "paid", "client_reference_id": f"{a2}:1:{wh['id']}"}, eid="e1", created=100))
    await client.put(f"/api/webhooks/{wh['id']}/subscribers/{sid}", json={"status": "paused"})
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "past_due"}, eid="e2", created=200))
    assert db.get_subscription(sid, a2)["status"] == "paused"
    await _post(sub_client, _ev("customer.subscription.updated", {"id": "sub_1", "customer": "cus_1", "status": "active"}, eid="e3", created=300))
    assert db.get_subscription(sid, a2)["status"] == "paused"                      # the publisher's decision is never overwritten by money


async def test_config_and_full_ledger_are_operator_only(client, two_areas, stripe):
    from app import auth
    from tests.conftest import _make_client
    other = db.create_user("admin2@example.com", "password123", is_admin=True)
    async with _make_client(auth.make_session(other["id"])) as c2:
        assert (await c2.get("/api/payments/config")).status_code == 403
        assert (await c2.put("/api/payments/config", json={"enabled": False})).status_code == 403
        assert (await c2.get("/api/payments")).json() == []                        # own listings only, and they have none
    assert (await client.get("/api/payments/config")).status_code == 200


async def test_webhook_signature_with_odd_bytes_is_a_400_not_a_500(sub_client, stripe):
    body = b'{"type":"ping"}'
    r = await sub_client.post("/api/payments/webhook", content=body, headers={"stripe-signature": b"t=1,v1=\xfc"})
    assert r.status_code == 400

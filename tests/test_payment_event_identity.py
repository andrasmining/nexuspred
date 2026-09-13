"""Billing events must identify the affected subscription, not just a customer."""
from __future__ import annotations

import pytest

from app import db, payments


def _event(kind, obj):
    return {"id": "evt_identity", "created": 100, "type": kind, "data": {"object": obj}}


def _paid(key, sid):
    return db.upsert_payment(1, 1, key, stripe_customer="cus_shared", stripe_subscription=sid, status="active")


async def test_modern_invoice_failure_withdraws_matching_subscription(admin):
    _paid("first", "sub_first")
    await payments.handle_event(_event("invoice.payment_failed", {
        "object": "invoice", "id": "in_first", "customer": "cus_shared",
        "parent": {"type": "subscription_details", "subscription_details": {"subscription": "sub_first"}},
    }))
    assert db.get_payment(1, 1, "first")["status"] == "past_due"


async def test_uncollectible_invoice_never_targets_latest_customer_payment(admin):
    _paid("first", "sub_first")
    _paid("second", "sub_second")
    await payments.handle_event(_event("invoice.marked_uncollectible", {
        "object": "invoice", "id": "in_first", "customer": "cus_shared", "subscription": "sub_first",
    }))
    assert db.get_payment(1, 1, "first")["status"] == "unpaid"
    assert db.get_payment(1, 1, "second")["status"] == "active"


async def test_dispute_resolves_unexpanded_charge_id(admin, monkeypatch):
    _paid("first", "sub_first")
    calls = []

    async def stripe(method, path, data=None):
        calls.append((method, path))
        return {"/charges/ch_first": {"id": "ch_first", "invoice": "in_first"},
                "/invoices/in_first": {"subscription": "sub_first"}}[path]

    monkeypatch.setattr(payments, "_stripe", stripe)
    await payments.handle_event(_event("charge.dispute.created", {"object": "dispute", "id": "du_first", "charge": "ch_first"}))
    assert db.get_payment(1, 1, "first")["status"] == "unpaid"
    assert calls == [("GET", "/charges/ch_first"), ("GET", "/invoices/in_first")]


async def test_refund_lookup_failure_is_retryable_not_wrong_customer_revocation(admin, monkeypatch):
    _paid("first", "sub_first")
    _paid("second", "sub_second")

    async def unavailable(*args, **kwargs):
        raise RuntimeError("Stripe temporarily unavailable")

    monkeypatch.setattr(payments, "_stripe", unavailable)
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await payments.handle_event(_event("charge.refunded", {
            "id": "ch_first", "invoice": "in_first", "customer": "cus_shared",
        }))
    assert db.get_payment(1, 1, "first")["status"] == "active"
    assert db.get_payment(1, 1, "second")["status"] == "active"


async def test_unrelated_charge_must_not_withdraw_any_customer_subscription(admin):
    _paid("first", "sub_first")
    await payments.handle_event(_event("charge.refunded", {"id": "ch_oneoff", "invoice": None, "customer": "cus_shared"}))
    assert db.get_payment(1, 1, "first")["status"] == "active"


async def test_modern_refund_uses_payment_invoice_not_customer(admin, monkeypatch):
    _paid("first", "sub_first")
    _paid("second", "sub_second")

    async def stripe(method, path, data=None):
        if path == "/invoice_payments":
            assert method == "GET"
            assert data["payment[type]"] == "payment_intent"
            assert data["payment[payment_intent]"] == "pi_first"
            return {"data": [{"invoice": "in_first"}], "has_more": False}
        assert path == "/invoices/in_first"
        return {"parent": {"type": "subscription_details", "subscription_details": {"subscription": {"id": "sub_first"}}}}

    monkeypatch.setattr(payments, "_stripe", stripe)
    await payments.handle_event(_event("charge.refunded", {"id": "ch_first", "customer": "cus_shared", "payment_intent": "pi_first"}))
    assert db.get_payment(1, 1, "first")["status"] == "unpaid"
    assert db.get_payment(1, 1, "second")["status"] == "active"


@pytest.mark.parametrize("links", [
    {"data": [{"invoice": "in_first"}, {"invoice": "in_second"}], "has_more": False},
    {"data": [{"invoice": "in_first"}], "has_more": True},
    {},
])
async def test_incomplete_or_ambiguous_invoice_lookup_is_not_acknowledged(admin, monkeypatch, links):
    _paid("first", "sub_first")

    async def stripe(method, path, data=None):
        return links

    monkeypatch.setattr(payments, "_stripe", stripe)
    with pytest.raises(RuntimeError):
        await payments.handle_event(_event("charge.refunded", {"id": "ch_first", "payment_intent": "pi_first"}))
    assert db.get_payment(1, 1, "first")["status"] == "active"


async def test_stripe_get_uses_query_parameters(admin, monkeypatch):
    from app import http

    payments.save_config({"stripe_secret_key": "sk_test_example"})
    seen = []

    class Response:
        status_code = 200
        def json(self):
            return {"data": []}

    class Client:
        async def request(self, *args, **kwargs):
            seen.append(kwargs)
            return Response()

    monkeypatch.setattr(http, "client", lambda kind: Client())
    filters = {"payment[type]": "payment_intent", "payment[payment_intent]": "pi_first"}
    await payments._stripe("GET", "/invoice_payments", filters)
    assert seen[0]["params"] == filters
    assert "data" not in seen[0]

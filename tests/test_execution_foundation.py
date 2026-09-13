"""The first execution slice: unchanged transport + scoped, durable identity."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest

from app import auth, config, context, db, risk, signals, tradovate
from app.routers import trading
from app.db import execution as ledger
from app.db import entitlements as grants
from app.execution import local, service
from app.execution.contracts import AccountTarget, ClosePosition, ExecutionError, ManualOrder, Outcome
from app.platform.workspaces import Actor, WorkspaceAccessDenied, WorkspaceId, current_actor
from tests.conftest import login_as
from tests.helpers import FakeExecutor
from tests.test_alpha76 import ticket  # noqa: F401

@pytest.fixture(autouse=True)
def fresh_ticket_limit(monkeypatch):
    monkeypatch.setattr(trading, "_TICKET_LIMIT", type(trading._TICKET_LIMIT)(30, 60))


BODY = {"lid": "L1", "spec": "DEMO11", "symbol": "MNQ1!", "action": "buy", "qty": 2}


def enabled() -> None:
    config.save_settings({"trading_enabled": True, "symbol_map": {"MNQ1!": "MNQZ6"}})


def command(admin, key="one-intent") -> ManualOrder:
    return ManualOrder.from_payload(Actor(WorkspaceId(1), admin["id"]), BODY, key)


async def test_optional_key_preserves_legacy_json_and_repeat_intents(client, ticket):
    enabled()
    first = await client.post("/api/orders/manual", json=BODY)
    second = await client.post("/api/orders/manual", json=BODY)
    assert first.status_code == second.status_code == 200
    assert first.json() == {"status": "submitted", "order_id": 1001, "contract": "MNQZ6", "account": "DEMO11",
                            "action": "buy", "qty": 2, "order_type": "Market"}
    assert first.headers["x-execution-command-id"] != second.headers["x-execution-command-id"]
    assert len(ticket.of("place")) == 2
    assert first.headers["x-execution-outcome"] == "accepted"  # not filled


async def test_same_key_replays_without_new_broker_reads_even_after_config_changes(client, ticket):
    enabled()
    headers = {"Idempotency-Key": "one-click"}
    first = await client.post("/api/orders/manual", json=BODY, headers=headers)
    config.save_settings({"trading_enabled": False, "symbol_map": {"MNQ1!": "ESZ6"}})
    second = await client.post("/api/orders/manual", json=BODY, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["idempotency-replayed"] == "true"
    assert len(ticket.of("place")) == len(ticket.of("resolve")) == 1
    row = ledger.get(1, "one-click")
    assert row["outcome"] == "accepted" and json.loads(row["target_json"])["contract"] == "MNQZ6"


@pytest.mark.parametrize("change", [{"qty": 3}, {"spec": "OTHER"}, {"lid": "L2"}, {"action": "sell"}, {"symbol": "ES"}])
async def test_same_key_cannot_change_intent(client, ticket, change):
    enabled()
    headers = {"Idempotency-Key": "fixed"}
    assert (await client.post("/api/orders/manual", json=BODY, headers=headers)).status_code == 200
    response = await client.post("/api/orders/manual", json={**BODY, **change}, headers=headers)
    assert response.status_code == 409 and "different instruction" in response.json()["detail"]
    assert len(ticket.of("place")) == 1


async def test_concurrent_duplicate_is_claimed_once_without_holding_sqlite_transaction(client, ticket, monkeypatch):
    enabled()
    entered, release = asyncio.Event(), asyncio.Event()
    original = ticket.place_order

    async def blocked(**kw):
        entered.set()
        await release.wait()
        return await original(**kw)

    monkeypatch.setattr(ticket, "place_order", blocked)
    headers = {"Idempotency-Key": "concurrent"}
    first = asyncio.create_task(client.post("/api/orders/manual", json=BODY, headers=headers))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        duplicate = await client.post("/api/orders/manual", json=BODY, headers=headers)
        assert duplicate.status_code == 409 and "in progress" in duplicate.json()["detail"]
        # A separate writer works while the broker is awaited: no long DB lock.
        await asyncio.to_thread(grants.set_manual_trading, 1, True)
        assert ledger.get(1, "concurrent")["outcome"] == "dispatching"
    finally:
        release.set()
        result = await first
    assert result.status_code == 200 and len(ticket.of("place")) == 1


@pytest.mark.parametrize("error", [tradovate.OrderOutcomeUnknown("lost reply"), TimeoutError("lost reply"), RuntimeError("disconnected")])
async def test_uncertain_broker_outcome_is_persisted_and_never_retried(client, ticket, monkeypatch, error):
    enabled()
    attempts = []

    async def lost(**kw):
        attempts.append(kw)
        raise error

    monkeypatch.setattr(ticket, "place_order", lost)
    headers = {"Idempotency-Key": "lost-answer"}
    first = await client.post("/api/orders/manual", json=BODY, headers=headers)
    assert first.status_code == 502 and "unknown" in first.json()["detail"].lower()
    assert ledger.get(1, "lost-answer")["outcome"] == "unknown"
    db.mark_uninitialized()
    db.reset_caches()
    ledger.recover_incomplete()
    second = await client.post("/api/orders/manual", json=BODY, headers=headers)
    assert second.status_code == 502 and len(attempts) == 1
    assert ledger.get(1, "lost-answer")["response_json"] == "{}"


async def test_explicit_rejection_remains_rejected_and_no_replay(client, ticket, monkeypatch):
    enabled()
    attempts = []

    async def rejected(**kw):
        attempts.append(kw)
        raise tradovate.TradovateError("broker says no")

    monkeypatch.setattr(ticket, "place_order", rejected)
    for _ in range(2):
        assert (await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "rejected"})).status_code == 502
    assert len(attempts) == 1 and ledger.get(1, "rejected")["outcome"] == "rejected"


@pytest.mark.parametrize("reply", [None, {}, {"status": "submitted"}, {"status": "unknown", "order_id": 123}])
async def test_malformed_acknowledgement_is_not_success(client, ticket, monkeypatch, reply):
    enabled()

    async def bad(**kw):
        return reply

    monkeypatch.setattr(ticket, "place_order", bad)
    response = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "bad-ack"})
    assert response.status_code == 502 and ledger.get(1, "bad-ack")["outcome"] == "unknown"


@pytest.mark.parametrize("operation", ["claim", "dispatch"])
async def test_ledger_write_failure_before_dispatch_never_calls_broker(client, ticket, monkeypatch, operation):
    enabled()

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(ledger, operation, unavailable)
    response = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "disk-full"})
    assert response.status_code == 503 and ticket.of("place") == []


async def test_recording_failure_after_acknowledgement_never_causes_resubmit(client, ticket, monkeypatch):
    enabled()

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "finish", unavailable)
        first = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "ack-lost"})
    assert first.status_code == 502 and "acknowledged" in first.json()["detail"]
    assert ledger.get(1, "ack-lost")["outcome"] == "dispatching"
    assert ledger.recover_incomplete() == 1
    second = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "ack-lost"})
    assert second.status_code == 502 and len(ticket.of("place")) == 1


async def test_cancelled_broker_await_is_unknown_not_retryable(client, ticket, monkeypatch):
    enabled()
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []

    async def hanging(**kw):
        attempts.append(kw)
        entered.set()
        await release.wait()

    monkeypatch.setattr(ticket, "place_order", hanging)
    task = asyncio.create_task(client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "cancelled"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
    assert ledger.get(1, "cancelled")["outcome"] == "unknown"
    assert (await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "cancelled"})).status_code == 502
    assert len(attempts) == 1


async def test_entitlement_is_rechecked_after_broker_lookup(client, ticket, monkeypatch):
    enabled()

    async def resolve(target):
        grants.set_manual_trading(1, False)
        return target

    monkeypatch.setattr(ticket, "resolve_contract", resolve)
    result = await client.post("/api/orders/manual", json=BODY)
    assert result.status_code == 403 and not ticket.of("place")


@pytest.mark.parametrize("mode", ["denied", "unreadable"])
async def test_commercial_restrictions_do_not_block_close_or_flatten(client, ticket, monkeypatch, mode):
    enabled()
    grants.set_manual_trading(1, False)
    if mode == "unreadable":
        def broken(*args):
            raise sqlite3.OperationalError("unavailable")
        monkeypatch.setattr(service.entitlements, "for_workspace", broken)
    manual = await client.post("/api/orders/manual", json={**BODY, "action": "sell", "reduce_only": True, "effect": "emergency"})
    assert manual.status_code == (403 if mode == "denied" else 503) and not ticket.of("place")
    # Ledger writes must not become a prerequisite for emergency operations.
    def broken_ledger(*args, **kwargs):
        raise AssertionError("close/flatten must not use the manual ledger")
    monkeypatch.setattr(ledger, "claim", broken_ledger)
    config.save_settings({"trading_enabled": False})
    closed = await client.post("/api/positions/close", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQZ6"})
    assert closed.status_code == 200 and ticket.of("liquidate")
    partial = {"status": "ok", "flattened": 1, "cancelled": 0, "accounts": 2, "errors": ["other account disconnected"]}
    async def flatten():
        return partial
    monkeypatch.setattr(signals, "flatten_all", flatten)
    flattened = await client.post("/api/flatten-all")
    assert flattened.status_code == 200 and flattened.json() == partial


async def test_service_rechecks_membership_after_await(client, ticket, monkeypatch):
    enabled()
    async def resolve(target):
        with db._connect() as c:
            c.execute("DELETE FROM memberships WHERE user_id=? AND area_id=?", (1, 1))
        return target
    monkeypatch.setattr(ticket, "resolve_contract", resolve)
    response = await client.post("/api/orders/manual", json=BODY)
    assert response.status_code == 403 and not ticket.of("place")


async def test_platform_admin_has_no_implicit_cross_workspace_execution(admin, ticket):
    customer = db.create_user("customer@example.com", "password123")
    aid = db.user_primary_area(customer["id"])
    wrong = replace(command(admin), actor=Actor(WorkspaceId(aid), admin["id"]))
    with pytest.raises(ExecutionError, match="membership") as err:
        await service.execution.place_manual_order(wrong)
    assert err.value.code == "forbidden" and not ticket.of("place")
    assert ledger.get(aid, wrong.command_id) is None


@pytest.mark.parametrize("role", ["user", "broadcaster", "admin"])
async def test_existing_roles_retain_own_workspace_trading(client, admin, ticket, role):
    customer = db.create_user(f"customer-{role}@example.com", "password123")
    db.set_role(customer["id"], role)
    aid = db.user_primary_area(customer["id"])
    with context.use_area(aid):
        enabled()
    login_as(client, auth.make_session(customer["id"]))
    result = await client.post("/api/orders/manual", json={**BODY, "area_id": 1, "user_id": admin["id"]},
                               headers={"Idempotency-Key": "tenant-owned"})
    assert result.status_code == 200 and ledger.get(1, "tenant-owned") is None
    assert ledger.get(aid, "tenant-owned")["actor_user_id"] == customer["id"]


async def test_same_key_in_two_workspaces_is_independent_and_reads_are_scoped(client, admin, monkeypatch):
    customer = db.create_user("other@example.com", "password123")
    aid = db.user_primary_area(customer["id"])
    executors = {1: FakeExecutor("DEMO11"), aid: FakeExecutor("DEMO11")}
    monkeypatch.setattr(local, "resolve_account", lambda target: executors[context.get_area()])
    enabled()
    with context.use_area(aid):
        enabled()
    await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "shared"})
    await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "only-admin"})
    login_as(client, auth.make_session(customer["id"]))
    result = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "shared"})
    assert result.status_code == 200 and len(executors[aid].of("place")) == 1 and len(executors[1].of("place")) == 2
    assert (await client.get("/api/execution/commands/only-admin")).status_code == 404
    own = await client.get("/api/execution/commands/shared")
    assert own.status_code == 200 and own.json()["outcome"] == "accepted"
    assert "request_json" not in own.json()


async def test_readonly_membership_cannot_place_close_or_flatten(admin, ticket):
    with db._connect() as c:
        c.execute("UPDATE memberships SET role='viewer' WHERE area_id=? AND user_id=?", (1, admin["id"]))
    cmd = command(admin)
    for operation in (service.execution.place_manual_order(cmd),
                      service.execution.close_position(ClosePosition(cmd.actor, cmd.account, "MNQZ6")),
                      service.execution.flatten(cmd.actor)):
        with pytest.raises(ExecutionError) as exc:
            await operation
        assert exc.value.code == "forbidden"
    assert not ticket.calls


async def test_support_view_still_blocks_writes(client, admin, ticket):
    user = db.create_user("support-target@example.com", "password123")
    result = await client.post(f"/api/users/{user['id']}/support")
    assert result.status_code == 200
    from app import web
    cookie = web.make_support_cookie(admin["id"], db.user_primary_area(user["id"]), user["email"])
    client.headers["cookie"] += f"; {web.SUPPORT_COOKIE}={cookie}"
    for endpoint, body in (("/api/orders/manual", BODY), ("/api/positions/close", BODY), ("/api/flatten-all", {})):
        response = await client.post(endpoint, json=body)
        assert response.status_code == 403 and "read-only" in response.json()["detail"]
    assert not ticket.calls


async def test_real_adapter_risk_guard_is_still_final(client, monkeypatch):
    enabled()
    s = tradovate.TradovateSession(0, {"name": "login", "lid": "L1", "token": "unused", "environment": "demo"})
    s.area_id = 1
    s.accounts = [{"id": 11, "spec": "DEMO11"}]
    executor = tradovate.AccountExecutor(s, s.accounts[0])
    async def resolve(symbol):
        return "MNQZ6"
    async def never_send(*args, **kwargs):
        pytest.fail("risk lock was bypassed")
    monkeypatch.setattr(executor, "resolve_contract", resolve)
    monkeypatch.setattr(s, "_request", never_send)
    monkeypatch.setattr(local, "resolve_account", lambda target: executor)
    monkeypatch.setattr(risk, "is_locked", lambda area_id, spec: "daily loss")
    response = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "risk-locked"})
    assert response.status_code == 502 and "risk guard" in response.json()["detail"]
    assert ledger.get(1, "risk-locked")["outcome"] == "rejected"


async def test_no_credential_or_raw_payload_in_command_ledger(client, ticket):
    enabled()
    body = {**BODY, "token": "PRIVATE_MARKER", "password": "PRIVATE_MARKER", "extra": {"secret": "PRIVATE_MARKER"}}
    assert (await client.post("/api/orders/manual", json=body, headers={"Idempotency-Key": "safe"})).status_code == 200
    assert "PRIVATE_MARKER" not in json.dumps(ledger.get(1, "safe"))


def test_current_actor_requires_explicit_authenticated_context(admin):
    tok = context.set_area(None)
    try:
        with pytest.raises(WorkspaceAccessDenied):
            current_actor(admin["id"])
    finally:
        context.reset_area(tok)


@pytest.mark.parametrize("key", ["", "x" * 129, "with spaces", "bad/key", "ünicode"])
async def test_invalid_idempotency_key_never_executes(client, ticket, key):
    enabled()
    if not key.isascii():
        with pytest.raises(ExecutionError):
            ManualOrder.from_payload(Actor(WorkspaceId(1), 1), BODY, key)
    else:
        assert (await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": key})).status_code == 400
    assert not ticket.of("place")


def test_command_is_versioned_serializable_and_price_normalized(admin):
    cmd = replace(command(admin), order_type="Limit", price="123.25")
    data = json.loads(cmd.intent_json())
    assert data["version"] == 1 and data["source"] == "manual"
    assert type(cmd.price) is float and data["price"] == 123.25
    assert "command_id" not in data
    assert replace(cmd, command_id="retry").fingerprint() == cmd.fingerprint()


@pytest.mark.parametrize("reply", [{"order_id": True}, {"order_id": {"raw": 1}}, {"order_id": 0},
                                    {"order_id": 123, "status": None}])
async def test_invalid_typed_ack_is_unknown(client, ticket, monkeypatch, reply):
    enabled()
    async def answer(**kw):
        return reply
    monkeypatch.setattr(ticket, "place_order", answer)
    response = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "invalid-id"})
    assert response.status_code == 502 and ledger.get(1, "invalid-id")["outcome"] == "unknown"


async def test_unresolved_contract_is_not_dispatched(client, ticket, monkeypatch):
    enabled()
    async def resolve(target):
        return None
    monkeypatch.setattr(ticket, "resolve_contract", resolve)
    response = await client.post("/api/orders/manual", json=BODY, headers={"Idempotency-Key": "unresolved"})
    assert response.status_code == 503 and not ticket.of("place")


async def test_status_storage_failure_is_explicit(client, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("disk unreadable")
    monkeypatch.setattr(ledger, "get", broken)
    assert (await client.get("/api/execution/commands/unknown-id")).status_code == 503

"""alpha.101 (review round 8): the verified findings of the security, trade-path,
performance, module, frontend and stability reviews — one regression each."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import backups, canary, config, context, db, history, mailer, readiness, releases, signals, web
from app.commercial import workspaces
from app.commercial.workspaces import Actor, WorkspaceAccessDenied
from app.db import execution as ledger
from tests.test_alpha93 import _client, trio  # noqa: F401 - the three-role fixture


# ================================================== trade path: the close waits
async def test_manual_close_waits_for_an_entry_that_is_not_tracked_yet(area):
    """An entry holds its lock for the whole handler but only becomes a tracked
    trade at the very end. Before this, a manual close derived its keys from the
    tracked map alone, took a different lock and could liquidate between the
    entry's fill and its protective stop."""
    root = "MNQ"
    key = f"{area}:live:wh_1:{root}"
    assert signals.inflight_lock_keys(area, root) == []

    started, release = asyncio.Event(), asyncio.Event()

    async def slow_entry():
        signals._inflight_add(area, root, key)
        try:
            async with signals._trade_lock(key):
                started.set()
                await release.wait()
        finally:
            signals._inflight_drop(area, root, key)

    task = asyncio.create_task(slow_entry())
    await started.wait()
    # The close would find nothing in the tracked map — the in-flight registry is
    # what tells it there is a signal running for this symbol right now.
    assert signals.inflight_lock_keys(area, root) == [key]
    lock = signals._trade_lock(key)
    assert lock.locked()

    waiting = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0.02)
    assert not waiting.done()                      # the close is held back, as it must be
    release.set()
    await task
    await asyncio.wait_for(waiting, timeout=1.0)
    lock.release()
    assert signals.inflight_lock_keys(area, root) == []


# ================================================== trade path: unknown outcome
async def test_copy_order_with_an_unknown_outcome_is_never_re_sent():
    """A placement that timed out may be resting at the follower's broker.
    Re-sending it would double the position, so it is held back instead."""
    from app.copy import orders as copy_orders

    class _Runner:
        followers = [{"spec": "F1", "enabled": True}]
        follower_err: dict = {}
        follower_err_at: dict = {}

    mirror = copy_orders.OrderMirror.__new__(copy_orders.OrderMirror)
    mirror._done_at = {}
    mirror._unknown = {}
    mirror.r = _Runner()

    assert mirror._held_back("F1", 42) is False
    mirror._unknown[mirror._twin_key("F1", 42)] = time.monotonic()
    assert mirror._held_back("F1", 42) is True, "an unknown outcome must block a re-send"
    # …and it expires rather than blocking the mirror for the life of the process
    mirror._unknown[mirror._twin_key("F1", 42)] = time.monotonic() - copy_orders.UNKNOWN_HOLD_S - 1
    assert mirror._held_back("F1", 42) is False
    assert copy_orders.UNKNOWN_HOLD_S > copy_orders.DONE_HOLD_S


# ================================================== assisted support may trade
async def test_granted_support_window_reaches_the_execution_service(trio):
    """PR #25 put a workspace-membership check in front of manual orders, close
    and the emergency flatten. A support admin has no membership row, so the
    write window the customer granted stopped working — including the kill switch."""
    admin, _bc, user = trio
    area = db.user_primary_area(user["id"])

    # the customer themselves: always
    workspaces.authorize(Actor(workspaces.workspace_id(area), user["id"]), write=True)

    # an admin without a granted window: never
    with pytest.raises(WorkspaceAccessDenied):
        workspaces.authorize(Actor(workspaces.workspace_id(area), admin["id"]), write=True)

    web.set_support_grant(area, 24, admin["email"])
    workspaces.authorize(Actor(workspaces.workspace_id(area), admin["id"], True), write=True)

    # a support session without the grant flag is still refused …
    with pytest.raises(WorkspaceAccessDenied):
        workspaces.authorize(Actor(workspaces.workspace_id(area), admin["id"]), write=True)
    # … and revoking the window stops the next order
    web.set_support_grant(area, 0, admin["email"])
    with pytest.raises(WorkspaceAccessDenied):
        workspaces.authorize(Actor(workspaces.workspace_id(area), admin["id"], True), write=True)


def test_current_actor_never_takes_the_support_flag_from_a_payload(area):
    """The flag comes from the gate's own support state, not from anything a
    client can send."""
    assert workspaces.current_actor(1).support is False
    assert workspaces.current_actor(1, {"area_id": area}).support is False        # a support view without write
    assert workspaces.current_actor(1, {"write": True}).support is True
    assert workspaces.current_actor(1, "write").support is False                  # not a dict: ignored


# ================================================== security
async def test_quota_and_feature_changes_respect_the_bootstrap_admin_rule(trio):
    """Every other cross-admin action is reserved for the bootstrap admin; the
    quota and feature endpoints skipped that guard."""
    bootstrap, _bc, other = trio
    db.set_role(other["id"], "admin")
    helper = db.create_user("helper@example.com", "password123", is_admin=True)
    db.set_role(helper["id"], "admin")

    async with _client(helper["id"]) as c:
        r = await c.put(f"/api/users/{other['id']}/quota", json={"webhooks": 0})
        assert r.status_code == 403, r.text
        r = await c.post(f"/api/users/{other['id']}/features", json={"feature": "discord_signals", "enabled": False})
        assert r.status_code == 403, r.text

    async with _client(bootstrap["id"]) as c:                    # user 1 still may
        r = await c.put(f"/api/users/{other['id']}/quota", json={"webhooks": 3})
        assert r.status_code == 200, r.text


async def test_heartbeat_url_is_checked_again_before_every_ping(area, monkeypatch):
    """A short-TTL record can point an accepted host at the internal network
    after the save, so the check runs at send time too."""
    readiness.save_heartbeat("https://hc-ping.com/abc", 60)
    sent: list = []

    class _Client:
        async def get(self, url, **kw):
            sent.append(url)
            raise AssertionError("must not be sent")

    monkeypatch.setattr(readiness.http, "client", lambda _p: _Client())
    monkeypatch.setattr(readiness.security, "check_outbound_url", lambda url: "resolves to a private address")
    assert await readiness.heartbeat_once() is False
    assert sent == []
    assert "private address" in readiness.heartbeat_status()["error"]


def test_a_mail_subject_can_never_break_the_message(area):
    """A title with a newline in it used to make every recipient's row fail."""
    assert mailer._header_safe("Wartung\r\nBcc: someone@else") == "Wartung Bcc: someone@else"
    assert mailer._header_safe("  spaced   out  ") == "spaced out"
    assert len(mailer._header_safe("x" * 900)) <= 300


# ================================================== canary isolation
async def test_canary_runs_in_its_own_book(area):
    """It used to rehearse in the operator's own simulator: same trade key, same
    position book, so it overwrote a tracked scenario trade."""
    res = await canary.run_once()
    assert res["ok"] is True
    assert canary.CANARY_AREA != context.DEFAULT_AREA_ID
    assert signals._sim_active.get(context.DEFAULT_AREA_ID) in (None, {})
    assert canary._settings()["symbol_map"]["MNQ1!"]
    assert canary._webhook()["id"] == "canary"


# ================================================== backups
def test_rotation_keeps_the_newest_verified_snapshot(area):
    """Recency alone would drop the last good backup as soon as newer ones fail."""
    rows = [
        {"name": "fluxbridge-2026-09-10.db", "created_at": "2026-09-10T00:00:00Z", "roles": ["daily"], "verified": True},
        {"name": "fluxbridge-2026-09-11.db", "created_at": "2026-09-11T00:00:00Z", "roles": ["daily"], "verified": False},
        {"name": "fluxbridge-2026-09-12.db", "created_at": "2026-09-12T00:00:00Z", "roles": ["daily"], "verified": False},
    ]
    kept, gone = backups.rotate(rows, {"keep_daily": 2, "keep_weekly": 0, "keep_monthly": 0})
    names = {r["name"] for r in kept}
    assert "fluxbridge-2026-09-10.db" in names, "the only verified snapshot must survive"
    assert "fluxbridge-2026-09-10.db" not in gone


def test_a_broken_snapshot_never_replaces_the_live_database(area, tmp_path, monkeypatch):
    live_before = db.DB_FILE.read_bytes()
    bad = backups.backup_dir() / "fluxbridge-broken.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)          # opens, fails its integrity check
    marker = backups.Path(config.DATA_DIR) / backups.RESTORE_FILE
    marker.write_text("fluxbridge-broken.db", encoding="utf-8")

    with pytest.raises(backups.sqlite3.DatabaseError):
        backups.apply_pending_restore()
    assert marker.exists(), "failed restore must retain retry intent"
    assert db.DB_FILE.read_bytes() == live_before, "the live database must be untouched"


# ================================================== retention
def test_the_tables_that_only_grow_are_pruned(area):
    old = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
    with db._connect() as c:
        c.execute("INSERT INTO audit_log(created_at,actor_user_id,actor_email,action,target,detail) VALUES(?,?,?,?,?,?)",
                  (old, None, "x@y", "login_ok", "", ""))
    before = len(db.list_audit(limit=500))
    assert db.prune_audit() >= 1
    assert len(db.list_audit(limit=500)) == before - 1

    ledger.claim(area, "settled-old", 1, "h" * 32, "{}")
    ledger.dispatch(area, "settled-old", {})
    ledger.finish(area, "settled-old", "accepted", response={})
    ledger.claim(area, "still-unknown", 1, "h" * 32, "{}")
    ledger.finish(area, "still-unknown", "unknown", error_code="interrupted")
    with db._connect() as c:
        c.execute("UPDATE execution_commands SET updated_at=? WHERE area_id=?", (old, area))
    assert ledger.prune_commands() == 1, "a settled command goes, an unresolved one stays"
    assert ledger.get(area, "still-unknown") is not None
    assert ledger.get(area, "settled-old") is None


def test_the_history_queue_is_bounded_and_never_blocks_the_order_path(monkeypatch):
    """A stalled writer must not grow the queue without limit; the trade never
    waits on its own bookkeeping."""
    assert history.QUEUE_MAX > 0
    assert history._q.maxsize == history.QUEUE_MAX
    monkeypatch.setattr(history, "_running", True)
    monkeypatch.setattr(history, "QUEUE_MAX", 3)
    import queue as _queue
    monkeypatch.setattr(history, "_q", _queue.Queue(maxsize=3))
    monkeypatch.setattr(history, "_dropped", 0)
    for i in range(10):
        history._submit("event", 1, {"n": i})            # must not raise and must not block
    assert history.backlog() == 3
    assert history.dropped() == 7


# ================================================== ledger durability
def test_the_claim_is_durable_and_the_outcome_writes_are_not(area):
    """The claim must survive a crash before any broker call — a lost one could
    send a second order. The writes after the broker answered are covered by the
    restart's reconciliation pass instead."""
    modes: list[str] = []
    real = ledger._connection

    import contextlib

    @contextlib.contextmanager
    def spy(*, durable: bool = True):
        modes.append("FULL" if durable else "NORMAL")
        with real(durable=durable) as c:
            yield c

    ledger._connection = spy
    try:
        ledger.claim(area, "dur-1", 1, "h" * 32, "{}")
        ledger.dispatch(area, "dur-1", {})
        ledger.finish(area, "dur-1", "accepted", response={})
    finally:
        ledger._connection = real
    assert modes == ["FULL", "NORMAL", "NORMAL"]
    assert ledger.BUSY_TIMEOUT_S >= 5.0, "a batch write must not make a claim fail"


# ================================================== settings history
def test_a_restart_does_not_record_a_version_claiming_everything_changed(area):
    """The fingerprints live in memory; without a baseline every restart wrote a
    full diff and pushed the real ones out of the retention window."""
    config.save_settings({"allowed_symbols": ["MNQ"]})
    config.save_settings({"allowed_symbols": ["MNQ", "ES"]})
    before = len(db.list_settings_versions(area, limit=30))

    config._fingerprints.clear()                    # what a restart looks like
    config.save_settings({"allowed_symbols": ["MNQ", "ES"]})   # nothing actually changed
    assert len(db.list_settings_versions(area, limit=30)) == before

    config._fingerprints.clear()
    config.save_settings({"allowed_symbols": ["MNQ", "ES", "NQ"]})
    versions = db.list_settings_versions(area, limit=30)
    assert len(versions) == before + 1
    assert versions[0]["keys"] == ["allowed_symbols"], "only the key that changed"


# ================================================== release mail
def test_a_release_is_not_marked_sent_while_no_mailer_is_configured(area, monkeypatch):
    monkeypatch.setattr(releases, "sections", lambda: {"9.9.9": "- something new"})
    monkeypatch.setattr(mailer, "configured", lambda: False)
    assert releases.mail_release("9.9.9") == 0
    assert db.meta_get(releases.MAILED_KEY) != "9.9.9", "the version must stay mailable"

    monkeypatch.setattr(mailer, "configured", lambda: True)
    releases.mail_release("9.9.9")
    assert db.meta_get(releases.MAILED_KEY) == "9.9.9"


# ================================================== single worker
def test_a_second_worker_is_refused(monkeypatch):
    from app import main as app_main
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError, match="one process"):
        app_main._refuse_multiple_workers()
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    app_main._refuse_multiple_workers()
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    app_main._refuse_multiple_workers()


# ================================================== notifications input
async def test_a_malformed_id_list_is_a_client_error(area, admin):
    async with _client(admin["id"]) as c:
        r = await c.post("/api/notifications/read", json={"ids": ["not-a-number"]})
        assert r.status_code == 400, r.text
        r = await c.post("/api/notifications/read", json={"ids": "all"})
        assert r.status_code == 400, r.text
        r = await c.post("/api/notifications/read", json={})
        assert r.status_code == 200, r.text


# ================================================== alpha.102: the order burst
async def test_a_copy_group_s_followers_leave_in_one_burst(area):
    """Six followers on ONE broker login used to queue 60 ms apart, so the last
    account was filled ~300 ms after the first. They now leave together, and past
    the bucket the sustained rate is exactly what it was."""
    import time as _t
    from app import tradovate

    class _Sess:
        def __init__(self):
            self._prio_lock = asyncio.Lock()
            self._pace_lock = asyncio.Lock()
            self._last_prio = _t.monotonic()
            self._last_sent = 0.0
            self._order_tokens = tradovate.ORDER_BURST
            self.penalty_until = -1e9
            self.rate_limits = 0
            self.sent = []

        async def _request_raw(self, method, path, **kw):
            self.sent.append(_t.monotonic())
            return {"orderId": len(self.sent)}

        _take_order_token = tradovate.TradovateSession._take_order_token
        _request_paced = tradovate.TradovateSession._request_paced

    assert tradovate.ORDER_BURST >= 6, "a six-follower group must fit in the bucket"
    s = _Sess()
    t0 = _t.monotonic()
    await asyncio.gather(*(s._request_paced("POST", "/order/placeorder", json={}) for _ in range(6)))
    spread = (max(s.sent) - min(s.sent))
    assert spread < tradovate.PRIORITY_SPACING_S, f"followers must not queue: {spread*1000:.0f} ms apart"

    # the bucket is spent: a further burst falls back to the old sustained spacing
    s._order_tokens = 0.0
    s._last_prio = _t.monotonic()
    t0 = _t.monotonic()
    await asyncio.gather(*(s._request_paced("POST", "/order/placeorder", json={}) for _ in range(3)))
    assert _t.monotonic() - t0 >= 2 * tradovate.PRIORITY_SPACING_S - 0.02


def test_the_order_lane_can_be_switched_off_without_dividing_by_zero(area, monkeypatch):
    """An operator who knows their own budget may set the spacing to 0."""
    from app import tradovate

    class _S:
        _order_tokens = 0.0
        _last_prio = 0.0
    monkeypatch.setattr(tradovate, "PRIORITY_SPACING_S", 0.0)
    assert tradovate.TradovateSession._take_order_token(_S()) == 0.0


# ================================================== alpha.103: nothing fans out serially
def test_no_broker_call_is_made_one_item_at_a_time_in_a_loop():
    """A guard against the pattern this release removed: a loop that awaits a
    broker call per login / per follower / per contract serialises work that is
    independent. Anything new that needs it must be listed here with a reason."""
    import ast
    import pathlib

    BROKER = ("place_order", "place_oco", "cancel_order", "modify_order", "liquidate",
              "positions_snapshot", "orders_snapshot", "working_orders", "_close_contract",
              "_cancel_working", "_flatten_account", "flatten_all")
    # (file, function): why this one is sequential on purpose
    ALLOWED = {
        ("app/engine/common.py", "_place_stop_with_retry"): "a retry, not a fan-out: attempt 2 only after attempt 1 failed",
        ("app/engine/common.py", "_cancel_working"): "a retry of the same list read, not a fan-out",
    }

    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for loop in [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.AsyncFor))]:
                body = ast.Module(body=loop.body, type_ignores=[])
                calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)]
                if any(getattr(getattr(c, "func", None), "attr", "") == "gather" for c in calls):
                    continue                     # the loop only builds the fan-out
                hit = [getattr(a.value.func, "attr", "") for a in ast.walk(body)
                       if isinstance(a, ast.Await) and isinstance(a.value, ast.Call)
                       and any(b in (getattr(a.value.func, "attr", "") or "") for b in BROKER)]
                if hit and (str(path), fn.name) not in ALLOWED:
                    offenders.append(f"{path}:{loop.lineno} in {fn.name}(): {sorted(set(hit))}")
    assert not offenders, "serial broker calls in a loop:\n  " + "\n  ".join(offenders)

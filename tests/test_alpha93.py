"""alpha.93: three roles — Admin / Broadcaster / User — with the support view."""
from __future__ import annotations

import httpx
import pytest

from app import auth, config, context, db, roles, web
from app import copy as cp
from app.db import core
from app.main import app


def _client(user_id: int) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    c.headers["cookie"] = f"{auth.COOKIE}={auth.make_session(user_id)}"
    return c


@pytest.fixture
def trio(admin):
    """admin (id 1) + a broadcaster + a plain user, each with their own workspace."""
    bc = db.create_user("bc@example.com", "password123", role="broadcaster")
    us = db.create_user("us@example.com", "password123", role="user")
    return admin, bc, us


# ================================================================ model
def test_roles_are_ordered_and_capabilities_follow(trio):
    admin, bc, us = trio
    assert [web.role_of(u) for u in (admin, bc, us)] == ["admin", "broadcaster", "user"]
    assert admin["is_admin"] and not bc["is_admin"] and not us["is_admin"]
    assert web.has_role(bc, "user") and web.has_role(bc, "broadcaster") and not web.has_role(bc, "admin")
    caps = web.capabilities(us)
    assert caps["webhooks"] and caps["agents"] and caps["subscribe"] and caps["follow"]      # a User consumes and runs own webhooks
    assert caps["lead"]                                                                     # own copy groups too (alpha.94)
    assert not caps["publish"] and not caps["simulator"] and not caps["admin"]
    caps = web.capabilities(bc)
    assert caps["publish"] and caps["lead"] and caps["simulator"] and caps["settings_io"]
    assert not caps["discord"] and not caps["users"] and not caps["support"]                 # Discord stays with the operator
    assert all(web.capabilities(admin).values()) or web.capabilities(admin)["role"] == "admin"


def test_first_user_is_admin_and_later_ones_default_to_user(admin):
    u = db.create_user("x@example.com", "password123")
    assert web.role_of(admin) == "admin" and web.role_of(u) == "user"
    assert db.count_admins() == 1


def test_existing_accounts_become_admins_on_migration(admin):
    """Every account created before alpha.93 had everything — it keeps it."""
    u = db.create_user("old@example.com", "password123")                       # would be a plain user today
    with core._connect() as c:
        for col in ("role", "role_request", "role_requested_at"):
            c.execute(f"ALTER TABLE users DROP COLUMN {col}")
        c.execute("UPDATE users SET is_admin=0 WHERE id=?", (u["id"],))
    db.mark_uninitialized()
    db.init()
    db.reset_caches()
    row = db.get_user(u["id"])
    assert row["role"] == "admin" and row["is_admin"] is True
    assert db.count_admins() == 2


async def test_invite_role_becomes_the_users_role(client, anon_client):
    for role in ("user", "broadcaster", "admin"):
        r = await client.post("/api/users/invite", json={"role": role, "email": f"{role}@x.com"})
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["role"] == role
        r = await anon_client.post("/register", data={"code": inv["code"], "email": f"{role}@x.com",
                                                      "password": "password123", "password2": "password123"})
        assert r.status_code == 302 and "error" not in r.headers["location"]
        assert db.get_user_by_email(f"{role}@x.com")["role"] == role
    assert (await client.post("/api/users/invite", json={"role": "king"})).status_code == 400
    # legacy callers: elevated → admin
    assert (await client.post("/api/users/invite", json={"elevated": True})).json()["role"] == "admin"


# ================================================================ gate
ROUTES = [
    # (method, path, minimal role that passes)
    ("GET", "/api/users", "admin"),
    ("GET", "/api/invites", "admin"),
    ("GET", "/api/audit", "admin"),
    ("GET", "/api/discord/status", "admin"),
    ("PUT", "/api/payments/config", "admin"),
    ("POST", "/api/news/", "admin"),
    ("GET", "/api/simulator", "broadcaster"),
    ("GET", "/api/settings/export", "user"),
    ("POST", "/api/copy/groups", "user"),
    ("GET", "/api/copy/groups/cg_x/subscribers", "broadcaster"),
    ("GET", "/api/webhooks/w1/subscribers", "broadcaster"),
    ("GET", "/api/users/directory", "broadcaster"),
    ("GET", "/api/webhooks", "user"),
    ("POST", "/api/webhooks", "user"),
    ("GET", "/api/me", "user"),
    ("GET", "/api/copy/groups", "user"),
    ("GET", "/api/marketplace", "user"),
    ("POST", "/api/agents/pairing-code", "user"),
]


@pytest.mark.parametrize("method,path,need", ROUTES)
async def test_route_policy_matrix(trio, method, path, need):
    admin, bc, us = trio
    for u in (admin, bc, us):
        async with _client(u["id"]) as c:
            r = await c.request(method, path, json={} if method != "GET" else None)
            allowed = web.has_role(u, need)
            if allowed:
                assert r.status_code != 403 or "role required" not in r.text, (u["role"], method, path, r.text)
            else:
                assert r.status_code == 403 and "role required" in r.text, (u["role"], method, path, r.text)


def test_min_role_for_matches_prefixes_and_methods():
    assert web.min_role_for("GET", "/api/news/") is None                       # everyone reads the news
    assert web.min_role_for("PUT", "/api/news/1") == "admin"
    assert web.min_role_for("GET", "/api/payments/config") is None
    assert web.min_role_for("POST", "/api/payments/config") == "admin"
    assert web.min_role_for("GET", "/api/copy/groups") is None
    assert web.min_role_for("DELETE", "/api/copy/groups/cg_1") is None                 # own groups: every role (alpha.94)
    assert web.min_role_for("POST", "/api/copy/groups") is None
    assert web.min_role_for("GET", "/api/copy/groups/cg_1/sharing") == "broadcaster"
    assert web.min_role_for("GET", "/api/webhooks/abc/sharing") == "broadcaster"
    assert web.min_role_for("POST", "/api/webhooks/abc/sharing/") == "broadcaster"
    assert web.min_role_for("GET", "/api/webhooks/abc") is None
    assert web.min_role_for("DELETE", "/api/webhooks/abc") == "user"
    assert web.min_role_for("POST", "/api/webhooks") == "user"
    assert web.min_role_for("GET", "/api/me") == "user"
    assert web.min_role_for("GET", "/api/update/status") == "admin"
    assert web.min_role_for("GET", "/api/users/directory") == "broadcaster"
    assert web.min_role_for("GET", "/api/users") == "admin"
    assert web.min_role_for("GET", "/api/support/enter") == "admin"


async def test_user_leads_own_copy_groups_but_cannot_publish(trio):
    _, _, us = trio
    async with _client(us["id"]) as c:
        r = await c.post("/api/copy/groups", json={"name": "Mine"})
        assert r.status_code == 200, r.text
        gid = r.json()["id"]
        assert (await c.put(f"/api/copy/groups/{gid}", json={"name": "Renamed"})).status_code == 200
        r = await c.put(f"/api/copy/groups/{gid}/sharing", json={"enabled": True})
        assert r.status_code == 403 and "Broadcaster" in r.text
        assert (await c.get(f"/api/copy/groups/{gid}/subscribers")).status_code == 403
        assert (await c.delete(f"/api/copy/groups/{gid}")).status_code == 200


async def test_user_runs_own_webhooks_but_cannot_publish(trio, monkeypatch):
    _, _, us = trio
    async with _client(us["id"]) as c:
        r = await c.post("/api/webhooks", json={"name": "mine"})
        assert r.status_code == 200, r.text
        wid = r.json()["id"]
        assert (await c.put(f"/api/webhooks/{wid}", json={"name": "renamed"})).status_code == 200
        assert [w["name"] for w in (await c.get("/api/webhooks")).json()] == ["renamed"]
        r = await c.put(f"/api/webhooks/{wid}/sharing", json={"enabled": True})
        assert r.status_code == 403 and "Broadcaster" in r.text


async def test_directory_is_the_broadcasters_pick_list(trio):
    admin, bc, us = trio
    async with _client(bc["id"]) as c:
        r = await c.get("/api/users/directory")
        assert r.status_code == 200
        assert r.json()["users"] == [{"id": admin["id"], "email": "admin@example.com"}, {"id": us["id"], "email": "us@example.com"}]
        assert (await c.get("/api/users")).status_code == 403
    async with _client(us["id"]) as c:
        assert (await c.get("/api/users/directory")).status_code == 403


# ================================================================ requests
async def test_user_requests_broadcaster_and_admin_approves(trio, monkeypatch):
    admin, bc, us = trio
    from app import mailer
    monkeypatch.setattr(mailer, "configured", lambda: True)                  # alpha.95: the platform mailer queues the note
    async with _client(us["id"]) as c:
        r = await c.post("/api/me/role-request", json={"role": "broadcaster"})
        assert r.status_code == 200 and r.json()["status"] == "requested"
        assert (await c.get("/api/me")).json()["role_request"] == "broadcaster"
        assert (await c.post("/api/me/role-request", json={"role": "admin"})).status_code == 400
    queued = [(r["to"], r["kind"]) for r in db.outbox_list()]
    assert queued == [("admin@example.com", "role_request")]                      # only the admin hears it
    async with _client(bc["id"]) as c:
        assert (await c.post("/api/me/role-request", json={})).status_code == 400  # already a broadcaster
    async with _client(admin["id"]) as c:
        users = (await c.get("/api/users")).json()["users"]
        pending = [u for u in users if u["id"] == us["id"]][0]
        assert pending["role_request"] == "broadcaster"
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "broadcaster"})
        assert r.status_code == 200 and r.json()["role"] == "broadcaster"
    me = db.get_user(us["id"])
    assert me["role"] == "broadcaster" and me["role_request"] == ""
    assert [a["action"] for a in db.list_audit(5)][:2] == ["role_set", "role_request"]


async def test_role_request_can_be_withdrawn(trio):
    _, _, us = trio
    async with _client(us["id"]) as c:
        await c.post("/api/me/role-request", json={})
        assert (await c.delete("/api/me/role-request")).status_code == 200
        assert (await c.get("/api/me")).json()["role_request"] == ""


# ================================================================ role changes
async def test_role_guards(trio):
    admin, bc, us = trio
    async with _client(admin["id"]) as c:
        assert (await c.post("/api/users/1/role", json={"role": "user"})).status_code == 403          # bootstrap admin stays
        assert (await c.post(f"/api/users/{us['id']}/role", json={"role": "boss"})).status_code == 400
        assert (await c.post("/api/users/999/role", json={"role": "user"})).status_code == 404
        r = await c.post(f"/api/users/{us['id']}/role", json={"role": "admin"})
        assert r.status_code == 200
    # a second admin cannot touch the first (another administrator) but may step down while one remains
    async with _client(us["id"]) as c:
        assert (await c.post("/api/users/1/role", json={"role": "user"})).status_code in (400, 403)
        assert (await c.post(f"/api/users/{us['id']}/role", json={"role": "user"})).status_code == 200
    assert db.count_admins() == 1
    # the last admin cannot demote themself (id 1 is also the bootstrap admin)
    async with _client(admin["id"]) as c:
        assert (await c.post("/api/users/1/role", json={"role": "broadcaster"})).status_code == 403
    async with _client(admin["id"]) as c:                                            # a later last admin: same answer, reason "last admin"
        await c.post(f"/api/users/{bc['id']}/role", json={"role": "admin"})
    async with _client(bc["id"]) as c:
        assert (await c.post(f"/api/users/{bc['id']}/role", json={"role": "user"})).status_code == 200   # two admins: may step down
    assert db.count_admins() == 1
    async with _client(bc["id"]) as c:
        assert (await c.post(f"/api/users/{us['id']}/role", json={"role": "admin"})).status_code == 403   # not an admin


async def test_losing_broadcaster_unpublishes_and_disables(trio, monkeypatch):
    admin, bc, us = trio
    area = db.user_primary_area(bc["id"])
    cancelled = []

    async def fake_cancel(pub, key, *, area_id=None):
        cancelled.append(key)
        return 1
    from app import payments
    monkeypatch.setattr(payments, "cancel_for_listing", fake_cancel)
    with context.use_area(area):
        wh = config.new_webhook(name="Signal")
        wh["sharing"] = {"enabled": True, "mode": "public"}
        config.save_settings({"webhooks": [wh]})
        g = cp.new_group("Lead")
        g["enabled"] = True
        g["sharing"] = {"enabled": True}
        cp.save_groups([g])
    db.upsert_subscription(db.user_primary_area(us["id"]), area, wh["id"], [], user_id=us["id"])
    db.upsert_subscription(db.user_primary_area(us["id"]), area, f"copy:{g['id']}", [], user_id=us["id"])
    async with _client(admin["id"]) as c:
        r = await c.post(f"/api/users/{bc['id']}/role", json={"role": "user"})
        assert r.status_code == 200, r.text
        assert r.json()["effects"] == {"listings": 1, "subscriptions": 2, "groups": 1}
    with context.use_area(area):
        assert config.load_settings()["webhooks"][0]["sharing"]["enabled"] is False
        g2 = cp.load_groups(area)[0]
        assert g2["enabled"] is True and g2["sharing"]["enabled"] is False           # off the marketplace, still running for own accounts
    assert db.list_subscriptions(db.user_primary_area(us["id"])) == []
    assert sorted(cancelled) == sorted([wh["id"], f"copy:{g['id']}"])
    assert db.get_user(bc["id"])["role"] == "user"
    # idempotent: nothing left to undo
    assert await roles.demote_publisher(area) == {"listings": 0, "subscriptions": 0, "groups": 0}


async def test_promotion_has_no_side_effects(trio):
    admin, bc, us = trio
    async with _client(admin["id"]) as c:
        r = await c.post(f"/api/users/{bc['id']}/role", json={"role": "admin"})
        assert r.status_code == 200 and r.json()["effects"] == {}
        r = await c.post(f"/api/users/{bc['id']}/role", json={"role": "admin"})       # same role again: no-op
        assert r.status_code == 200 and r.json()["effects"] == {}


# ================================================================ support view
async def test_support_view_is_read_only_and_scoped_to_the_target(trio):
    admin, bc, us = trio
    with context.use_area(db.user_primary_area(us["id"])):
        config.save_settings({"webhooks": [config.new_webhook(name="Theirs")]})
    async with _client(admin["id"]) as c:
        r = await c.post(f"/api/users/{us['id']}/support")
        assert r.status_code == 200 and r.json()["email"] == "us@example.com"
        cookie = r.cookies.get(web.SUPPORT_COOKIE)
        assert cookie
        c.headers["cookie"] += f"; {web.SUPPORT_COOKIE}={cookie}"
        me = (await c.get("/api/me")).json()
        assert me["support"]["email"] == "us@example.com" and me["role"] == "admin"
        assert [w["name"] for w in (await c.get("/api/webhooks")).json()] == ["Theirs"]
        r = await c.post("/api/webhooks", json={"name": "nope"})
        assert r.status_code == 403 and "read-only" in r.text
        assert (await c.put("/api/settings", json={"default_qty": 9})).status_code == 403
        r = await c.post("/api/support/exit")
        assert r.status_code == 200
    with context.use_area(db.user_primary_area(us["id"])):
        assert [w["name"] for w in config.load_settings()["webhooks"]] == ["Theirs"]
    assert [a["action"] for a in db.list_audit(5)][:2] == ["support_view", "support_view"]


async def test_support_cookie_is_bound_to_the_admin_and_expires(trio, monkeypatch):
    admin, bc, us = trio
    area = db.user_primary_area(us["id"])
    cookie = web.make_support_cookie(admin["id"], area, "us@example.com")
    assert web.read_support_cookie(cookie, admin["id"])["area_id"] == area
    assert web.read_support_cookie(cookie, bc["id"]) is None                   # someone else's cookie
    assert web.read_support_cookie("garbage", admin["id"]) is None
    assert web.read_support_cookie(None, admin["id"]) is None
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: _t.monotonic() + 10**10)
    assert web.read_support_cookie(cookie, admin["id"]) is None
    monkeypatch.undo()
    # a non-admin carrying a (stolen) support cookie stays in their own workspace
    async with _client(bc["id"]) as c:
        c.headers["cookie"] += f"; {web.SUPPORT_COOKIE}={cookie}"
        me = (await c.get("/api/me")).json()
        assert me["support"] is None
        assert (await c.post("/api/webhooks", json={"name": "ok"})).status_code == 200


async def test_support_view_targets_require_admin_and_exist(trio):
    admin, bc, us = trio
    async with _client(admin["id"]) as c:
        assert (await c.post("/api/users/999/support")).status_code == 404
        assert (await c.post(f"/api/users/{admin['id']}/support")).status_code == 400
    async with _client(bc["id"]) as c:
        assert (await c.post(f"/api/users/{us['id']}/support")).status_code == 403

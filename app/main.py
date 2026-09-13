"""FastAPI application: auth middleware, lifespan (background loops, HTTP
pool), and the routers under :mod:`app.routers` + the Discord module.

Run with a single uvicorn worker: runtime state (sessions, active trades, live
streams) is in-process by design."""
from __future__ import annotations

import asyncio
import contextlib
import gc
import secrets
from typing import Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, automations, config, context, copy, crypto, db, drawdown, health, history, http, journal, metrics, news, pnl, push, security, state, watchdog  # noqa: F401 - automations / metrics subscribe to the event bus on import
from .discord_signals.routes import router as discord_router
from .routers import ROUTERS
from .web import BASE_DIR, is_auth_exempt, mfa_setup_allowed, wants_html

_loop_tasks: list[asyncio.Task] = []


async def _startup() -> None:
    db.init()
    # Default each area's alert "Notify email" to its owner's address where unset.
    try:
        if db.backfill_alert_emails():
            for aid in db.all_area_ids():
                config.invalidate(aid)
    except Exception as exc:  # noqa: BLE001 - never let a migration block startup
        state.log_event("warn", f"alert-email backfill failed: {exc}")
    # One-shot: collapse journal trades that alpha.18–26 imported twice (report + fill pairs).
    try:
        if not db.meta_get("journal_dedupe_v1"):
            for aid in db.all_area_ids():
                n = db.dedupe_journal_trades(aid)
                if n:
                    with context.use_area(aid):
                        state.log_event("info", f"Journal: removed {n} duplicate trade(s) stored by earlier imports")
            db.meta_set("journal_dedupe_v1", "done")
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"journal dedupe failed: {exc}")
    # Encrypt secrets written by earlier versions (idempotent, one pass).
    try:
        try:
            push.available() and push.public_key()  # re-encrypt the VAPID key under the current crypto key
        except Exception:  # noqa: BLE001
            pass
        if db.encrypt_existing_settings():
            config.invalidate()
        if crypto.key_source() == "db":
            state.log_event("warn", "Secrets are encrypted with the auto-generated key stored in the "
                                    "database. Set NEXUSPRED_ENCRYPTION_KEY (or SESSION_SECRET) in the "
                                    "environment so the key lives outside the DB file.")
    except Exception as exc:  # noqa: BLE001 - never block startup on the migration
        state.log_event("warn", f"secret encryption pass failed: {exc}")
    # Durable signal/order history: prune, refill the live buffers, start the writer.
    try:
        db.prune_copy_events()
        pruned = history.prune()
        loaded = history.hydrate(db.all_area_ids())
        if loaded or pruned:
            state.log_event("info", f"History: {loaded} signal/order rows restored"
                                    + (f", {pruned} expired rows pruned" if pruned else ""))
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"history restore failed: {exc}")
    history.start()
    state.log_event("info", f"Bridge started (v{config.get_version()})")
    _loop_tasks[:] = [asyncio.create_task(health.health_loop(), name="health-loop"),
                      asyncio.create_task(health.discord_health_loop(), name="discord-health-loop"),
                      asyncio.create_task(_history_prune_loop(), name="history-prune-loop"),
                      asyncio.create_task(journal.scheduler_loop(), name="journal-import-loop"),
                      asyncio.create_task(pnl.pnl_loop(), name="pnl-loop"),
                      asyncio.create_task(copy.copy_loop(), name="copy-loop"),
                      asyncio.create_task(news.news_loop(), name="news-loop"),
                      asyncio.create_task(watchdog.heartbeat_loop(), name="heartbeat-loop")]
    health.start_discord_listeners()     # the health loop keeps them alive from here on


async def _history_prune_loop() -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await asyncio.to_thread(history.prune)
            await asyncio.to_thread(db.prune_copy_events)
        except Exception as exc:  # noqa: BLE001
            state.log_event("warn", f"history prune failed: {exc}")


async def _shutdown() -> None:
    """Stop the background loops and Discord listeners; close the HTTP pool."""
    for t in _loop_tasks:
        t.cancel()
    for t in _loop_tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    _loop_tasks.clear()
    await copy.stop_all()
    with contextlib.suppress(Exception):
        await asyncio.to_thread(drawdown.flush)   # batched drawdown state → settings
    await health.stop_discord_listeners()
    await http.aclose_all()
    await asyncio.to_thread(history.stop)  # drain queued history writes


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await _startup()
    # everything allocated at startup (modules, settings, caches) lives for the
    # process: frozen out of the collector's way, and gen-0 collections happen
    # every 50k allocations instead of 700 — the fan-out's p95 was the collector
    gc.collect()
    gc.freeze()
    gc.set_threshold(50_000, 50, 100)
    try:
        yield
    finally:
        await _shutdown()


class _Static(StaticFiles):
    """Static files whose scripts/styles always revalidate (ETag → 304), so a
    deploy never leaves a browser with a stale ES module next to a fresh one."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if path.endswith(".js"):
            # ES modules import their siblings with plain relative paths (no
            # version query), and a standalone iOS PWA will serve those from
            # cache without revalidating even under no-cache — leaving a device
            # on stale code after a deploy. no-store forces a fresh fetch.
            resp.headers["Cache-Control"] = "no-store"
        elif path.endswith(".css"):
            resp.headers["Cache-Control"] = "no-cache"  # already ?v= busted
        return resp


app = FastAPI(title="Fluxbridge", version=config.get_version(), lifespan=_lifespan)
app.mount("/static", _Static(directory=str(BASE_DIR / "static")), name="static")
for _router in ROUTERS:
    app.include_router(_router)
app.include_router(discord_router)  # Discord signal module (same server + auth)


class GateMiddleware:
    """The request gate as ONE pure-ASGI layer: CSP nonce → CSRF origin check →
    rate limits → login / two-factor gate with the tenant's area context →
    security headers on the answer. Starlette's ``BaseHTTPMiddleware`` (the
    previous two ``@app.middleware`` layers) runs the handler in its own task
    and re-streams every response body chunk through a queue — on the live SSE
    stream that made each broadcast eight times more expensive and cost a
    quarter of a millisecond on every request. Here the app runs in the
    caller's task, the response passes through untouched, only the headers of
    ``http.response.start`` are completed."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        path = scope.get("path", "")

        async def send_secured(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = security.security_header_list(request, nonce, list(message.get("headers") or []))
            await send(message)

        if not security.csrf_exempt(path) and security.cross_site(request):
            await JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)(scope, receive, send_secured)
            return
        limited = security._rate_limited(request)
        if limited is not None:
            await limited(scope, receive, send_secured)
            return
        if is_auth_exempt(path):
            await self.app(scope, receive, send_secured)
            return
        early = self._gate(request, path, scope)
        if early is not None:
            await early(scope, receive, send_secured)
            return
        tok = context.set_area(scope["state"]["area_id"])
        try:
            await self.app(scope, receive, send_secured)
        finally:
            context.reset_area(tok)

    @staticmethod
    def _gate(request: Request, path: str, scope: dict[str, Any]) -> Response | None:
        """Require a login session; record the user and their area on the request."""
        if db.user_count() == 0:  # first run: force admin setup
            if wants_html(request):
                return RedirectResponse("/setup", status_code=302)
            return JSONResponse({"detail": "Setup required"}, status_code=503)
        user = auth.current_user(request)
        if not user:
            if wants_html(request):
                return RedirectResponse("/login", status_code=302)
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        if user.get("totp_required") and not user.get("totp_enabled") and not mfa_setup_allowed(path):
            # new accounts enrol in two-factor authentication before anything else
            if wants_html(request):
                return RedirectResponse("/2fa/setup", status_code=302)
            return JSONResponse({"detail": "Two-factor setup required"}, status_code=403)
        area_id = db.user_primary_area(user["id"])
        if not area_id:
            return JSONResponse({"detail": "No workspace membership"}, status_code=403)
        scope["state"]["user"] = user
        scope["state"]["area_id"] = area_id
        return None


# Outermost first: body cap → gate (CSRF / rate limit / auth / headers) → the app.
app.add_middleware(GateMiddleware)
app.add_middleware(security.BodyLimitMiddleware)


@app.exception_handler(config.SettingsUnavailable)
async def _settings_unavailable(_request: Request, exc: config.SettingsUnavailable) -> JSONResponse:
    """A save that would have written defaults over unreadable settings was refused."""
    return JSONResponse({"detail": f"{exc} — check the database and try again"}, status_code=503)

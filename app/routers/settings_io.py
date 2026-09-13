"""Settings export / import: the workspace's configuration as one JSON file —
webhooks with their routing, symbol map, trading rules, alert preferences,
news-lock rules — without any secret (broker tokens, passwords, API keys, the
Discord user token, the webhook passphrase, the heartbeat ping URL). The webhook
tokens *do* travel (they are the URLs TradingView already points at) — the file
is a capability to fire signals and must be stored like one. Meant for
backups of the configuration and for moving a workspace to another bridge; the
database backup (Settings → Updates) is the full copy including secrets.

Marketplace publication/ACL state is installation-local: portable exports keep
listing metadata as a disabled draft but never carry live publication or numeric
user authorization ids into another Fluxbridge database.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import automations, config, context, db, marketplace, news, settings_schema, sizing, state, trade_window
from .core import validate_settings

router = APIRouter(prefix="/api/settings", tags=["settings-io"])

FORMAT = 1
MAX_WEBHOOKS = 200
EXPORT_KEYS = settings_schema.PORTABLE_KEYS
SECRET_KEYS = set(config.SECRET_FIELDS) | {"webhook_secret", "discord_user_token", "heartbeat_url"}
_PORTABLE_KEYS = tuple(k for k in EXPORT_KEYS if k in config.DEFAULT_SETTINGS and k not in SECRET_KEYS)


def _portable_sharing(raw: Any) -> dict[str, Any]:
    """A listing draft that cannot authorize/execute on another installation."""
    sh = dict(raw) if isinstance(raw, dict) else {}
    sh["enabled"] = False
    sh["allowed_user_ids"] = []
    sh["published_at"] = ""
    sh["paused"] = False
    return sh


def _portable_webhook(raw: Any) -> dict[str, Any]:
    wh = dict(raw) if isinstance(raw, dict) else {}
    if isinstance(wh.get("accounts"), list):
        wh["accounts"] = [dict(a) for a in wh["accounts"] if isinstance(a, dict)]
    if isinstance(wh.get("sharing"), dict):
        wh["sharing"] = _portable_sharing(wh["sharing"])
    return wh


def export_settings(area_id: int) -> dict[str, Any]:
    s = config.load_settings(area_id=area_id)
    out = {k: s[k] for k in _PORTABLE_KEYS if k in s}
    if isinstance(out.get("webhooks"), list):
        out["webhooks"] = [_portable_webhook(w) for w in out["webhooks"] if isinstance(w, dict)]
    return {"fluxbridge_settings": FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": config.get_version(), "settings": out}


def _import_webhook(w: dict[str, Any], current: dict[str, Any], area_id: int) -> dict[str, Any]:
    """A webhook from the file, normalised like the create/edit endpoints do.
    Routing survives only for logins that exist here (matched by login id);
    a token already used by another workspace is replaced. Marketplace state is
    always imported disabled with its installation-local user ACL cleared.
    """
    try:
        wh = config.new_webhook(name=str(w.get("name") or "Imported webhook")[:80],
                                strategy=str(w.get("strategy") or "simple"),
                                default_qty=w.get("default_qty", 1), tp_qty=w.get("tp_qty", 1))
        wid = str(w.get("id") or "")
        if wid.startswith("wh_") and 4 <= len(wid) <= 32 and wid[3:].isalnum():
            wh["id"] = wid
        token = str(w.get("token") or "")
        owner, _ = config.find_webhook(token) if token else (None, None)
        if 16 <= len(token) <= 64 and token.replace("-", "").replace("_", "").isalnum() and owner in (None, area_id):
            wh["token"] = token
        wh["enabled"] = bool(w.get("enabled", True))
        accounts = []
        for a in w.get("accounts") or []:
            if not isinstance(a, dict) or not a.get("spec"):
                continue
            idx = config.login_index(current, str(a.get("lid") or ""))
            if idx is None:
                continue
            sz = sizing.normalize(a)
            accounts.append({"token_idx": idx, "lid": str(a["lid"]), "spec": str(a["spec"])[:64],
                             "enabled": bool(a.get("enabled")), "qty_multiplier": sizing.effective_multiplier(sz),
                             "sizing": sz})
        wh["accounts"] = accounts
        if isinstance(w.get("sharing"), dict):
            wh["sharing"] = marketplace.normalize_sharing(_portable_sharing(w["sharing"]))
        if w.get("trade_window"):
            wh["trade_window"] = trade_window.normalize(w["trade_window"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook in file: {exc}") from exc
    return wh


async def _validate(doc: Any, area_id: int) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("fluxbridge_settings") != FORMAT or not isinstance(doc.get("settings"), dict):
        raise HTTPException(status_code=400, detail="Not a Fluxbridge settings export (format 1)")
    incoming = dict(doc["settings"])
    unknown = [k for k in incoming if k not in _PORTABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Export carries keys that cannot be imported: {', '.join(sorted(unknown)[:5])}")
    current = config.load_settings(area_id=area_id)
    if "webhooks" in incoming:
        whs = incoming["webhooks"]
        if not isinstance(whs, list) or len(whs) > MAX_WEBHOOKS or not all(isinstance(w, dict) for w in whs):
            raise HTTPException(status_code=400, detail="webhooks must be a list of webhook objects")
        seen: set[str] = set()
        out = []
        for w in whs:
            wh = _import_webhook(w, current, area_id)
            if wh["id"] in seen or wh["token"] in seen:
                wh["id"], wh["token"] = f"wh_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
            seen.update((wh["id"], wh["token"]))
            out.append(wh)
        incoming["webhooks"] = out
    if "news_lock" in incoming:
        try:
            incoming["news_lock"] = news.normalize(incoming["news_lock"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"news_lock: {exc}") from exc
    if "automations" in incoming:
        try:
            incoming["automations"] = automations.normalize_rules(incoming["automations"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"automations: {exc}") from exc
    if "symbol_map" in incoming:
        sm = incoming["symbol_map"]
        if not isinstance(sm, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in sm.items()):
            raise HTTPException(status_code=400, detail="symbol_map must map symbol names to contracts")
    try:
        incoming.update(settings_schema.coerce({k: v for k, v in incoming.items() if k not in ("webhooks", "news_lock", "automations")}, allow_protected=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await validate_settings(incoming)
    return incoming


@router.get("/export")
async def api_export(request: Request) -> JSONResponse:
    user = request.state.user
    doc = export_settings(context.get_area())
    db.log_action(user["id"], user["email"], "settings_export", user["email"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="fluxbridge-settings-{stamp}.json"'})


@router.post("/import")
async def api_import(request: Request) -> dict[str, Any]:
    user = request.state.user
    area = context.get_area()
    incoming = await _validate(await request.json(), area)

    def apply(s: dict[str, Any]) -> None:
        for k, v in incoming.items():
            s[k] = v
    config.update(apply, area_id=area)
    config.invalidate(area)
    db.log_action(user["id"], user["email"], "settings_import", user["email"], ", ".join(sorted(incoming)))
    state.log_event("info", f"Settings imported ({len(incoming)} key(s): {', '.join(sorted(incoming))})")
    return {"status": "ok", "keys": sorted(incoming), "webhooks": len(incoming.get("webhooks") or [])}

"""Telegram as an alert channel (alpha.99).

The admin sets a bot token under Settings → Platform. A user links their
chat from Settings → Alerts: the dashboard shows a one-time code, the user
sends ``/start <code>`` to the bot, the poll loop pairs the chat with the
workspace. From then on the workspace's alerts (gated by
``alert_telegram_enabled`` and ``alert_min_severity_telegram``) go to that
chat, and a critical alert's escalation can reach it too.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, Optional

from . import db, http

log = logging.getLogger("nexuspred.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
LINK_TTL_S = 15 * 60
POLL_TIMEOUT_S = 25
_offset = 0


def _token() -> str:
    from . import platform
    return platform.get_config()["telegram_bot_token"]


def configured() -> bool:
    return bool(_token())


# ------------------------------------------------------------------ linking
def link_code(area_id: int) -> str:
    """A one-time code the user sends to the bot as ``/start <code>``."""
    code = secrets.token_hex(4).upper()
    db.meta_set(f"tg_link:{code}", f"{area_id}:{int(time.time()) + LINK_TTL_S}")
    return code


def _consume_code(code: str) -> Optional[int]:
    raw = db.meta_get(f"tg_link:{code.upper()}")
    if not raw:
        return None
    db.meta_set(f"tg_link:{code.upper()}", "")
    try:
        area, exp = raw.split(":")
        if int(exp) < time.time():
            return None
        return int(area)
    except ValueError:
        return None


def chat_for(area_id: int) -> str:
    return db.meta_get(f"tg_chat:{area_id}") or ""


def link_chat(area_id: int, chat_id: str, name: str = "") -> None:
    db.meta_set(f"tg_chat:{area_id}", str(chat_id))
    db.meta_set(f"tg_chat_name:{area_id}", name[:80])


def unlink(area_id: int) -> None:
    db.meta_set(f"tg_chat:{area_id}", "")
    db.meta_set(f"tg_chat_name:{area_id}", "")


def status(area_id: int) -> dict[str, Any]:
    return {"configured": configured(), "linked": bool(chat_for(area_id)), "chat_name": db.meta_get(f"tg_chat_name:{area_id}") or "",
            "bot_name": __import__("app.platform", fromlist=["get_config"]).get_config()["telegram_bot_name"]}


# ------------------------------------------------------------------ sending
async def send(chat_id: str, text: str) -> None:
    token = _token()
    if not token or not chat_id:
        raise RuntimeError("Telegram is not configured")
    r = await http.client("outbound").post(API.format(token=token, method="sendMessage"),
                                            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown", "disable_web_page_preview": True}, timeout=15.0)
    if r.status_code >= 400:
        # Markdown that Telegram refuses: send it plain
        r = await http.client("outbound").post(API.format(token=token, method="sendMessage"),
                                                json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}, timeout=15.0)
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram answered {r.status_code}: {r.text[:200]}")


async def send_area(area_id: int, text: str) -> bool:
    chat = chat_for(area_id)
    if not chat:
        return False
    await send(chat, text)
    return True


# ------------------------------------------------------------------ inbound (linking)
async def handle_update(upd: dict[str, Any]) -> Optional[str]:
    """One update from getUpdates: ``/start CODE`` links the chat. Returns the reply text."""
    msg = upd.get("message") or upd.get("edited_message") or {}
    text = str(msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    area = _consume_code(code) if code else None
    if area is None:
        return "Send /start <code> with the code from Fluxbridge → Settings → Alerts → Telegram."
    who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name"), chat.get("username") and "@" + chat["username"]) if x)
    link_chat(area, chat_id, who or chat_id)
    return "Linked ✓ — this chat now receives your Fluxbridge alerts."


async def poll_once() -> int:
    global _offset
    token = _token()
    if not token:
        return 0
    r = await http.client("outbound").get(API.format(token=token, method="getUpdates"), params={"offset": _offset, "timeout": POLL_TIMEOUT_S}, timeout=POLL_TIMEOUT_S + 10)
    if r.status_code >= 400:
        raise RuntimeError(f"getUpdates {r.status_code}: {r.text[:200]}")
    updates = (r.json() or {}).get("result") or []
    for upd in updates:
        _offset = max(_offset, int(upd.get("update_id") or 0) + 1)
        try:
            reply = await handle_update(upd)
            chat_id = str(((upd.get("message") or {}).get("chat") or {}).get("id") or "")
            if reply and chat_id:
                await send(chat_id, reply)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram update failed: %s", exc)
    return len(updates)


async def poll_loop() -> None:
    while True:
        try:
            if configured():
                await poll_once()
            else:
                await asyncio.sleep(30.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram poll failed: %s", exc)
            await asyncio.sleep(15.0)


def reset() -> None:
    global _offset
    _offset = 0

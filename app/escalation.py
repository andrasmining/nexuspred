"""Escalation with acknowledgement for critical alerts (alpha.99).

A critical alert (risk guard, unprotected position, feed loss with flatten,
an order whose outcome is unknown) opens an *escalation*: the push goes out at
once with an acknowledge link; after :data:`STAGE_EMAIL_S` unacknowledged the
platform mailer sends it to the workspace owner; after :data:`STAGE_SMS_S`
Telegram and (with Twilio) SMS follow. One tap on the link — or the button in
the inbox — closes it. Only workspaces with ``alert_escalation`` on take part.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from . import auth, config, context, db, http, state

log = logging.getLogger("nexuspred.escalation")

STAGE_EMAIL_S = 120
STAGE_SMS_S = 300
LOOP_TICK_S = 15.0


def ack_token(esc_id: int) -> str:
    body = auth._b64e(json.dumps({"e": int(esc_id)}).encode())
    return f"{body}.{auth._sign(body)}"


def ack_url(esc_id: int) -> str:
    return f"{config.PUBLIC_URL or ''}/ack?t={ack_token(esc_id)}"


def parse_token(token: str) -> Optional[int]:
    import hmac
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, auth._sign(body)):
        return None
    try:
        return int(json.loads(auth._b64d(body))["e"])
    except (ValueError, KeyError, TypeError):
        return None


def open_escalation(area_id: int, kind: str, title: str, message: str, url: str) -> dict[str, Any]:
    esc = db.add_escalation(area_id, kind, title, message, url)
    state.log_event("warn", f"Escalation #{esc['id']} opened: {title} — acknowledge it in the inbox or via the link in the alert")
    return esc


def acknowledge(esc_id: int, by: str, area_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    if not db.ack_escalation(esc_id, by, area_id):
        return None
    esc = db.get_escalation(esc_id)
    if esc:
        with context.use_area(esc["area_id"]):
            state.log_event("info", f"Escalation #{esc_id} acknowledged by {by}")
    return esc


async def _sms(to: str, text: str) -> None:
    from . import platform
    cfg = platform.get_config()
    if not (cfg["twilio_sid"] and cfg["twilio_token"] and cfg["twilio_from"]):
        raise RuntimeError("SMS is not configured")
    r = await http.client("outbound").post(f"https://api.twilio.com/2010-04-01/Accounts/{cfg['twilio_sid']}/Messages.json",
                                            data={"From": cfg["twilio_from"], "To": to, "Body": text[:1500]},
                                            auth=(cfg["twilio_sid"], cfg["twilio_token"]), timeout=20.0)
    if r.status_code >= 400:
        raise RuntimeError(f"Twilio answered {r.status_code}: {r.text[:200]}")


async def _step(esc: dict[str, Any], now: float) -> None:
    """Advance one open escalation by the time that passed."""
    from . import alerts, mailer, telegram
    created = datetime.fromisoformat(esc["created_at"]).timestamp()
    age = now - created
    area = esc["area_id"]
    link = ack_url(esc["id"])
    with context.use_area(area):
        s = config.load_settings()
        owner = db.area_owner(area)
        if esc["stage"] < 1 and age >= STAGE_EMAIL_S:
            db.step_escalation(esc["id"], 1)
            text = f"{esc['message']}\n\nStill unacknowledged after {int(age // 60)} min. Acknowledge: {link}"
            if owner and mailer.can_send(area):
                u = db.get_user(owner)
                if u:
                    mailer.send_template(u["email"], "notice", {"title": f"⚠ {esc['title']}", "message": text, "button": "Acknowledge", "url": link}, area_id=area)
            await alerts._send_email(f"Fluxbridge: {esc['title']} (unacknowledged)", text)
            state.log_event("warn", f"Escalation #{esc['id']} → e-mail (no acknowledgement after {STAGE_EMAIL_S} s)")
        elif esc["stage"] < 2 and age >= STAGE_SMS_S:
            db.step_escalation(esc["id"], 2)
            text = f"🚨 {esc['title']}\n{esc['message']}\nUnacknowledged for {int(age // 60)} min — acknowledge: {link}"
            sent = []
            try:
                if await telegram.send_area(area, text):
                    sent.append("telegram")
            except Exception as exc:  # noqa: BLE001
                log.warning("escalation telegram failed: %s", exc)
            phone = str(s.get("alert_sms_to") or "")
            if phone:
                try:
                    await _sms(phone, text)
                    sent.append("sms")
                except Exception as exc:  # noqa: BLE001
                    log.warning("escalation sms failed: %s", exc)
            state.log_event("warn", f"Escalation #{esc['id']} → {', '.join(sent) or 'no further channel configured'} (no acknowledgement after {STAGE_SMS_S} s)")


async def tick(now: Optional[float] = None) -> int:
    now = now or time.time()
    n = 0
    for esc in db.open_escalations():
        try:
            await _step(esc, now)
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("escalation #%s step failed: %s", esc["id"], exc)
    return n


async def loop() -> None:
    while True:
        await asyncio.sleep(LOOP_TICK_S)
        try:
            await tick()
        except Exception as exc:  # noqa: BLE001
            log.warning("escalation loop failed: %s", exc)


def summary(area_id: int) -> dict[str, Any]:
    return {"open": db.open_escalations(area_id), "recent": db.list_escalations(area_id, 20)}


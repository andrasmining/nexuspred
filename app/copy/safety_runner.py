"""Safety extensions for copy-trading execution and follower flattening.

The main GroupRunner remains the high-churn feed/mirror implementation. This
subclass adds the financial-safety boundaries that must be checked at mutation
time: marketplace authorization and broker-confirmed feed-loss flattening.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .. import config, context, db, marketplace, news
from ..tradovate import TradovateError
from .group_runner import (
    FEED_STALE_S,
    POLL_ERROR_SLEEP_S,
    GroupRunner as _BaseGroupRunner,
)
from .groups import load_groups, target_qty

FLATTEN_VERIFY_DELAY_S = 0.25
FLATTEN_VERIFY_READS = 3


class GroupRunner(_BaseGroupRunner):
    """GroupRunner with execution-time ACL and broker-truth flatten semantics."""

    def __init__(self, area_id: int, group: dict[str, Any]) -> None:
        super().__init__(area_id, group)
        self.flatten_unresolved: list[str] = []

    def _external_authorized(self, follower: dict[str, Any]) -> bool:
        """Current publisher/subscriber authorization for one external follower."""
        if not follower.get("external"):
            return True
        try:
            farea = self._area_of(follower)
            sub_id = int(follower.get("sub_id") or 0)
            sub = db.get_subscription(sub_id, area_id=farea) if sub_id else None
            current = next((g for g in load_groups(self.area_id) if g.get("id") == self.id), None)
            if not sub or not current or not current.get("enabled"):
                return False
            if not sub.get("enabled") or str(sub.get("status") or "active") != "active":
                return False
            if int(sub.get("publisher_area_id") or 0) != self.area_id:
                return False
            if str(sub.get("webhook_id") or "") != f"copy:{self.id}":
                return False
            sh = marketplace.sharing_of(current)
            if not sh.get("enabled") or sh.get("paused"):
                return False
            return marketplace.subscription_allowed(current, sub)
        except Exception:  # noqa: BLE001 - authorization uncertainty fails closed
            return False

    async def _mirror_follower(self, f: dict[str, Any], cid: int, name: str, net: int, unit: int,
                               copy_adds: bool, reason: str, t0: Optional[float]) -> None:
        """Mirror one follower with authorization checked inside its execution lock."""
        spec = f["spec"]
        lock = self.locks.setdefault(spec, asyncio.Lock())
        async with lock:
            # This check is intentionally immediately before the order path. A
            # selected-user ACL revocation must not wait for sync_area's next
            # five-second refresh before it stops new financial actions.
            if not self._external_authorized(f):
                self._record("skipped", follower=spec, symbol=name,
                             detail=f"{reason}: marketplace authorization no longer valid")
                return

            target = target_qty(f, net, unit, copy_adds=copy_adds)
            ex = self._executor(f)
            if ex is None:
                self.follower_err[spec] = "login disabled or account gone"
                self._record("reject", follower=spec, symbol=name, detail="login disabled or account gone")
                return
            farea = self._area_of(f)
            if not config.setting("trading_enabled", area_id=farea):
                self._record("skipped", follower=spec, symbol=name,
                             detail=f"{reason}: trading switch is off" + (" in the follower's workspace" if farea != self.area_id else ""))
                return
            if reason == "drift" and self.leader_net.get(cid, 0) != net:
                return
            if news.flattened_lock(farea):
                self._record("skipped", follower=spec, symbol=name,
                             detail=f"{reason}: news lock (flatten) active in the follower's workspace")
                return
            if self.orders.touched_recently(spec, cid):
                await self.orders.cancel_all(reason="leader position changed", spec=spec, cid=cid)
                actual = await self._broker_net(ex, cid)
                if actual is not None:
                    self.follower_pos[(spec, cid)] = actual
            have = self.follower_pos.get((spec, cid), 0)
            delta = target - have
            if delta == 0:
                return
            with context.use_area(farea):
                try:
                    res = await ex.place_order(symbol=name, action="Buy" if delta > 0 else "Sell",
                                               qty=abs(delta), order_type="Market")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    err = f"{exc}" if isinstance(exc, TradovateError) else f"{type(exc).__name__}: {exc}"
                    self.follower_err[spec] = err[:200]
                    self.follower_err_at[spec] = time.monotonic()
                    self._record("reject", follower=spec, symbol=name, detail=f"{reason}: {err}")
                    self._alert(f"Copy reject: {spec}", f"{name} {reason}: {err}", area_id=farea)
                    return
            if not isinstance(res, dict) or res.get("status") != "submitted":
                raw = res.get("raw") if isinstance(res, dict) else None
                err = str((raw or {}).get("errorText") or (res.get("status") if isinstance(res, dict) else res))
                self.follower_err[spec] = err[:200]
                self.follower_err_at[spec] = time.monotonic()
                self._record("reject", follower=spec, symbol=name, detail=f"{reason}: {err}")
                self._alert(f"Copy reject: {spec}", f"{name} {reason}: {err}", area_id=farea)
                return
            self.follower_pos[(spec, cid)] = target
            self.last_order_at[spec] = time.monotonic()
            self.follower_err.pop(spec, None)
            self.follower_err_at.pop(spec, None)
            latency = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            self.last_latency_ms = latency if latency is not None else self.last_latency_ms
            self._record("mirror", follower=spec, symbol=name, latency_ms=latency,
                         detail=f"{reason} → {'Buy' if delta > 0 else 'Sell'} {abs(delta)} (now {target:+d})")

    async def _confirm_flat(self, ex: Any, cid: int) -> Optional[int]:
        """Read broker positions until flat or the short settle window expires."""
        last: Optional[int] = None
        for attempt in range(FLATTEN_VERIFY_READS):
            last = await self._broker_net(ex, cid)
            if last == 0:
                return 0
            if attempt + 1 < FLATTEN_VERIFY_READS:
                await asyncio.sleep(FLATTEN_VERIFY_DELAY_S)
        return last

    async def watchdog(self) -> None:
        """Pause on feed loss and distinguish confirmed-flat from unresolved."""
        if self.paused or not self.tasks:
            return
        limit = float(self.group.get("feed_loss_flatten_s") or 30)
        if time.monotonic() < self.throttled_until:
            return
        lost_for = (time.monotonic() - self.last_frame) if self.last_frame else 0.0
        stale = max(FEED_STALE_S, 3 * POLL_ERROR_SLEEP_S, self.poll_interval + FEED_STALE_S)
        if self.feed_ok and lost_for > stale:
            self.error = self.error or "poll stalled"
            self._mark_feed(False)
        if not self.feed_ok and self.last_frame and lost_for > limit:
            if str(self.group.get("on_feed_loss") or "flatten") == "pause":
                self.paused = True
                self.pause_reason = (
                    f"feed lost for {int(lost_for)} s — mirroring paused "
                    "(followers keep their positions); resume when the leader feed is back"
                )
            else:
                sent = await self.flatten_followers(reason=f"feed lost for {int(lost_for)} s")
                self.paused = True
                if self.flatten_unresolved:
                    detail = "; ".join(self.flatten_unresolved[:4])
                    more = f" (+{len(self.flatten_unresolved) - 4} more)" if len(self.flatten_unresolved) > 4 else ""
                    self.pause_reason = (
                        f"feed lost for {int(lost_for)} s — flatten incomplete after "
                        f"{sent} submitted order(s): {detail}{more}; mirroring paused — "
                        "check the follower accounts before resuming"
                    )
                else:
                    self.pause_reason = (
                        f"feed lost for {int(lost_for)} s — followers confirmed flat "
                        f"({sent} order(s)); resume when the leader feed is back"
                    )
            self._record("paused", detail=self.pause_reason)
            self._alert(f"Copy group paused: {self.group['name']}", self.pause_reason, email=True)

    async def flatten_followers(self, *, reason: str) -> int:
        """Close mirrored exposure at most once and baseline only confirmed-flat.

        Local copy state can choose the direction/quantity when broker reads are
        unavailable, but it can never prove that an account is flat. Unknown
        outcomes therefore remain unresolved and are not baselined.
        """
        sent = 0
        self.flatten_unresolved = []
        await self.orders.cancel_all(reason=f"flatten: {reason}")

        contracts = {cid for cid, _net in self.leader_net.items() if cid not in self.baseline}
        contracts |= {k[1] for k in self.orders.touched if k[1]}
        contracts |= {int(t.get("contract_id") or 0) for t in self.orders.twins.values() if t.get("contract_id")}
        contract_ok = {cid: True for cid in contracts}

        for (spec, _leader_id), twin in list(self.orders.twins.items()):
            cid = int(twin.get("contract_id") or 0)
            if cid in contract_ok:
                contract_ok[cid] = False
                self.flatten_unresolved.append(
                    f"{spec} {self.contract_names.get(cid, str(cid))}: working copied order remains"
                )

        for follower in self.followers:
            if not follower.get("enabled", True):
                continue
            spec = str(follower["spec"])
            ex = self._executor(follower)
            if ex is None:
                for cid in contracts:
                    contract_ok[cid] = False
                    self.flatten_unresolved.append(
                        f"{spec} {self.contract_names.get(cid, str(cid))}: login/account unavailable"
                    )
                continue

            for cid in contracts:
                key = (spec, cid)
                name = self.contract_names.get(cid, str(cid))
                lock = self.locks.setdefault(spec, asyncio.Lock())
                async with lock:
                    actual = await self._broker_net(ex, cid)
                    if actual is None:
                        have = int(self.follower_pos.get(key, 0) or 0)
                        if not have:
                            contract_ok[cid] = False
                            self.flatten_unresolved.append(f"{spec} {name}: broker position unreadable")
                            continue
                    else:
                        have = int(actual)

                    if not have:
                        self.follower_pos[key] = 0
                        continue

                    with context.use_area(self._area_of(follower)):
                        try:
                            result = await ex.place_order(
                                symbol=name,
                                action="Sell" if have > 0 else "Buy",
                                qty=abs(have),
                                order_type="Market",
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - never replay a maybe-live close
                            contract_ok[cid] = False
                            self.flatten_unresolved.append(f"{spec} {name}: flatten unresolved ({exc})")
                            self._record("reject", follower=spec, symbol=name,
                                         detail=f"flatten ({reason}): {exc}")
                            continue

                    if not isinstance(result, dict) or result.get("status") != "submitted":
                        contract_ok[cid] = False
                        self.flatten_unresolved.append(f"{spec} {name}: flatten not accepted ({result})")
                        self._record("reject", follower=spec, symbol=name,
                                     detail=f"flatten ({reason}): not accepted ({result})")
                        continue

                    sent += 1
                    self.last_order_at[spec] = time.monotonic()
                    confirmed = await self._confirm_flat(ex, cid)
                    if confirmed == 0:
                        self.follower_pos[key] = 0
                        self.follower_err.pop(spec, None)
                        self.follower_err_at.pop(spec, None)
                        self._record("flatten", follower=spec, symbol=name,
                                     detail=f"{reason}: broker confirmed flat from {have:+d}")
                    else:
                        contract_ok[cid] = False
                        if confirmed is None:
                            detail = "broker position unreadable after flatten"
                        else:
                            self.follower_pos[key] = int(confirmed)
                            detail = f"broker still shows {int(confirmed):+d} after flatten"
                        self.flatten_unresolved.append(f"{spec} {name}: {detail}")
                        self.follower_err[spec] = detail[:200]
                        self.follower_err_at[spec] = time.monotonic()
                        self._record("reject", follower=spec, symbol=name,
                                     detail=f"flatten ({reason}): {detail}")

        for cid in contracts:
            if self.leader_net.get(cid) and contract_ok.get(cid, False):
                self.baseline.add(cid)
                self._persist(cid)

        self.flatten_unresolved = list(dict.fromkeys(self.flatten_unresolved))
        return sent

    async def sync_now(self) -> int:
        self.flatten_unresolved = []
        return await super().sync_now()

    def status(self) -> dict[str, Any]:
        return {**super().status(), "flatten_unresolved": list(self.flatten_unresolved)}

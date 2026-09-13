"""``simple`` strategy: one Market (or Limit) order per account, no TP/SL."""
from __future__ import annotations

import asyncio

from .. import config
from .common import _collect_entries, _entry_result, _price, _signal_qty, _trade_key, _track_entry
from ..sizing import account_qty


async def handle_entry(payload, action, root, target, executors, active_map, tag, webhook, *, settings=None):
    """One Market (or Limit, if 'entry'/'price' given) order per account, sized by
    the payload's qty (or the webhook default), no TP/SL."""
    s = settings if settings is not None else config.load_settings()
    base_qty = _signal_qty(payload.get("qty", payload.get("contracts")), webhook.get("default_qty", 1), strict=True)

    entry_side = "Buy" if action == "buy" else "Sell"
    order_type = s.get("entry_order_type", "Market")
    price = payload.get("entry", payload.get("price"))
    if price is not None:
        price = _price(price, "entry")

    async def place_for(ex):
        contract = await ex.resolve_contract(target)
        qty = account_qty(ex, base_qty)
        order = await ex.place_order(
            symbol=contract, action=entry_side, qty=qty,
            order_type=order_type, price=price,
        )
        info = {
            "name": ex.name, "contract": contract, "qty": qty, "entry_qty": qty,
            "sl_order_id": None, "tp_order_ids": [],
        }
        return ex.name, info, [order], contract

    results = await asyncio.gather(*(place_for(ex) for ex in executors), return_exceptions=True)

    acct_state, orders, summary, contract = _collect_entries(executors, results, tag=tag, label="Entry", fallback_contract=target, qty_key="qty")

    _track_entry(active_map, _trade_key(webhook["id"], root), {"side": action, "qty": base_qty}, acct_state,
                 tag=tag, webhook=webhook, root=root, action=action, contract=contract, settings=s)
    # a failed account is isolated: the entry stays "ok" for the others; the names travel in ``failed``
    return _entry_result({"status": "ok", "action": action, "contract": contract, "accounts": summary, "orders": orders, "simulated": tag != ""},
                         executors, results, acct_state, tag=tag,
                         line=f"{tag}[{webhook.get('name', '?')}] {action.upper()} {contract} on {len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}")

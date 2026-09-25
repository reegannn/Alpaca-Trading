"""Stop protection for fractional order mode (`trader.py protect`).

Fractional orders must be DAY orders, so a protective stop cannot be GTC:
it is re-placed every session. For every open long position with an
open_trades.json record, ``protect`` ensures there is exactly one active sell
stop for the full position qty at the recorded stop price:

* none            -> create one
* wrong qty/price -> cancel, confirm the cancel, then create (never two at once)
* duplicates      -> keep one correct stop, cancel the rest
* correct         -> no-op

Positions without a record, without a recorded stop, with another sell order
already open (e.g. a pending market close), with an open BUY order (a sell
stop could be rejected as a potential wash trade; normally impossible because
`enter` never leaves a buy working), or already trading at/below the stop are
flagged and left untouched.

``plan_protection`` is pure; ``execute_protection`` performs the plan against
a client and is written so a crash or error can never leave two open stops.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .orders import STOP_PREFIX, client_order_id, stop_order
from .sizing import floor_qty, round_to_tick

STOP_TYPES = {"stop"}
CANCEL_WAIT_SECONDS = 30.0
CANCEL_POLL_SECONDS = 1.5


@dataclass
class ProtectAction:
    symbol: str
    action: str                          # ok | create | replace | dedupe | flag
    reason: str
    record_id: str | None = None         # open_trades.json key (entry client_order_id)
    qty: str | None = None               # full position qty as a decimal string
    stop_price: float | None = None
    keep_order_id: str | None = None
    cancel_order_ids: list[str] = field(default_factory=list)
    flag: str | None = None              # untracked | no_stop_recorded | open_buy | pending_sell | breached | not_long

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def qty_equal(a: Any, b: Any) -> bool:
    """Equal at Alpaca's 9-decimal precision (False if either is not a number)."""
    try:
        return floor_qty(a) == floor_qty(b)
    except (InvalidOperation, ValueError, TypeError):
        return False


def price_equal(a: Any, b: Any) -> bool:
    da, db = _dec(a), _dec(b)
    if da is None or db is None:
        return False
    return _dec(round_to_tick(float(da))) == _dec(round_to_tick(float(db)))


def latest_record(open_trades: dict[str, dict[str, Any]], symbol: str) -> tuple[str, dict[str, Any]] | None:
    """The most recently submitted open_trades record for ``symbol``."""
    recs = [(cid, r) for cid, r in open_trades.items() if str(r.get("symbol", "")).upper() == symbol]
    if not recs:
        return None
    return max(recs, key=lambda cr: str(cr[1].get("submitted_at") or ""))


def plan_protection(positions: list[dict[str, Any]], open_orders: list[dict[str, Any]],
                    open_trades: dict[str, dict[str, Any]]) -> list[ProtectAction]:
    plan: list[ProtectAction] = []
    for p in positions:
        sym = str(p.get("symbol", "")).upper()
        qty = str(p.get("qty", "0"))
        qd = _dec(qty)
        if p.get("side", "long") != "long" or qd is None or qd <= 0:
            plan.append(ProtectAction(sym, "flag", "not a long position", flag="not_long"))
            continue
        if any(str(o.get("symbol", "")).upper() == sym and o.get("side") == "buy" for o in open_orders):
            plan.append(ProtectAction(sym, "flag", "skipped: open buy order", qty=qty, flag="open_buy"))
            continue
        found = latest_record(open_trades, sym)
        if found is None:
            plan.append(ProtectAction(sym, "flag", "no open_trades.json record; not touched",
                                      qty=qty, flag="untracked"))
            continue
        cid, rec = found
        stop_raw = rec.get("stop")
        if isinstance(stop_raw, bool) or not isinstance(stop_raw, (int, float)) or stop_raw <= 0:
            plan.append(ProtectAction(sym, "flag", "record has no valid stop price; not touched",
                                      record_id=cid, qty=qty, flag="no_stop_recorded"))
            continue
        stop = round_to_tick(float(stop_raw))

        sells = [o for o in open_orders
                 if str(o.get("symbol", "")).upper() == sym and o.get("side") == "sell"]
        stops = [o for o in sells if o.get("type") in STOP_TYPES]
        others = [o for o in sells if o.get("type") not in STOP_TYPES]
        if others:
            plan.append(ProtectAction(
                sym, "flag", "another sell order is open (e.g. a pending close); not placing a stop",
                record_id=cid, qty=qty, stop_price=stop, flag="pending_sell"))
            continue

        good = [o for o in stops if qty_equal(o.get("qty"), qty) and price_equal(o.get("stop_price"), stop)]
        if good:
            keep = good[0]
            extras = [str(o["id"]) for o in stops if o is not keep]
            plan.append(ProtectAction(
                sym, "dedupe" if extras else "ok",
                "duplicate stops found; keeping one" if extras else "correct stop already active",
                record_id=cid, qty=qty, stop_price=stop, keep_order_id=str(keep["id"]),
                cancel_order_ids=extras))
            continue

        current = _dec(p.get("current_price"))
        if current is not None and current <= _dec(stop):
            plan.append(ProtectAction(
                sym, "flag", f"price {current} is at/below the stop {stop}; close the position",
                record_id=cid, qty=qty, stop_price=stop, flag="breached"))
            continue

        if stops:
            plan.append(ProtectAction(
                sym, "replace", "existing stop has the wrong qty or price",
                record_id=cid, qty=qty, stop_price=stop,
                cancel_order_ids=[str(o["id"]) for o in stops]))
        else:
            plan.append(ProtectAction(sym, "create", "no active stop", record_id=cid,
                                      qty=qty, stop_price=stop))
    return plan


def open_sells(client: Any, sym: str) -> list[dict[str, Any]]:
    return [o for o in client.list_orders(status="open", nested=False, symbols=[sym])
            if str(o.get("symbol", "")).upper() == sym and o.get("side") == "sell"]


def remember_stop(rec: dict[str, Any], order_id: str) -> None:
    rec["stop_order_id"] = order_id
    ids = list(rec.get("stop_order_ids") or [])
    if order_id not in ids:
        ids.append(order_id)
    rec["stop_order_ids"] = ids


def execute_protection(client: Any, plan: list[ProtectAction],
                       open_trades: dict[str, dict[str, Any]], today: date,
                       sleep: Callable[[float], None] = time.sleep,
                       monotonic: Callable[[], float] = time.monotonic,
                       wait_seconds: float = CANCEL_WAIT_SECONDS) -> dict[str, Any]:
    """Carry out ``plan``. Mutates ``open_trades`` (stop order ids). Never retries a submit."""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for a in plan:
        res = a.to_dict()
        rec = open_trades.get(a.record_id) if a.record_id else None

        if a.action == "flag":
            results.append(res)
            continue

        # Cancel duplicates / wrong stops first and confirm they are gone.
        if a.cancel_order_ids:
            cancel_errors = []
            for oid in a.cancel_order_ids:
                try:
                    client.cancel_order(oid)
                except Exception as exc:  # noqa: BLE001 - verified by the poll below
                    cancel_errors.append(f"{oid}: {exc}")
            deadline = monotonic() + wait_seconds
            still = [o for o in open_sells(client, a.symbol) if str(o.get("id")) in a.cancel_order_ids]
            while still and monotonic() < deadline:
                sleep(CANCEL_POLL_SECONDS)
                still = [o for o in open_sells(client, a.symbol) if str(o.get("id")) in a.cancel_order_ids]
            if still:
                res["error"] = (f"could not confirm cancellation of {[o.get('id') for o in still]}; "
                                f"no new stop placed (avoids a duplicate)")
                if cancel_errors:
                    res["cancel_errors"] = cancel_errors
                errors.append(res)
                results.append(res)
                continue
            res["cancelled"] = list(a.cancel_order_ids)

        if a.action in ("ok", "dedupe"):
            if rec is not None and a.keep_order_id:
                remember_stop(rec, a.keep_order_id)
            results.append(res)
            continue

        # create / replace: final guard against duplicates, then submit exactly once.
        open_now = open_sells(client, a.symbol)
        if open_now:
            res["error"] = (f"sell order(s) {[o.get('id') for o in open_now]} still open for "
                            f"{a.symbol}; not placing another stop")
            errors.append(res)
            results.append(res)
            continue
        assert a.qty is not None and a.stop_price is not None
        order = stop_order(a.symbol, a.qty, a.stop_price, client_order_id(STOP_PREFIX, a.symbol, today))
        try:
            submitted = client.submit_order(order)
        except Exception as exc:  # noqa: BLE001 - never retried
            res["error"] = f"stop submission failed ({exc}); not retried"
            if rec is not None:
                rec["stop_order_id"] = None
            errors.append(res)
            results.append(res)
            continue
        oid = str((submitted or {}).get("id"))
        res["submitted_order_id"] = oid
        res["order"] = order
        if rec is not None:
            remember_stop(rec, oid)
        results.append(res)

    return {"results": results, "errors": errors}


# Flags that leave a position without a working stop.
UNPROTECTED_FLAGS = {"untracked", "no_stop_recorded", "open_buy", "breached", "not_long"}


def unprotected_summary(results: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str | None]:
    """Positions left without a working stop (errors, unprotected flags, breaches),
    and the Slack warning line for them, e.g. "⚠️ UNPROTECTED: XYZ (breached)"."""
    items: list[dict[str, str]] = []
    for r in results:
        if r.get("error"):
            items.append({"symbol": r["symbol"], "reason": f"error: {r['error']}"})
        elif r.get("action") == "flag" and r.get("flag") in UNPROTECTED_FLAGS:
            reason = r["reason"] if r["flag"] == "open_buy" else str(r["flag"])
            items.append({"symbol": r["symbol"], "reason": reason})
    if not items:
        return [], None
    text = ", ".join(f"{i['symbol']} ({i['reason'][:80]})" for i in items)
    return items, f"⚠️ UNPROTECTED: {text}"

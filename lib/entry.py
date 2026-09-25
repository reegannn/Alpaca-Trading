"""Fill-or-cancel fractional entries (`trader.py enter` in fractional mode).

A fractional entry is never left working after the run that placed it:

1. submit the DAY limit buy (exactly once, never retried; done by the caller,
   which saves the open_trades.json record before calling ``follow_fractional_entry``
   so a crash mid-poll still leaves a record);
2. poll it every ``poll_seconds`` for up to ``timeout_seconds``;
3. if it is not completely filled, cancel the remainder and confirm the cancel;
4. if anything filled, place the protective DAY sell stop for the filled qty.
   If that fails, check that no stop is open (so a retry can never duplicate)
   and retry once. If it fails again, close the filled qty at once with a
   market sell (``close_reason = risk``) and report the error.

So a successful ``enter`` always leaves the position protected (or, if
protection was impossible, already being closed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from .orders import EXIT_PREFIX, STOP_PREFIX, client_order_id, market_sell_order, stop_order
from .protect import open_sells, price_equal, qty_equal
from .sizing import floor_qty, format_qty

FINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}
CANCEL_CONFIRM_SECONDS = 30.0
STOP_ATTEMPTS = 2          # first try + one retry


@dataclass
class EntryOutcome:
    status: str = "pending"              # filled | partial | cancelled | closed_unprotected | unprotected
    entry_order_id: str | None = None
    order_status: str | None = None
    filled_qty: str = "0"
    filled_avg_price: float | None = None
    filled_at: str | None = None
    cancel_confirmed: bool | None = None
    stop_order_id: str | None = None
    stop_attempts: list[dict[str, Any]] = field(default_factory=list)
    close_order_id: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def protected(self) -> bool:
        return self.stop_order_id is not None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["protected"] = self.protected
        return d


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def follow_fractional_entry(client: Any, submitted: dict[str, Any], symbol: str, stop_price: float,
                            today: date, timeout_seconds: float, poll_seconds: float,
                            sleep: Callable[[float], None] = time.sleep,
                            monotonic: Callable[[], float] = time.monotonic,
                            cancel_confirm_seconds: float = CANCEL_CONFIRM_SECONDS) -> EntryOutcome:
    """Steps 2-4 above for an entry order already submitted (``submitted`` is Alpaca's
    response). Errors are captured in the outcome, never raised."""
    sym = symbol.upper()
    out = EntryOutcome()
    oid = str(submitted.get("id"))
    out.entry_order_id = oid
    current = submitted

    # 2. Poll for a complete fill.
    deadline = monotonic() + timeout_seconds
    while current.get("status") != "filled" and monotonic() < deadline:
        sleep(poll_seconds)
        current = client.get_order(oid, nested=False) or current

    # 3. Cancel any remainder and confirm.
    if current.get("status") != "filled":
        if current.get("status") not in FINAL_STATUSES:
            try:
                client.cancel_order(oid)
            except Exception as exc:  # noqa: BLE001 - a fill may have raced the cancel; checked below
                out.errors.append(f"cancel of entry {oid} failed: {exc}")
        confirm_deadline = monotonic() + cancel_confirm_seconds
        current = client.get_order(oid, nested=False) or current
        while current.get("status") not in FINAL_STATUSES and monotonic() < confirm_deadline:
            sleep(poll_seconds)
            current = client.get_order(oid, nested=False) or current
        out.cancel_confirmed = current.get("status") in FINAL_STATUSES
        if not out.cancel_confirmed:
            out.errors.append(f"cancellation of entry {oid} not confirmed (status {current.get('status')})")

    out.order_status = current.get("status")
    filled = floor_qty(current.get("filled_qty") or "0")
    out.filled_qty = format_qty(filled)
    out.filled_avg_price = _f(current.get("filled_avg_price"))
    out.filled_at = current.get("filled_at")
    if filled <= Decimal(0):
        out.status = "cancelled"          # nothing filled; no position, no stop
        return out
    full = current.get("status") == "filled"

    # 4. Protect the filled qty: one try + one retry, never two stops.
    for attempt in range(1, STOP_ATTEMPTS + 1):
        if attempt > 1:
            existing = [o for o in open_sells(client, sym)
                        if o.get("type") == "stop" and qty_equal(o.get("qty"), filled)
                        and price_equal(o.get("stop_price"), stop_price)]
            if existing:          # the first attempt did reach Alpaca after all
                out.stop_order_id = str(existing[0].get("id"))
                out.stop_attempts.append({"attempt": attempt, "adopted": out.stop_order_id})
                break
        try:
            placed = client.submit_order(stop_order(sym, filled, stop_price,
                                                    client_order_id(STOP_PREFIX, sym, today)))
            out.stop_order_id = str(placed.get("id"))
            out.stop_attempts.append({"attempt": attempt, "order_id": out.stop_order_id})
            break
        except Exception as exc:  # noqa: BLE001
            out.stop_attempts.append({"attempt": attempt, "error": str(exc)})

    if out.protected:
        out.status = "filled" if full else "partial"
        return out

    # Stop could not be placed twice: exit the filled qty immediately.
    out.errors.append("protective stop could not be placed after a retry; closing the position")
    try:
        closed = client.submit_order(market_sell_order(sym, filled, client_order_id(EXIT_PREFIX, sym, today)))
        out.close_order_id = str(closed.get("id"))
        out.status = "closed_unprotected"
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"UNPROTECTED: closing {sym} also failed ({exc})")
        out.status = "unprotected"
    return out

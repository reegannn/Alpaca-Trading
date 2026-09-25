"""Exit detection from FILL activities (used by `reconcile`).

In fractional mode there are no bracket legs: a position is exited by one of
the daily-replaced protective stop orders, or by a market sell placed by
`close` (target / time_stop / earnings_exit / thesis_broken / manual / risk /
stop). Exits are identified from sell FILL activities for the symbol after the
entry fill, and classified by the order that produced them:

* order id in the record's ``stop_order_ids`` (every stop placed over the
  position's life), or an order whose type is a stop -> ``stop``
* the record's ``close_order_id`` -> the recorded ``close_reason``
* anything else -> the recorded ``close_reason`` or ``unknown``

When fills come from more than one kind of order, the reason covering the
largest quantity wins (ties go to ``stop``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .market_calendar import parse_ts

STOP_ORDER_TYPES = {"stop", "stop_limit", "trailing_stop"}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def exit_fills(fills: list[dict[str, Any]], symbol: str, since: datetime,
               qty: float) -> list[dict[str, Any]]:
    """Sell fills for ``symbol`` at/after ``since``, oldest first, covering up to ``qty``.

    Returns [] unless the fills cover the full ``qty`` (position fully exited).
    Each returned fill carries ``used_qty`` (the part counted toward this exit).
    """
    sym = symbol.upper()
    rows = []
    for f in fills:
        if str(f.get("symbol", "")).upper() != sym or f.get("side") != "sell":
            continue
        ts = parse_ts(f.get("transaction_time"))
        if ts is None or ts < since:
            continue
        rows.append((ts, f))
    rows.sort(key=lambda r: r[0])
    used: list[dict[str, Any]] = []
    got = 0.0
    for _, f in rows:
        take = min(_f(f.get("qty")), qty - got)
        if take <= 1e-12:
            break
        used.append({**f, "used_qty": take})
        got += take
        if got >= qty - 1e-9:
            break
    return used if got >= qty - 1e-9 and got > 0 else []


def vwap(used: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    """(average exit price, last fill time) of the fills returned by ``exit_fills``."""
    q = sum(f["used_qty"] for f in used)
    if q <= 0:
        return None, None
    price = sum(f["used_qty"] * _f(f.get("price")) for f in used) / q
    return price, used[-1].get("transaction_time")


def classify_exit(used: list[dict[str, Any]], record: dict[str, Any],
                  order_types: dict[str, str] | None = None) -> str:
    """Exit reason for a fractional-mode position (see module docstring)."""
    order_types = order_types or {}
    stop_ids = {str(i) for i in (record.get("stop_order_ids") or []) if i}
    if record.get("stop_order_id"):
        stop_ids.add(str(record["stop_order_id"]))
    close_id = str(record.get("close_order_id") or "")
    close_reason = record.get("close_reason")

    qty_by_reason: dict[str, float] = {}
    for f in used:
        oid = str(f.get("order_id") or "")
        if oid in stop_ids or order_types.get(oid) in STOP_ORDER_TYPES:
            reason = "stop"
        elif close_id and oid == close_id:
            reason = close_reason or "manual"
        else:
            reason = close_reason or "unknown"
        qty_by_reason[reason] = qty_by_reason.get(reason, 0.0) + f["used_qty"]
    if not qty_by_reason:
        return close_reason or "unknown"
    best = max(qty_by_reason.values())
    if qty_by_reason.get("stop", -1.0) >= best - 1e-12:
        return "stop"
    return max(qty_by_reason.items(), key=lambda kv: kv[1])[0]

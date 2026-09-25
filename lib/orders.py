"""Order-mode helpers and order payload builders shared by trader.py and tests.

Alpaca behaviour relied on (docs.alpaca.markets, "Fractional Trading" and
"Orders at Alpaca"):

* Fractional trading supports market, limit, stop and stop-limit orders with
  ``time_in_force=day`` only; ``qty`` accepts up to 9 decimal places; fractional
  sells are always long (no fractional shorts).
* A DAY order submitted after the close is queued and submitted the following
  trading day; unfilled DAY orders are cancelled/expired after the close.
* Outside market hours Alpaca may reject a second quantity sell for the same
  fractional position, so we never hold more than one open sell per symbol.
"""

from __future__ import annotations

import secrets
from datetime import date
from typing import Any

from .sizing import format_price, format_qty

MODES = ("fractional", "bracket")
DEFAULT_MODE = "fractional"

ENTRY_PREFIX = "sw-"     # bot entry orders (counted as entries)
STOP_PREFIX = "sl-"      # protective stop orders placed by `protect`
EXIT_PREFIX = "sx-"      # market sells placed by `close` in fractional mode


def order_mode(config: dict[str, Any]) -> str:
    mode = str((config.get("orders") or {}).get("mode", DEFAULT_MODE)).strip().lower()
    if mode not in MODES:
        raise ValueError(f"config orders.mode must be one of {MODES}, got {mode!r}")
    return mode


def client_order_id(prefix: str, symbol: str, today: date) -> str:
    return f"{prefix}{today.strftime('%Y%m%d')}-{symbol.upper()}-{secrets.token_hex(3)}"


def fractional_entry_order(symbol: str, qty: float, limit_price: float, cid: str) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "qty": format_qty(qty),
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",          # fractional orders must be DAY
        "limit_price": format_price(limit_price),
        "client_order_id": cid,
    }


def stop_order(symbol: str, qty: str | float, stop_price: float, cid: str) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "qty": format_qty(qty),
        "side": "sell",
        "type": "stop",
        "time_in_force": "day",          # fractional orders must be DAY; re-placed daily
        "stop_price": format_price(stop_price),
        "client_order_id": cid,
    }


def market_sell_order(symbol: str, qty: str | float, cid: str) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "qty": format_qty(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": cid,
    }

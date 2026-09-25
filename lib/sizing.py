"""Position sizing.

    raw = min(risk_per_trade * equity / (limit - stop),
              max_position_pct * equity / limit) * size_factor

* bracket mode: qty = floor(raw), whole shares only (Alpaca bracket orders
  reject fractional quantities).
* fractional mode: qty = raw rounded DOWN to 9 decimal places (the maximum
  precision Alpaca accepts for fractional qty).

In both modes the order is rejected if qty * limit < min_order_notional.
``size_factor`` (0 < F <= 1, default 1) can only shrink a position. It is
applied after all caps, so it can never raise the size above the limits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

QTY_DECIMALS = 9                     # Alpaca: qty accepts up to 9 decimal places
QTY_QUANTUM = Decimal(1).scaleb(-QTY_DECIMALS)


@dataclass
class SizingResult:
    qty: float                   # int-valued in bracket mode
    risk_qty: float | None
    cap_qty: float | None
    binding: str | None          # "risk" | "position_cap" | None
    reason: str | None = None    # set when the order must be rejected
    size_factor: float = 1.0
    fractional: bool = False
    notional: float = 0.0

    @property
    def ok(self) -> bool:
        return self.qty > 0 and self.reason is None

    def to_dict(self) -> dict[str, Any]:
        return {"qty": self.qty, "risk_qty": self.risk_qty, "cap_qty": self.cap_qty,
                "binding": self.binding, "reason": self.reason,
                "size_factor": self.size_factor, "fractional": self.fractional,
                "notional": self.notional}


def round_to_tick(price: float) -> float:
    """Round to a valid US equity tick: $0.01 at or above $1, $0.0001 below."""
    tick = Decimal("0.01") if price >= 1.0 else Decimal("0.0001")
    return float(Decimal(str(price)).quantize(tick, rounding=ROUND_HALF_UP))


def format_price(price: float) -> str:
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


def floor_qty(qty: float | str | Decimal) -> Decimal:
    """Round a quantity down to Alpaca's 9-decimal precision."""
    return Decimal(str(qty)).quantize(QTY_QUANTUM, rounding=ROUND_DOWN)


def format_qty(qty: float | str | Decimal) -> str:
    """Plain decimal string (no exponent), at most 9 decimals, trailing zeros removed."""
    text = format(floor_qty(qty), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def entry_limit_price(last_price: float, slippage_pct: float) -> float:
    return round_to_tick(last_price * (1.0 + slippage_pct))


def valid_size_factor(value: Any) -> bool:
    """True only for a real number with 0 < value <= 1 (rejects NaN, inf, bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0.0 < float(value) <= 1.0


def size_position(equity: float, limit_price: float, stop_price: float,
                  risk_cfg: dict[str, Any], size_factor: float = 1.0,
                  fractional: bool = False) -> SizingResult:
    def reject(reason: str) -> SizingResult:
        return SizingResult(0, None, None, None, reason, fractional=fractional)

    if not valid_size_factor(size_factor):
        return reject("size_factor must satisfy 0 < F <= 1")
    risk_pct = float(risk_cfg["risk_per_trade_pct_equity"])
    cap_pct = float(risk_cfg["max_position_pct_equity"])
    min_notional = float(risk_cfg.get("min_order_notional", 0.0) or 0.0)
    if equity <= 0:
        return reject("equity is not positive")
    if limit_price <= 0:
        return reject("limit price is not positive")
    per_share_risk = limit_price - stop_price
    if per_share_risk <= 0:
        return reject("stop is not below the limit price")
    risk_qty = risk_pct * equity / per_share_risk
    cap_qty = cap_pct * equity / limit_price
    binding = "risk" if risk_qty <= cap_qty else "position_cap"
    raw = min(risk_qty, cap_qty) * float(size_factor)

    reason: str | None = None
    if fractional:
        # Small epsilon guards against float artefacts like 0.4999999999 -> 0.499999999.
        qty: float = float(floor_qty(raw + 1e-12))
        if qty <= 0:
            reason = "computed quantity rounds to zero"
    else:
        qty = int(math.floor(raw + 1e-9))
        if qty < 1:
            reason = "computed quantity is below 1 whole share"
    notional = qty * limit_price
    if reason is None and notional < min_notional - 1e-9:
        reason = f"order notional {notional:.2f} is below min_order_notional {min_notional:.2f}"
    return SizingResult(max(qty, 0), round(risk_qty, 6), round(cap_qty, 6), binding, reason,
                        float(size_factor), fractional, round(notional, 6))

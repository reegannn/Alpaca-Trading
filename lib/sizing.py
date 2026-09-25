"""Position sizing. Whole shares only (Alpaca bracket orders reject fractional qty).

qty = floor(min(risk_per_trade * equity / (limit - stop),
                max_position_pct * equity / limit) * size_factor)

``size_factor`` (0 < F <= 1, default 1) can only shrink a position. It is applied
after all caps, so it can never raise the size above what the limits allow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


@dataclass
class SizingResult:
    qty: int
    risk_qty: float | None
    cap_qty: float | None
    binding: str | None          # "risk" | "position_cap" | None
    reason: str | None = None    # set when qty < 1
    size_factor: float = 1.0

    @property
    def ok(self) -> bool:
        return self.qty >= 1

    def to_dict(self) -> dict[str, Any]:
        return {"qty": self.qty, "risk_qty": self.risk_qty, "cap_qty": self.cap_qty,
                "binding": self.binding, "reason": self.reason,
                "size_factor": self.size_factor}


def round_to_tick(price: float) -> float:
    """Round to a valid US equity tick: $0.01 at or above $1, $0.0001 below."""
    tick = Decimal("0.01") if price >= 1.0 else Decimal("0.0001")
    return float(Decimal(str(price)).quantize(tick, rounding=ROUND_HALF_UP))


def entry_limit_price(last_price: float, slippage_pct: float) -> float:
    return round_to_tick(last_price * (1.0 + slippage_pct))


def valid_size_factor(value: Any) -> bool:
    """True only for a real number with 0 < value <= 1 (rejects NaN, inf, bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0.0 < float(value) <= 1.0


def size_position(equity: float, limit_price: float, stop_price: float,
                  risk_cfg: dict[str, Any], size_factor: float = 1.0) -> SizingResult:
    if not valid_size_factor(size_factor):
        return SizingResult(0, None, None, None, "size_factor must satisfy 0 < F <= 1")
    risk_pct = float(risk_cfg["risk_per_trade_pct_equity"])
    cap_pct = float(risk_cfg["max_position_pct_equity"])
    if equity <= 0:
        return SizingResult(0, None, None, None, "equity is not positive")
    if limit_price <= 0:
        return SizingResult(0, None, None, None, "limit price is not positive")
    per_share_risk = limit_price - stop_price
    if per_share_risk <= 0:
        return SizingResult(0, None, None, None, "stop is not below the limit price")
    risk_qty = risk_pct * equity / per_share_risk
    cap_qty = cap_pct * equity / limit_price
    binding = "risk" if risk_qty <= cap_qty else "position_cap"
    # size_factor is applied after both caps. Small epsilon guards against
    # float artefacts like 9.999999999 -> 9.
    qty = int(math.floor(min(risk_qty, cap_qty) * float(size_factor) + 1e-9))
    reason = None if qty >= 1 else "computed quantity is below 1 whole share"
    return SizingResult(max(qty, 0), round(risk_qty, 4), round(cap_qty, 4), binding, reason,
                        float(size_factor))

"""Broad setup scan over a liquid universe (``trader.py scan``).

Pure functions over Alpaca asset dicts, snapshots and SIP daily bars, so they
are testable without the API. ``trader.py scan`` makes the requests, all of
them batched:

1. ``GET /v2/assets?status=active&asset_class=us_equity`` (one request), then
   the same filters as ``check``: tradable, allowed exchange, fractionable
   (fractional order mode), plain ticker, not excluded, not a leveraged /
   inverse / volatility fund.
2. Multi-symbol snapshots (``scan.batch_size`` symbols per request) to drop
   symbols below ``universe.min_price`` and rank the rest by the dollar volume
   (close × volume) of the last completed session's daily bar. The top
   ``scan.top_n`` are kept.
3. Multi-symbol SIP daily bars for those symbols and SPY. Each symbol must
   still pass the full universe check (``check_universe``) on the SIP bars.

Patterns (watch lists, not entry signals):

* ``breakout_watch``: close within ``breakout_max_below_high_pct`` (3%) below
  the 20-day high, close above the 50-day SMA, and 20-day SMA above 50-day SMA.
* ``pullback_watch``: 20-day SMA above the 50-day SMA, close above the 50-day
  SMA, and close within ``pullback_max_from_sma20_pct`` (2%) of the 20-day SMA.
* ``relative_strength``: top ``rs_top_fraction`` (decile) of the analysed
  universe by 20-day return minus SPY's 20-day return.

Rows are sorted by pattern (in that order), then by strength (20-day return
vs SPY, descending). A symbol matching several patterns appears once per
pattern; ``patterns`` lists all of them.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

from .market_calendar import NY, parse_ts
from .universe import (SYMBOL_PATTERN, avg_dollar_volume, check_universe, leveraged_match,
                       pct_change, sma)

PATTERNS = ("breakout_watch", "pullback_watch", "relative_strength")
BENCHMARK = "SPY"
ATR_PERIOD = 14
SWING_LOW_LOOKBACK = 10
HIGH_LOOKBACK = 20
RETURN_LOOKBACK = 20
SLOPE_LOOKBACK = 5
MIN_BARS_DAYS = 60       # SMA50 (+5 days for its slope) needs 55 bars; keep a margin
MAX_BATCH_SIZE = 100
EPS = 1e-12


@dataclass(frozen=True)
class ScanParams:
    top_n: int = 300
    bars_days: int = 70
    snapshot_feed: str = "iex"
    batch_size: int = 100
    breakout_max_below_high_pct: float = 0.03
    pullback_max_from_sma20_pct: float = 0.02
    rs_top_fraction: float = 0.10

    @classmethod
    def from_config(cls, scan_cfg: dict[str, Any] | None) -> "ScanParams":
        d = scan_cfg or {}
        p = cls(
            top_n=int(d.get("top_n", cls.top_n)),
            bars_days=int(d.get("bars_days", cls.bars_days)),
            snapshot_feed=str(d.get("snapshot_feed", cls.snapshot_feed)),
            batch_size=int(d.get("batch_size", cls.batch_size)),
            breakout_max_below_high_pct=float(d.get("breakout_max_below_high_pct",
                                                    cls.breakout_max_below_high_pct)),
            pullback_max_from_sma20_pct=float(d.get("pullback_max_from_sma20_pct",
                                                    cls.pullback_max_from_sma20_pct)),
            rs_top_fraction=float(d.get("rs_top_fraction", cls.rs_top_fraction)),
        )
        if not 1 <= p.top_n <= 1000:
            raise ValueError("scan.top_n must be between 1 and 1000")
        if not MIN_BARS_DAYS <= p.bars_days <= 250:
            raise ValueError(f"scan.bars_days must be between {MIN_BARS_DAYS} and 250")
        if not 1 <= p.batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"scan.batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if not 0 < p.rs_top_fraction <= 1:
            raise ValueError("scan.rs_top_fraction must satisfy 0 < x <= 1")
        if p.breakout_max_below_high_pct < 0 or p.pullback_max_from_sma20_pct < 0:
            raise ValueError("scan pattern thresholds must not be negative")
        return p


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "" or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- universe

def universe_assets(assets: list[dict[str, Any]], universe_cfg: dict[str, Any],
                    exclusions: set[str], require_fractionable: bool
                    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Assets that pass the static filters, plus a count of drops by reason."""
    allowed = {str(e).upper() for e in universe_cfg.get("allowed_exchanges", [])}
    keep: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    for a in assets:
        sym = str(a.get("symbol", "")).strip().upper()
        if (a.get("class") or a.get("asset_class")) != "us_equity":
            reason = "not_us_equity"
        elif a.get("status") != "active":
            reason = "inactive"
        elif not a.get("tradable"):
            reason = "not_tradable"
        elif str(a.get("exchange", "")).upper() not in allowed:
            reason = "exchange"
        elif require_fractionable and a.get("fractionable") is not True:
            reason = "not_fractionable"
        elif not SYMBOL_PATTERN.fullmatch(sym):
            reason = "symbol_format"
        elif sym in exclusions:
            reason = "excluded"
        elif leveraged_match(sym, str(a.get("name") or ""), universe_cfg) is not None:
            reason = "leveraged_or_volatility"
        else:
            keep.append(a)
            continue
        dropped[reason] += 1
    return keep, dict(dropped)


def _bar_day(bar: dict[str, Any]) -> date | None:
    try:
        ts = parse_ts(bar.get("t"))
    except ValueError:
        return None
    return ts.astimezone(NY).date() if ts else None


def completed_daily_bar(snapshot: dict[str, Any] | None, session_day: date) -> dict[str, Any] | None:
    """The snapshot's daily bar for ``session_day`` (the last completed session).

    Before the open, ``dailyBar`` is still the last completed session; during the
    session it is today's partial bar and ``prevDailyBar`` is the completed one.
    """
    for key in ("dailyBar", "prevDailyBar"):
        bar = (snapshot or {}).get(key)
        if isinstance(bar, dict) and _bar_day(bar) == session_day:
            return bar
    return None


def rank_by_dollar_volume(snapshots: dict[str, Any], symbols: list[str], session_day: date,
                          min_price: float) -> list[dict[str, Any]]:
    """Symbols priced at or above ``min_price``, by last-session dollar volume (descending)."""
    rows = []
    for sym in symbols:
        bar = completed_daily_bar(snapshots.get(sym), session_day)
        if bar is None:
            continue
        close, volume = _f(bar.get("c")), _f(bar.get("v"))
        if close is None or volume is None or close < min_price:
            continue
        rows.append({"symbol": sym, "prev_close": close, "prev_dollar_volume": close * volume})
    rows.sort(key=lambda r: (-r["prev_dollar_volume"], r["symbol"]))
    for i, r in enumerate(rows, start=1):
        r["dollar_volume_rank"] = i
    return rows


# ----------------------------------------------------------------- metrics

def atr(bars: list[dict[str, Any]], period: int = ATR_PERIOD) -> float | None:
    """Average true range: the simple mean of the last ``period`` true ranges."""
    if len(bars) < period + 1:
        return None
    total = 0.0
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        h, l, pc = float(cur["h"]), float(cur["l"]), float(prev["c"])
        total += max(h - l, abs(h - pc), abs(l - pc))
    return total / period


def bar_metrics(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Daily-bar metrics as fractions (None where history is too short)."""
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    close = closes[-1] if closes else None
    sma20, sma50 = sma(closes, 20), sma(closes, 50)
    sma50_prev = sma(closes[:-SLOPE_LOOKBACK], 50) if len(closes) >= 50 + SLOPE_LOOKBACK else None
    high20 = max(highs[-HIGH_LOOKBACK:]) if len(highs) >= HIGH_LOOKBACK else None
    swing_low = min(lows[-SWING_LOW_LOOKBACK:]) if len(lows) >= SWING_LOW_LOOKBACK else None
    a = atr(bars)
    return {
        "close": close,
        "sma20": sma20,
        "sma50": sma50,
        "sma50_slope_5d": (sma50 / sma50_prev - 1.0) if (sma50 and sma50_prev) else None,
        "atr14": a,
        "atr_pct": (a / close) if (a is not None and close) else None,
        "high_20d": high20,
        "below_20d_high": ((high20 - close) / high20) if (high20 and close is not None) else None,
        "swing_low_10d": swing_low,
        "from_sma20": ((close - sma20) / sma20) if (sma20 and close is not None) else None,
        "return_20d": pct_change(closes, RETURN_LOOKBACK),
        "avg_dollar_volume_20d": avg_dollar_volume(bars),
    }


def pattern_flags(m: dict[str, Any], params: ScanParams) -> list[str]:
    """breakout_watch / pullback_watch for one symbol's metrics."""
    close, s20, s50 = m.get("close"), m.get("sma20"), m.get("sma50")
    if close is None or s20 is None or s50 is None:
        return []
    if not (s20 > s50 and close > s50):
        return []
    out = []
    below = m.get("below_20d_high")
    if below is not None and -EPS <= below <= params.breakout_max_below_high_pct + EPS:
        out.append("breakout_watch")
    dev = m.get("from_sma20")
    if dev is not None and abs(dev) <= params.pullback_max_from_sma20_pct + EPS:
        out.append("pullback_watch")
    return out


def relative_strength_leaders(rs_by_symbol: dict[str, float | None], top_fraction: float) -> set[str]:
    """Symbols in the top ``top_fraction`` by RS (ties at the cut-off are included)."""
    vals = sorted(((v, s) for s, v in rs_by_symbol.items() if v is not None),
                  key=lambda t: (-t[0], t[1]))
    if not vals:
        return set()
    k = max(1, math.ceil(len(vals) * top_fraction))
    cutoff = vals[k - 1][0]
    return {s for v, s in vals if v >= cutoff - EPS}


# -------------------------------------------------------------------- rows

def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def _pct(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v * 100.0, nd)


def _row(sym: str, pattern: str, patterns: list[str], m: dict[str, Any], rs: float | None,
         ranked: dict[str, Any], asset: dict[str, Any] | None) -> dict[str, Any]:
    close, a = m["close"], m["atr14"]
    return {
        "pattern": pattern,
        "symbol": sym,
        "patterns": patterns,
        "name": (asset or {}).get("name"),
        "exchange": (asset or {}).get("exchange"),
        "close": _r(close),
        "sma20": _r(m["sma20"]),
        "sma50": _r(m["sma50"]),
        "sma50_slope_5d_pct": _pct(m["sma50_slope_5d"]),
        "atr14": _r(a, 3),
        "atr_pct": _pct(m["atr_pct"]),
        "high_20d": _r(m["high_20d"]),
        "pct_below_20d_high": _pct(m["below_20d_high"]),
        "swing_low_10d": _r(m["swing_low_10d"]),
        "pct_from_sma20": _pct(m["from_sma20"]),
        "close_minus_1_5_atr": _r(close - 1.5 * a) if (close is not None and a is not None) else None,
        "close_minus_2_atr": _r(close - 2.0 * a) if (close is not None and a is not None) else None,
        "return_20d_pct": _pct(m["return_20d"]),
        "rs_vs_spy_20d_pct": _pct(rs),
        "avg_dollar_volume_20d": _r(m["avg_dollar_volume_20d"], 0),
        "prev_dollar_volume": _r(ranked.get("prev_dollar_volume"), 0),
        "dollar_volume_rank": ranked.get("dollar_volume_rank"),
    }


def analyse(ranked: list[dict[str, Any]], assets_by_symbol: dict[str, dict[str, Any]],
            bars: dict[str, list[dict[str, Any]]], universe_cfg: dict[str, Any],
            exclusions: set[str], require_fractionable: bool,
            params: ScanParams) -> dict[str, Any]:
    """Universe re-check on SIP bars, metrics, pattern flags and sorted rows."""
    spy_ret = bar_metrics(bars.get(BENCHMARK, []))["return_20d"]
    warnings: list[str] = []
    if spy_ret is None:
        warnings.append(f"{BENCHMARK} bars unavailable: relative strength not computed")

    metrics: dict[str, dict[str, Any]] = {}
    ineligible = 0
    failed_checks: Counter[str] = Counter()
    for r in ranked:
        sym = r["symbol"]
        b = bars.get(sym, [])
        res = check_universe(sym, assets_by_symbol.get(sym), b, universe_cfg, exclusions,
                             require_fractionable=require_fractionable)
        if not res.eligible:
            ineligible += 1
            failed_checks.update(name for name, c in res.checks.items() if not c["pass"])
            continue
        metrics[sym] = bar_metrics(b)

    rs = {s: (m["return_20d"] - spy_ret) if (m["return_20d"] is not None and spy_ret is not None)
          else None for s, m in metrics.items()}
    leaders = relative_strength_leaders(rs, params.rs_top_fraction)
    by_rank = {r["symbol"]: r for r in ranked}

    rows: list[dict[str, Any]] = []
    for sym, m in metrics.items():
        patterns = pattern_flags(m, params)
        if sym in leaders:
            patterns.append("relative_strength")
        for p in patterns:
            rows.append(_row(sym, p, patterns, m, rs[sym], by_rank[sym], assets_by_symbol.get(sym)))
    rows.sort(key=lambda row: (
        PATTERNS.index(row["pattern"]),
        -(row["rs_vs_spy_20d_pct"] if row["rs_vs_spy_20d_pct"] is not None else -math.inf),
        row["symbol"],
    ))
    return {
        "analysed": len(metrics),
        "ineligible_on_sip_bars": {"symbols": ineligible, "failed_checks": dict(failed_checks)},
        "benchmark": {"symbol": BENCHMARK, "return_20d_pct": _pct(spy_ret)},
        "counts": {p: sum(1 for row in rows if row["pattern"] == p) for p in PATTERNS},
        "rows": rows,
        "warnings": warnings,
    }

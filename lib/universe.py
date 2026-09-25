"""Universe eligibility filters.

Pure functions over an Alpaca asset dict and a list of SIP daily bars
(ascending), so they are testable without the API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DOLLAR_VOLUME_LOOKBACK = 20

# Used only if config.yaml has no universe.fund_name_indicators.
DEFAULT_FUND_NAME_INDICATORS = (
    "ETF", "ETFS", "ETN", "ETNS", "ETP", "FUND", "TRUST", "PROSHARES", "DIREXION",
)


@dataclass
class UniverseResult:
    symbol: str
    eligible: bool
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {c['detail']}" for name, c in self.checks.items() if not c["pass"]]

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "eligible": self.eligible,
                "checks": self.checks, "failures": self.failures}


def load_exclusions(universe_cfg: dict[str, Any], repo_root: Path) -> set[str]:
    path = universe_cfg.get("exclude_symbols_file")
    if not path:
        return set()
    p = (repo_root / path) if not Path(path).is_absolute() else Path(path)
    if not p.exists():
        return set()
    out: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            out.add(line)
    return out


@dataclass(frozen=True)
class LeveragedMatch:
    """Why a symbol was treated as leveraged / inverse / volatility-linked."""
    source: str                    # "symbol_list" | "name_pattern"
    entry: str                     # the list entry or pattern that matched
    fund_indicator: str | None     # the word that marked the name as a fund
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "entry": self.entry,
                "fund_indicator": self.fund_indicator, "reason": self.reason}


def word_match(term: str, upper_text: str) -> bool:
    """Case-insensitive whole-word match.

    Letters, digits and hyphens count as word characters, so "ULTRA" does not
    match "Ultragenyx" or "Ultra-Short", "SHORT" does not match "Short-Term",
    and "2X" does not match inside "-2X" (the "-2X" pattern covers that).
    """
    t = term.strip().upper()
    if not t:
        return False
    return re.search(rf"(?<![A-Z0-9-]){re.escape(t)}(?![A-Z0-9-])", upper_text) is not None


def fund_indicator(name: str, universe_cfg: dict[str, Any]) -> str | None:
    """The first fund-indicating word in the asset name, or None for common stocks."""
    upper = (name or "").upper()
    for ind in universe_cfg.get("fund_name_indicators") or DEFAULT_FUND_NAME_INDICATORS:
        if word_match(str(ind), upper):
            return str(ind).upper()
    return None


def leveraged_match(symbol: str, name: str, universe_cfg: dict[str, Any]) -> LeveragedMatch | None:
    """Return a match if the symbol/name looks leveraged, inverse or volatility-linked.

    * Any asset on ``leveraged_etf_symbols`` is excluded.
    * Name patterns apply only to funds (name contains a ``fund_name_indicators``
      word) and must match whole words. Common stocks are therefore excluded only
      via the explicit symbol list (e.g. "Ultra Clean Holdings" is allowed).
    """
    sym = symbol.upper()
    for entry in universe_cfg.get("leveraged_etf_symbols", []):
        if str(entry).upper() == sym:
            return LeveragedMatch("symbol_list", str(entry).upper(), None,
                                  f"{sym} is on leveraged_etf_symbols")
    ind = fund_indicator(name, universe_cfg)
    if ind is None:
        return None
    upper_name = (name or "").upper()
    for pat in universe_cfg.get("leveraged_etf_name_patterns", []):
        if word_match(str(pat), upper_name):
            p = str(pat).upper()
            return LeveragedMatch("name_pattern", p, ind,
                                  f"fund name (indicator {ind!r}) matches "
                                  f"leveraged_etf_name_patterns entry {p!r}")
    return None


def avg_dollar_volume(bars: list[dict[str, Any]], lookback: int = DOLLAR_VOLUME_LOOKBACK) -> float | None:
    if len(bars) < lookback:
        return None
    window = bars[-lookback:]
    return sum(float(b["c"]) * float(b["v"]) for b in window) / lookback


def check_universe(symbol: str, asset: dict[str, Any] | None,
                   bars: list[dict[str, Any]], universe_cfg: dict[str, Any],
                   exclusions: set[str] | None = None) -> UniverseResult:
    """Evaluate every filter and report pass/fail per filter (never short-circuits)."""
    sym = symbol.upper()
    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    if asset is None:
        add("asset", False, "asset not found")
    else:
        cls = asset.get("class") or asset.get("asset_class")
        ok = cls == "us_equity" and asset.get("status") == "active" and bool(asset.get("tradable"))
        add("asset", ok, f"class={cls}, status={asset.get('status')}, tradable={asset.get('tradable')}")

        exch = str(asset.get("exchange", ""))
        allowed = [str(e).upper() for e in universe_cfg.get("allowed_exchanges", [])]
        add("exchange", exch.upper() in allowed, f"exchange={exch}")

    lev = leveraged_match(sym, (asset or {}).get("name", ""), universe_cfg)
    add("not_leveraged", lev is None, lev.reason if lev else "ok")
    checks["not_leveraged"]["matched"] = lev.to_dict() if lev else None

    if exclusions:
        add("not_excluded", sym not in exclusions, "in exclude_symbols_file" if sym in exclusions else "ok")

    min_price = float(universe_cfg.get("min_price", 0))
    last_close = float(bars[-1]["c"]) if bars else None
    if last_close is None:
        add("min_price", False, "no daily bars")
    else:
        add("min_price", last_close >= min_price, f"last_close={last_close:.2f} min={min_price:.2f}")

    min_dv = float(universe_cfg.get("min_avg_dollar_volume_20d", 0))
    adv = avg_dollar_volume(bars)
    if adv is None:
        add("dollar_volume", False, f"fewer than {DOLLAR_VOLUME_LOOKBACK} daily bars")
    else:
        add("dollar_volume", adv >= min_dv, f"avg_dollar_volume_20d={adv:,.0f} min={min_dv:,.0f}")

    eligible = all(c["pass"] for c in checks.values())
    return UniverseResult(sym, eligible, checks)


def sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def pct_change(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[-1 - n] == 0:
        return None
    return closes[-1] / closes[-1 - n] - 1.0


def screen_metrics(bars: list[dict[str, Any]], price: float | None) -> dict[str, Any]:
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    high20 = max(highs[-20:]) if len(highs) >= 20 else None
    px = price if price is not None else (closes[-1] if closes else None)
    return {
        "price": px,
        "last_close": closes[-1] if closes else None,
        "avg_dollar_volume_20d": avg_dollar_volume(bars),
        "change_1d_pct": _pct(pct_change(closes, 1)),
        "change_5d_pct": _pct(pct_change(closes, 5)),
        "sma20": sma(closes, 20),
        "sma50": sma(closes, 50),
        "high_20d": high20,
        "pct_from_20d_high": _pct(px / high20 - 1.0) if (px and high20) else None,
    }


def _pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100.0, 2)

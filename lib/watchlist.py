"""Load and strictly validate state/watchlist/<date>.yaml.

File-level errors (wrong date, too many candidates, duplicate symbols,
unparseable file) invalidate every entry. Entry-level errors invalidate only
that entry. ``enter`` refuses any symbol whose entry has errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .market_calendar import parse_date

# Keep in sync with CLAUDE.md sections 4 and 5 (both files are protected).
SETUP_TAGS = frozenset({
    "breakout", "pullback_to_trend", "relative_strength",
    "post_earnings_drift", "oversold_mean_reversion", "catalyst_news",
})
IDEA_SOURCES = frozenset({
    "screener", "news_catalyst", "sector_rotation",
    "earnings_followthrough", "web_research", "other",
})
ASSET_TYPES = frozenset({"stock", "etf"})
TRIGGER_TYPES = frozenset({"above", "below"})
MAX_CANDIDATES = 8


@dataclass
class Watchlist:
    path: Path | None
    date: date | None
    file_errors: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    entry_errors: dict[str, list[str]] = field(default_factory=dict)

    def find(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.upper()
        for c in self.candidates:
            if str(c.get("symbol", "")).upper() == sym:
                return c
        return None

    def errors_for(self, symbol: str) -> list[str]:
        """All errors that make ``symbol`` non-enterable (file + entry level)."""
        errs = list(self.file_errors)
        if self.find(symbol) is None:
            errs.append(f"{symbol.upper()} is not in today's watchlist")
        else:
            errs.extend(self.entry_errors.get(symbol.upper(), []))
        return errs


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def earnings_value(raw: Any) -> date | str | None:
    """Return a date, 'unknown', 'n/a', or None if invalid."""
    if isinstance(raw, date):
        return parse_date(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("unknown", "n/a"):
            return s
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


def validate_candidate(c: Any) -> list[str]:
    """Entry-level validation. Returns a list of error strings (empty = valid)."""
    if not isinstance(c, dict):
        return ["candidate is not a mapping"]
    errs: list[str] = []
    symbol = c.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        errs.append("symbol missing")
    asset_type = c.get("asset_type")
    if asset_type not in ASSET_TYPES:
        errs.append(f"asset_type must be one of {sorted(ASSET_TYPES)}")
    for key in ("sector", "thesis", "catalyst"):
        v = c.get(key)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{key} missing or empty")
    if c.get("setup_tag") not in SETUP_TAGS:
        errs.append(f"setup_tag {c.get('setup_tag')!r} not in allowed list")
    if c.get("idea_source") not in IDEA_SOURCES:
        errs.append(f"idea_source {c.get('idea_source')!r} not in allowed list")

    trigger = c.get("trigger")
    tprice: float | None = None
    if not isinstance(trigger, dict):
        errs.append("trigger missing")
    else:
        if trigger.get("type") not in TRIGGER_TYPES:
            errs.append("trigger.type must be 'above' or 'below'")
        tprice = _num(trigger.get("price"))
        if tprice is None or tprice <= 0:
            errs.append("trigger.price must be a positive number")
            tprice = None

    stop = _num(c.get("stop"))
    target = _num(c.get("target"))
    if stop is None or stop <= 0:
        errs.append("stop must be a positive number")
    if target is None or target <= 0:
        errs.append("target must be a positive number")
    if stop is not None and target is not None and stop > 0 and target > 0:
        if not stop < target:
            errs.append("stop must be below target")
        if tprice is not None and not (stop < tprice < target):
            errs.append("trigger.price must be between stop and target")

    ev = earnings_value(c.get("earnings_date"))
    if ev is None:
        errs.append("earnings_date must be a date, 'unknown', or 'n/a'")
    elif ev == "n/a" and asset_type != "etf":
        errs.append("earnings_date 'n/a' is only allowed for ETFs")
    elif ev == "unknown" and asset_type == "stock":
        errs.append("earnings_date is 'unknown' for a stock (entry blocked)")

    sources = c.get("sources")
    if not isinstance(sources, list) or not any(isinstance(s, str) and s.strip() for s in sources):
        errs.append("sources must be a non-empty list")
    return errs


def validate_watchlist(data: Any, today: date, path: Path | None = None) -> Watchlist:
    wl = Watchlist(path=path, date=None)
    if not isinstance(data, dict):
        wl.file_errors.append("watchlist file is not a mapping")
        return wl
    wl.date = parse_date(data.get("date"))
    if wl.date != today:
        wl.file_errors.append(f"watchlist date {data.get('date')!r} is not today ({today.isoformat()})")
    cands = data.get("candidates")
    if cands is None:
        cands = []
    if not isinstance(cands, list):
        wl.file_errors.append("candidates must be a list")
        return wl
    if len(cands) > MAX_CANDIDATES:
        wl.file_errors.append(f"too many candidates ({len(cands)} > {MAX_CANDIDATES})")
    seen: set[str] = set()
    for c in cands:
        sym = str(c.get("symbol", "")).strip().upper() if isinstance(c, dict) else ""
        if sym and sym in seen:
            wl.file_errors.append(f"duplicate symbol {sym}")
        if sym:
            seen.add(sym)
            c = dict(c)
            c["symbol"] = sym
        wl.candidates.append(c if isinstance(c, dict) else {})
        if sym:
            wl.entry_errors[sym] = validate_candidate(c)
    return wl


def load_watchlist(path: Path, today: date) -> Watchlist:
    if not path.exists():
        wl = Watchlist(path=path, date=None)
        wl.file_errors.append(f"no watchlist for {today.isoformat()}")
        return wl
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        wl = Watchlist(path=path, date=None)
        wl.file_errors.append(f"watchlist YAML parse error: {type(exc).__name__}")
        return wl
    return validate_watchlist(data, today, path)

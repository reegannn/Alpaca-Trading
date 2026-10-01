"""Load and strictly validate state/watchlist/<date>.yaml.

File-level errors (wrong date, too many candidates, duplicate symbols,
unparseable file) invalidate every entry. Entry-level errors invalidate only
that entry. ``enter`` refuses any symbol whose entry has errors.

Entry zones
-----------
A candidate's trigger is an entry zone, ``{type: zone, low: X, high: Y}``. A
trade run may enter only while ``low <= last price <= high``.

The numeric zone rules (``ZoneRules``, built from config.yaml) guarantee that
every price inside the zone passes ``enter``'s own reward:risk and
stop-distance checks. Both are evaluated at the worst price in the zone: the
limit ``enter`` would send at the zone top, ``high × (1 + entry_limit_slippage_pct)``
rounded to the tick exactly as ``enter`` rounds it. Reward:risk only falls,
and stop distance only grows, as the entry price rises, so passing at the top
means passing everywhere in the zone.

``reference_price`` (the latest price when the research run validated the
file) and ``in_zone_at_research`` are written by
``trader.py validate-watchlist``. A candidate without them has not been
validated and cannot be entered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .market_calendar import parse_date
from .sizing import entry_limit_price

# Keep in sync with CLAUDE.md sections 4 and 5 (both files are protected).
SETUP_TAGS = frozenset({
    "breakout", "pullback_to_trend", "relative_strength",
    "post_earnings_drift", "oversold_mean_reversion", "catalyst_news",
})
IDEA_SOURCES = frozenset({
    "screener", "news_catalyst", "sector_rotation",
    "earnings_followthrough", "web_research", "other",
})
TARGET_BASES = frozenset({"resistance", "prior_high", "measured_move", "atr_multiple"})
ASSET_TYPES = frozenset({"stock", "etf"})
ZONE = "zone"
MAX_CANDIDATES = 8
EPS = 1e-12
VALIDATE_HINT = "run `python trader.py validate-watchlist`"


@dataclass(frozen=True)
class ZoneRules:
    """The config limits that every price in an entry zone must satisfy."""
    min_width_pct: float           # entry.zone_min_width_pct, fraction of zone low
    max_width_pct: float           # entry.zone_max_width_pct, fraction of zone low
    min_reward_to_risk: float      # risk.min_reward_to_risk
    max_stop_distance_pct: float   # risk.max_stop_distance_pct
    slippage_pct: float            # risk.entry_limit_slippage_pct

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ZoneRules":
        try:
            entry, risk = config["entry"], config["risk"]
            return cls(
                min_width_pct=float(entry["zone_min_width_pct"]),
                max_width_pct=float(entry["zone_max_width_pct"]),
                min_reward_to_risk=float(risk["min_reward_to_risk"]),
                max_stop_distance_pct=float(risk["max_stop_distance_pct"]),
                slippage_pct=float(risk["entry_limit_slippage_pct"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"config.yaml: missing entry-zone setting {exc}") from None


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


# ------------------------------------------------------------------ zones

def zone_bounds(cand: Any) -> tuple[float, float] | None:
    """(low, high) of a well-formed entry zone, else None."""
    trig = cand.get("trigger") if isinstance(cand, dict) else None
    if not isinstance(trig, dict) or trig.get("type") != ZONE:
        return None
    low, high = _num(trig.get("low")), _num(trig.get("high"))
    if low is None or high is None or low <= 0 or high <= 0:
        return None
    return low, high


def in_zone(price: float, low: float, high: float) -> bool:
    return low <= price <= high


def distance_to_zone(price: float, low: float, high: float) -> float:
    """Fractional move from ``price`` to the nearest zone edge (0 inside the zone).

    Positive: price must rise to reach the zone. Negative: price must fall.
    """
    if price < low:
        return (low - price) / price
    if price > high:
        return (high - price) / price
    return 0.0


def zone_metrics(cand: dict[str, Any], rules: ZoneRules) -> dict[str, Any]:
    """The numbers behind the zone rules; None wherever an input is missing or invalid."""
    m: dict[str, Any] = {
        "zone_low": None, "zone_high": None, "zone_width": None, "limit_at_zone_top": None,
        "reward_to_risk_at_zone_top": None, "stop_distance_at_zone_top": None,
        "reference_price": None, "distance_to_zone": None, "in_zone": None,
    }
    bounds = zone_bounds(cand)
    if bounds is None:
        return m
    low, high = bounds
    stop, target = _num(cand.get("stop")), _num(cand.get("target"))
    top = entry_limit_price(high, rules.slippage_pct)
    m.update(zone_low=low, zone_high=high, zone_width=(high - low) / low, limit_at_zone_top=top)
    if stop is not None and target is not None and stop < top < target:
        m["reward_to_risk_at_zone_top"] = (target - top) / (top - stop)
        m["stop_distance_at_zone_top"] = (top - stop) / top
    ref = _num(cand.get("reference_price"))
    if ref is not None and ref > 0:
        m["reference_price"] = ref
        m["in_zone"] = in_zone(ref, low, high)
        m["distance_to_zone"] = distance_to_zone(ref, low, high)
    return m


def zone_warnings(cand: dict[str, Any], rules: ZoneRules) -> list[str]:
    """Non-blocking notes for the research run (shown by validate-watchlist)."""
    m = zone_metrics(cand, rules)
    if m["in_zone"] is True:
        return ["price is already inside the zone at research time: the thesis must explicitly "
                "be 'enter now'"]
    if m["distance_to_zone"] is not None and m["distance_to_zone"] < 0:
        return ["price is above the zone at research time: entry needs a pullback into it"]
    return []


def _zone_errors(c: dict[str, Any], rules: ZoneRules) -> list[str]:
    errs: list[str] = []
    trigger = c.get("trigger")
    low = high = None
    if not isinstance(trigger, dict):
        errs.append("trigger missing (expected {type: zone, low: X, high: Y})")
    else:
        if trigger.get("type") != ZONE:
            errs.append(f"trigger.type must be 'zone' (got {trigger.get('type')!r}); "
                        "above/below triggers are no longer supported")
        low, high = _num(trigger.get("low")), _num(trigger.get("high"))
        if low is None or low <= 0:
            errs.append("trigger.low must be a positive number")
            low = None
        if high is None or high <= 0:
            errs.append("trigger.high must be a positive number")
            high = None

    stop = _num(c.get("stop"))
    target = _num(c.get("target"))
    if stop is None or stop <= 0:
        errs.append("stop must be a positive number")
        stop = None
    if target is None or target <= 0:
        errs.append("target must be a positive number")
        target = None
    if errs or low is None or high is None or stop is None or target is None:
        return errs

    if not (stop < low <= high < target):
        errs.append(f"require stop < zone low <= zone high < target "
                    f"(stop={stop}, low={low}, high={high}, target={target})")
        return errs

    m = zone_metrics(c, rules)
    width = m["zone_width"]
    if width < rules.min_width_pct - EPS:
        errs.append(f"zone width {width:.3%} of low is below the minimum {rules.min_width_pct:.3%}")
    if width > rules.max_width_pct + EPS:
        errs.append(f"zone width {width:.3%} of low is above the maximum {rules.max_width_pct:.3%}")

    top = m["limit_at_zone_top"]
    rr, dist = m["reward_to_risk_at_zone_top"], m["stop_distance_at_zone_top"]
    if rr is None or dist is None:
        errs.append(f"limit at the zone top ({top}) must lie between stop and target")
        return errs
    if rr < rules.min_reward_to_risk - EPS:
        errs.append(f"reward:risk at the zone top is {rr:.3f} (limit {top}), below the minimum "
                    f"{rules.min_reward_to_risk:.2f}: lower the zone or the stop, or justify a higher target")
    if dist > rules.max_stop_distance_pct + EPS:
        errs.append(f"stop distance at the zone top is {dist:.3%} (limit {top}), above the maximum "
                    f"{rules.max_stop_distance_pct:.2%}")
    return errs


# ------------------------------------------------------------- validation

def validate_candidate(c: Any, rules: ZoneRules) -> list[str]:
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

    errs.extend(_zone_errors(c, rules))

    if c.get("target_basis") not in TARGET_BASES:
        errs.append(f"target_basis {c.get('target_basis')!r} must be one of {sorted(TARGET_BASES)}")
    note = c.get("target_note")
    if not isinstance(note, str) or not note.strip():
        errs.append("target_note missing or empty (one line justifying the target)")
    elif "\n" in note.strip():
        errs.append("target_note must be a single line")

    ref = _num(c.get("reference_price"))
    if ref is None or ref <= 0:
        errs.append(f"reference_price missing; {VALIDATE_HINT}")
    bounds = zone_bounds(c)
    if bounds is not None:
        flag = c.get("in_zone_at_research")
        if not isinstance(flag, bool):
            errs.append(f"in_zone_at_research missing; {VALIDATE_HINT}")
        elif ref is not None and ref > 0 and flag != in_zone(ref, *bounds):
            errs.append(f"in_zone_at_research does not match reference_price and the zone; {VALIDATE_HINT}")

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


def validate_watchlist(data: Any, today: date, rules: ZoneRules,
                       path: Path | None = None) -> Watchlist:
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
            wl.entry_errors[sym] = validate_candidate(c, rules)
    return wl


def read_watchlist_yaml(path: Path) -> Any:
    """Parsed YAML. Raises ValueError for a missing or unparseable file."""
    if not path.exists():
        raise ValueError(f"no watchlist file at {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"watchlist YAML parse error: {type(exc).__name__}") from None


def load_watchlist(path: Path, today: date, rules: ZoneRules) -> Watchlist:
    if not path.exists():
        wl = Watchlist(path=path, date=None)
        wl.file_errors.append(f"no watchlist for {today.isoformat()}")
        return wl
    try:
        data = read_watchlist_yaml(path)
    except ValueError as exc:
        wl = Watchlist(path=path, date=None)
        wl.file_errors.append(str(exc))
        return wl
    return validate_watchlist(data, today, rules, path)


# --------------------------------------------------- validate-watchlist fill

def fill_research_fields(data: dict[str, Any], prices: dict[str, float | None],
                         generated_at: str) -> list[str]:
    """Write the machine-filled fields into a parsed watchlist, in place.

    Sets ``generated_at``; for every candidate, normalises the symbol to upper
    case and sets ``reference_price`` (from ``prices``) and
    ``in_zone_at_research``. A candidate with no price loses any stale
    ``reference_price`` / ``in_zone_at_research`` so it cannot pass validation.
    Returns one error per candidate whose price was unavailable.
    """
    data["generated_at"] = generated_at
    errors: list[str] = []
    for c in data.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol", "")).strip().upper()
        if sym:
            c["symbol"] = sym
        price = prices.get(sym)
        if price is None or price <= 0:
            c.pop("reference_price", None)
            c.pop("in_zone_at_research", None)
            errors.append(f"{sym or '(no symbol)'}: no latest trade price; reference_price not set")
            continue
        c["reference_price"] = round(float(price), 4)
        bounds = zone_bounds(c)
        if bounds is None:
            c.pop("in_zone_at_research", None)
        else:
            c["in_zone_at_research"] = in_zone(c["reference_price"], *bounds)
    return errors


def dump_watchlist_yaml(data: dict[str, Any]) -> str:
    """YAML text with the original key order (comments are not preserved)."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

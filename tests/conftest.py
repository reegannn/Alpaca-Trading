"""Shared test fixtures. Tests use mocked API data only; no network access."""

from __future__ import annotations

import copy
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Make sure the paper guard never trips because of the developer's environment.
for _var in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_ENDPOINT"):
    os.environ.pop(_var, None)

TODAY = date(2026, 9, 28)  # a Monday


def weekday_calendar_rows(start: date, end: date) -> list[dict[str, str]]:
    """Alpaca /v2/calendar-shaped rows for every weekday (no holidays)."""
    rows = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            rows.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
        d += timedelta(days=1)
    return rows


def daily_bars(n: int, close: float, volume: float, end: date = TODAY - timedelta(days=3),
               high: float | None = None, low: float | None = None) -> list[dict[str, Any]]:
    """n SIP-style daily bars ending on ``end`` (weekdays only), ascending."""
    out: list[dict[str, Any]] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"t": f"{d.isoformat()}T04:00:00Z", "o": close, "h": high or close,
                        "l": low or close, "c": close, "v": volume, "n": 1000, "vw": close})
        d -= timedelta(days=1)
    return list(reversed(out))


BASE_CONFIG: dict[str, Any] = {
    # Universe filters mirror the real config so tests track config.yaml changes.
    "universe": yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))["universe"],
    "risk": {
        "long_only": True,
        "max_position_pct_equity": 0.10,
        "risk_per_trade_pct_equity": 0.01,
        "max_open_positions": 8,
        "max_gross_exposure_pct_equity": 1.00,
        "max_sector_pct_equity": 0.30,
        "max_new_entries_per_day": 2,
        "daily_loss_circuit_breaker_pct": -0.02,
        "earnings_blackout_trading_days": 3,
        "min_reward_to_risk": 1.5,
        "max_stop_distance_pct": 0.12,
        # 0 in tests so boundary arithmetic is exact; slippage is tested separately.
        "entry_limit_slippage_pct": 0.0,
        "no_entry_minutes_after_open": 15,
        "no_entry_minutes_before_close": 15,
    },
    "holding": {"max_hold_days": 10},
    "notifications": {"slack_channel": "#alpaca-paper-bot"},
    "data": {"latest_feed": "iex", "bars_feed": "sip", "bars_adjustment": "split"},
}


@pytest.fixture
def config() -> dict[str, Any]:
    return copy.deepcopy(BASE_CONFIG)


def candidate(**overrides: Any) -> dict[str, Any]:
    c: dict[str, Any] = {
        "symbol": "XYZ",
        "asset_type": "stock",
        "sector": "Technology",
        "setup_tag": "pullback_to_trend",
        "idea_source": "screener",
        "thesis": "Uptrend pullback to the 20-day SMA.",
        "catalyst": "Product launch confirmed.",
        "trigger": {"type": "above", "price": 100.0},
        "stop": 95.0,
        "target": 115.0,
        "earnings_date": date(2026, 10, 20),
        "sources": ["https://example.com/a"],
    }
    c.update(overrides)
    return c


def watchlist_data(*cands: dict[str, Any], day: date = TODAY) -> dict[str, Any]:
    return {"date": day, "generated_at": "2026-09-28T12:05:00Z",
            "candidates": list(cands) if cands else [candidate()]}


def ny(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ZoneInfo("America/New_York"))


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

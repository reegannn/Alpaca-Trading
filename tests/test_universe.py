"""Universe filters: leveraged ETF detection, OTC exclusion, price and dollar-volume thresholds."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import daily_bars
from lib.universe import avg_dollar_volume, check_universe, fund_indicator, leveraged_match, word_match


def asset(**overrides: Any) -> dict[str, Any]:
    a = {"symbol": "XYZ", "class": "us_equity", "status": "active", "tradable": True,
         "exchange": "NASDAQ", "name": "XYZ Holdings Inc. Common Stock"}
    a.update(overrides)
    return a


GOOD_BARS = daily_bars(30, 50.0, 1_000_000)   # $50M/day


def test_eligible(config: dict[str, Any]) -> None:
    r = check_universe("XYZ", asset(), GOOD_BARS, config["universe"])
    assert r.eligible, r.failures


@pytest.mark.parametrize("sym", ["TQQQ", "SQQQ", "UVXY", "tsll"])
def test_leveraged_by_symbol(config: dict[str, Any], sym: str) -> None:
    r = check_universe(sym, asset(symbol=sym.upper()), GOOD_BARS, config["universe"])
    assert not r.eligible
    assert not r.checks["not_leveraged"]["pass"]


@pytest.mark.parametrize("name", [
    "ProShares UltraPro QQQ",
    "Direxion Daily Semiconductor Bull 3X Shares",
    "Some Fund -2x Daily ETF",
    "iPath Series B S&P 500 VIX Short-Term Futures ETN",
    "ProShares Short S&P500",
    "AXS 1.5x Inverse Fund",
    "some leveraged fund",    # case-insensitive
    "ProShares UltraShort Bloomberg Crude Oil",
])
def test_leveraged_by_name_pattern(config: dict[str, Any], name: str) -> None:
    assert leveraged_match("ABCD", name, config["universe"]) is not None
    r = check_universe("ABCD", asset(symbol="ABCD", name=name), GOOD_BARS, config["universe"])
    assert not r.eligible


def test_plain_name_not_flagged(config: dict[str, Any]) -> None:
    assert leveraged_match("SPY", "SPDR S&P 500 ETF Trust", config["universe"]) is None


def test_ultra_clean_holdings_allowed(config: dict[str, Any]) -> None:
    # A common stock: name patterns do not apply, and UCTT is not on the symbol list.
    name = "Ultra Clean Holdings, Inc. Common Stock"
    assert fund_indicator(name, config["universe"]) is None
    assert leveraged_match("UCTT", name, config["universe"]) is None
    r = check_universe("UCTT", asset(symbol="UCTT", name=name), GOOD_BARS, config["universe"])
    assert r.eligible, r.failures
    assert r.checks["not_leveraged"]["matched"] is None


def test_known_leveraged_etf_matched_by_name(config: dict[str, Any]) -> None:
    # USD = ProShares Ultra Semiconductors (2x), not on the explicit symbol list.
    assert "USD" not in config["universe"]["leveraged_etf_symbols"]
    r = check_universe("USD", asset(symbol="USD", exchange="ARCA", name="ProShares Ultra Semiconductors"),
                       GOOD_BARS, config["universe"])
    assert not r.eligible
    check = r.checks["not_leveraged"]
    assert not check["pass"]
    assert check["matched"] == {"source": "name_pattern", "entry": "ULTRA",
                                "fund_indicator": "PROSHARES", "reason": check["detail"]}
    assert "ULTRA" in check["detail"] and "PROSHARES" in check["detail"]


def test_symbol_list_match_reports_entry(config: dict[str, Any]) -> None:
    r = check_universe("TQQQ", asset(symbol="TQQQ", name="ProShares UltraPro QQQ"),
                       GOOD_BARS, config["universe"])
    matched = r.checks["not_leveraged"]["matched"]
    assert matched["source"] == "symbol_list" and matched["entry"] == "TQQQ"
    assert "leveraged_etf_symbols" in r.checks["not_leveraged"]["detail"]


@pytest.mark.parametrize("sym,name", [
    ("BHH", "Bull Horn Holdings Common Stock"),      # common stock: patterns never apply
    ("BEAR", "Bear Creek Mining Corp"),
    ("BSV", "Vanguard Short-Term Bond ETF"),          # "Short-Term" is not the word SHORT
    ("JPST", "JPMorgan Ultra-Short Income ETF"),      # "Ultra-Short" is not ULTRA or SHORT
    ("RARE", "Ultragenyx Pharmaceutical Inc."),
])
def test_whole_word_and_fund_only_matching(config: dict[str, Any], sym: str, name: str) -> None:
    assert leveraged_match(sym, name, config["universe"]) is None


def test_common_stock_excluded_only_via_symbol_list(config: dict[str, Any]) -> None:
    cfg = {**config["universe"], "leveraged_etf_symbols": ["UCTT"]}
    m = leveraged_match("UCTT", "Ultra Clean Holdings, Inc. Common Stock", cfg)
    assert m is not None and m.source == "symbol_list"


def test_word_match_rules() -> None:
    assert word_match("2X", "BULL 2X SHARES")
    assert word_match("-2X", "FUND -2X DAILY")
    assert not word_match("2X", "FUND -2X DAILY")      # covered by the "-2X" pattern instead
    assert not word_match("ULTRA", "ULTRAPRO QQQ")
    assert word_match("GLOBAL X", "GLOBAL X FUNDS")


def test_otc_excluded(config: dict[str, Any]) -> None:
    r = check_universe("XYZ", asset(exchange="OTC"), GOOD_BARS, config["universe"])
    assert not r.eligible
    assert not r.checks["exchange"]["pass"]


@pytest.mark.parametrize("field,value", [
    ("status", "inactive"), ("tradable", False), ("class", "crypto"),
])
def test_asset_status_filters(config: dict[str, Any], field: str, value: Any) -> None:
    r = check_universe("XYZ", asset(**{field: value}), GOOD_BARS, config["universe"])
    assert not r.checks["asset"]["pass"]


def test_asset_missing(config: dict[str, Any]) -> None:
    assert not check_universe("XYZ", None, GOOD_BARS, config["universe"]).eligible


def test_min_price_boundary(config: dict[str, Any]) -> None:
    at = check_universe("XYZ", asset(), daily_bars(30, 5.0, 10_000_000), config["universe"])
    assert at.checks["min_price"]["pass"]
    below = check_universe("XYZ", asset(), daily_bars(30, 4.99, 10_000_000), config["universe"])
    assert not below.checks["min_price"]["pass"]


def test_dollar_volume_boundary(config: dict[str, Any]) -> None:
    at = check_universe("XYZ", asset(), daily_bars(30, 20.0, 1_000_000), config["universe"])   # $20M
    assert at.checks["dollar_volume"]["pass"]
    below = check_universe("XYZ", asset(), daily_bars(30, 20.0, 999_999), config["universe"])
    assert not below.checks["dollar_volume"]["pass"]


def test_dollar_volume_uses_last_20_bars() -> None:
    bars = daily_bars(10, 10.0, 1.0) + daily_bars(20, 10.0, 100.0)
    assert avg_dollar_volume(bars) == 1000.0


def test_too_few_bars(config: dict[str, Any]) -> None:
    r = check_universe("XYZ", asset(), daily_bars(19, 50.0, 1_000_000), config["universe"])
    assert not r.checks["dollar_volume"]["pass"]


def test_all_failures_reported(config: dict[str, Any]) -> None:
    r = check_universe("TQQQ", asset(symbol="TQQQ", exchange="OTC", status="inactive"),
                       daily_bars(30, 1.0, 100), config["universe"])
    failed = {name for name, c in r.checks.items() if not c["pass"]}
    assert {"asset", "exchange", "not_leveraged", "min_price", "dollar_volume"} <= failed

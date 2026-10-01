"""Entry-zone validation: every rule at its boundary, reward:risk and stop distance at the
zone top (with slippage), the in_zone_at_research flag, and `validate-watchlist`."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import RULES, TODAY, candidate, ny, watchlist_data, zone
from lib.watchlist import (distance_to_zone, fill_research_fields, validate_candidate,
                           zone_metrics, zone_warnings)

SLIP = replace(RULES, slippage_pct=0.003)


def errs(rules: Any = RULES, **kw: Any) -> list[str]:
    return validate_candidate(candidate(**kw), rules)


def has(errors: list[str], text: str) -> bool:
    return any(text in e for e in errors)


def test_baseline_candidate_is_valid() -> None:
    assert errs() == []


# ------------------------------------------------------------------ order

@pytest.mark.parametrize("kw", [
    {"stop": 99.0},                                  # stop == low
    {"trigger": zone(100.0, 99.0)},                  # low > high
    {"target": 100.0},                               # high == target
    {"trigger": zone(99.0, 116.0), "target": 115.0},  # high above target
])
def test_order_stop_low_high_target(kw: dict[str, Any]) -> None:
    assert has(errs(**kw), "require stop < zone low <= zone high < target")


def test_low_equal_high_passes_order_but_fails_width() -> None:
    e = errs(trigger=zone(100.0, 100.0), reference_price=101.0)
    assert not has(e, "require stop")
    assert has(e, "below the minimum")


def test_zone_bounds_must_be_positive_numbers() -> None:
    assert has(errs(trigger={"type": "zone", "low": "99", "high": 100.0}), "trigger.low")
    assert has(errs(trigger={"type": "zone", "low": 99.0, "high": None}), "trigger.high")
    assert has(errs(trigger={"type": "zone", "low": -1.0, "high": 100.0}), "trigger.low")


# ------------------------------------------------------------------ width

@pytest.mark.parametrize("low,high,ok", [
    (100.0, 100.5, True),     # exactly 0.5% of low
    (100.0, 100.49, False),   # 0.49%
    (100.0, 103.0, True),     # exactly 3% (R:R at the top is exactly 1.5 too)
    (100.0, 103.01, False),   # 3.01%
])
def test_width_boundaries(low: float, high: float, ok: bool) -> None:
    e = errs(trigger=zone(low, high), reference_price=110.0)
    assert (not has(e, "zone width")) is ok, e


def test_width_is_measured_against_low() -> None:
    # 2.97 / 99 = 3.0% of low (it would be 2.97% of 100).
    assert not has(errs(trigger=zone(99.0, 101.97), target=130.0), "zone width")
    assert has(errs(trigger=zone(99.0, 101.98), target=130.0), "above the maximum")


# ------------------------------------------- reward:risk at the zone top

@pytest.mark.parametrize("target,ok", [(108.25, True), (108.24, False)])
def test_reward_to_risk_at_zone_top_includes_slippage(target: float, ok: bool) -> None:
    # Zone top 100 -> limit 100.30 with 0.3% slippage; stop 95 -> risk 5.30; 1.5R = 108.25.
    e = errs(SLIP, target=target)
    assert (not has(e, "reward:risk at the zone top")) is ok, e


def test_slippage_is_what_fails_it() -> None:
    assert not has(errs(RULES, target=108.24), "reward:risk")     # at 100 flat: 1.65R
    assert has(errs(SLIP, target=108.24), "reward:risk")


@pytest.mark.parametrize("high,ok", [(101.0, True), (101.01, False)])
def test_reward_to_risk_is_checked_at_the_top_not_the_bottom(high: float, ok: bool) -> None:
    # Stop 95, target 110: at the low (99) R:R is 2.75; at 101 it is exactly 1.5.
    e = errs(trigger=zone(99.0, high), target=110.0)
    assert (not has(e, "reward:risk")) is ok, e


def test_zone_metrics_report_the_top_limit() -> None:
    m = zone_metrics(candidate(), SLIP)
    assert m["limit_at_zone_top"] == 100.30
    assert m["reward_to_risk_at_zone_top"] == pytest.approx((115 - 100.30) / (100.30 - 95))
    assert m["stop_distance_at_zone_top"] == pytest.approx((100.30 - 95) / 100.30)
    assert m["zone_width"] == pytest.approx(1 / 99)


# ------------------------------------------------ stop distance at the top

@pytest.mark.parametrize("stop,target,ok", [(88.0, 118.0, True), (87.99, 130.0, False)])
def test_stop_distance_boundary(stop: float, target: float, ok: bool) -> None:
    e = errs(stop=stop, target=target)          # zone top 100, no slippage: max 12% -> 88.00
    assert (not has(e, "stop distance")) is ok, e


def test_stop_distance_uses_the_slipped_limit() -> None:
    # 12% from the zone high itself, but 12.26% from the 100.30 limit enter would send.
    assert not has(errs(RULES, stop=88.0, target=120.0), "stop distance")
    e = errs(SLIP, stop=88.0, target=120.0)
    assert has(e, "stop distance at the zone top")
    assert not has(e, "reward:risk")


# ------------------------------------------------------------ target basis

@pytest.mark.parametrize("basis", ["resistance", "prior_high", "measured_move", "atr_multiple"])
def test_valid_target_bases(basis: str) -> None:
    assert errs(target_basis=basis) == []


@pytest.mark.parametrize("basis", [None, "", "hope", "Resistance"])
def test_invalid_target_basis(basis: Any) -> None:
    assert has(errs(target_basis=basis), "target_basis")


@pytest.mark.parametrize("note,text", [(None, "target_note missing"), ("  ", "target_note missing"),
                                       ("line one\nline two", "single line")])
def test_target_note_required_and_one_line(note: Any, text: str) -> None:
    assert has(errs(target_note=note), text)


# ------------------------------------------------------ in_zone_at_research

@pytest.mark.parametrize("ref,inside", [(99.0, True), (99.5, True), (100.0, True),
                                        (98.99, False), (100.01, False)])
def test_in_zone_flag(ref: float, inside: bool) -> None:
    c = candidate(reference_price=ref)
    assert c["in_zone_at_research"] is inside
    assert validate_candidate(c, RULES) == []
    assert zone_metrics(c, RULES)["in_zone"] is inside


def test_in_zone_flag_must_match_reference_price() -> None:
    assert has(errs(reference_price=99.5, in_zone_at_research=False), "does not match")
    assert has(errs(reference_price=101.0, in_zone_at_research=True), "does not match")


def test_reference_price_and_flag_are_required() -> None:
    c = candidate()
    del c["reference_price"], c["in_zone_at_research"]
    e = validate_candidate(c, RULES)
    assert has(e, "reference_price missing") and has(e, "in_zone_at_research missing")


def test_distance_to_zone_sign() -> None:
    assert distance_to_zone(99.5, 99.0, 100.0) == 0.0
    assert distance_to_zone(98.0, 99.0, 100.0) == pytest.approx(1 / 98)       # must rise
    assert distance_to_zone(101.0, 99.0, 100.0) == pytest.approx(-1 / 101)    # must fall


def test_warnings() -> None:
    assert "enter now" in zone_warnings(candidate(reference_price=99.5), RULES)[0]
    assert "above the zone" in zone_warnings(candidate(reference_price=101.0), RULES)[0]
    assert zone_warnings(candidate(reference_price=98.0), RULES) == []


def test_fill_research_fields() -> None:
    stale = candidate(symbol="abc", reference_price=50.0)       # lowercase symbol, stale price
    data = watchlist_data(candidate(), stale)
    errors = fill_research_fields(data, {"XYZ": 99.5}, "2026-09-28T12:10:00Z")
    xyz, abc = data["candidates"]
    assert data["generated_at"] == "2026-09-28T12:10:00Z"
    assert xyz["reference_price"] == 99.5 and xyz["in_zone_at_research"] is True
    assert abc["symbol"] == "ABC"
    assert "reference_price" not in abc and "in_zone_at_research" not in abc
    assert errors and "ABC" in errors[0]


# ------------------------------------------------- validate-watchlist command

class SnapClient:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls: list[list[str]] = []

    def get_snapshots(self, symbols: list[str], feed: str = "iex") -> dict[str, Any]:
        self.calls.append(list(symbols))
        return {s: {"latestTrade": {"p": self.prices[s]}} for s in symbols if s in self.prices}


def _validate(tmp_path: Path, config: dict[str, Any], data: dict[str, Any],
              prices: dict[str, float]) -> tuple[Any, Any]:
    import trader
    from lib.state import StateStore
    state = StateStore(tmp_path)
    (tmp_path / "watchlist").mkdir(parents=True)
    state.watchlist_path(TODAY).write_text(yaml.safe_dump(data), encoding="utf-8")
    app = trader.App(config=config, state=state, client=SnapClient(prices), now=ny(TODAY, 8, 10, 5))
    return app, (lambda: trader.cmd_validate_watchlist(app, argparse.Namespace()))


def _unvalidated(**kw: Any) -> dict[str, Any]:
    c = candidate(**kw)
    c.pop("reference_price", None)
    c.pop("in_zone_at_research", None)
    return c


def test_validate_watchlist_fills_fields_and_passes(tmp_path: Path, config: dict[str, Any]) -> None:
    data = watchlist_data(_unvalidated())
    data["generated_at"] = "08:1X typed by hand"
    app, run = _validate(tmp_path, config, data, {"XYZ": 99.5})
    out = run()
    assert out["valid"] is True and out["error_count"] == 0
    [row] = out["candidates"]
    assert row["zone"] == {"low": 99.0, "high": 100.0}
    assert row["reference_price"] == 99.5 and row["in_zone_at_research"] is True
    assert row["distance_to_zone_pct"] == 0.0
    assert row["reward_to_risk_at_zone_top"] == 3.0
    assert row["warnings"]                        # inside the zone: "enter now" warning
    saved = yaml.safe_load(app.state.watchlist_path(TODAY).read_text(encoding="utf-8"))
    assert saved["generated_at"] == "2026-09-28T12:10:05Z"      # real clock, not the typed value
    assert saved["candidates"][0]["reference_price"] == 99.5
    assert saved["candidates"][0]["in_zone_at_research"] is True


def test_validate_watchlist_reports_every_error(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    # 5.05% wide and a bad target_basis; target 130 keeps reward:risk at the top (2.89) valid.
    bad = _unvalidated(symbol="BAD", trigger=zone(99.0, 104.0), target=130.0, target_basis="hope")
    data = watchlist_data(_unvalidated(), bad)
    app, run = _validate(tmp_path, config, data, {"XYZ": 101.0, "BAD": 101.0})
    with pytest.raises(trader.CommandError) as info:
        run()
    payload = info.value.payload
    assert payload["valid"] is False and payload["error_count"] == 2
    rows = {r["symbol"]: r for r in payload["candidates"]}
    assert rows["XYZ"]["errors"] == []
    assert has(rows["BAD"]["errors"], "zone width") and has(rows["BAD"]["errors"], "target_basis")
    assert rows["XYZ"]["distance_to_zone_pct"] == pytest.approx(-0.99, abs=0.001)
    # The file is still written, so the next run of the command starts from the filled values.
    saved = yaml.safe_load(app.state.watchlist_path(TODAY).read_text(encoding="utf-8"))
    assert all("reference_price" in c for c in saved["candidates"])


def test_validate_watchlist_symbol_without_price(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    app, run = _validate(tmp_path, config, watchlist_data(_unvalidated(symbol="NOPE")), {})
    with pytest.raises(trader.CommandError) as info:
        run()
    [row] = info.value.payload["candidates"]
    assert has(row["errors"], "no latest trade price for NOPE")
    assert not has(row["errors"], "run `python trader.py validate-watchlist`")
    assert info.value.payload["unpriced_symbols"] == ["NOPE"]


def test_validate_watchlist_empty_list_passes(tmp_path: Path, config: dict[str, Any]) -> None:
    app, run = _validate(tmp_path, config, {"date": TODAY, "candidates": []}, {})
    out = run()
    assert out["valid"] is True and out["candidate_count"] == 0
    assert app.client.calls == []                 # nothing to price, no API call


def test_validate_watchlist_missing_file(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    from lib.state import StateStore
    app = trader.App(config=config, state=StateStore(tmp_path), client=SnapClient({}), now=ny(TODAY, 8, 10))
    with pytest.raises(trader.CommandError):
        trader.cmd_validate_watchlist(app, argparse.Namespace())

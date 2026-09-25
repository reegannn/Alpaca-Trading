"""Position sizing: risk-limited vs cap-limited, whole shares, qty < 1 rejected."""

from __future__ import annotations

from typing import Any

import pytest

from lib.sizing import entry_limit_price, round_to_tick, size_position


def test_risk_limited(config: dict[str, Any]) -> None:
    # risk: 1000 / (100 - 80) = 50 ; cap: 10000 / 100 = 100
    r = size_position(100_000, 100.0, 80.0, config["risk"])
    assert r.qty == 50
    assert r.binding == "risk"


def test_position_cap_limited(config: dict[str, Any]) -> None:
    # risk: 1000 / 1 = 1000 ; cap: 10000 / 100 = 100
    r = size_position(100_000, 100.0, 99.0, config["risk"])
    assert r.qty == 100
    assert r.binding == "position_cap"


def test_whole_shares_floor(config: dict[str, Any]) -> None:
    # risk: 1000 / 3 = 333.3 ; cap: 10000 / 33 = 303.03 -> 303
    r = size_position(100_000, 33.0, 30.0, config["risk"])
    assert r.qty == 303
    assert isinstance(r.qty, int)


def test_qty_below_one_rejected(config: dict[str, Any]) -> None:
    # risk: 10 / 100 = 0.1 ; cap: 100 / 500 = 0.2
    r = size_position(1_000, 500.0, 400.0, config["risk"])
    assert r.qty == 0
    assert not r.ok
    assert r.reason


@pytest.mark.parametrize("stop", [100.0, 101.0])
def test_stop_not_below_limit_rejected(config: dict[str, Any], stop: float) -> None:
    r = size_position(100_000, 100.0, stop, config["risk"])
    assert r.qty == 0 and not r.ok


def test_non_positive_equity_rejected(config: dict[str, Any]) -> None:
    assert not size_position(0, 100.0, 90.0, config["risk"]).ok


def test_exact_integer_not_floored_down(config: dict[str, Any]) -> None:
    # 0.10 * 100000 / 100 = 100 exactly; float noise must not turn it into 99.
    assert size_position(100_000, 100.0, 50.0, {**config["risk"], "risk_per_trade_pct_equity": 1.0}).qty == 100


def test_round_to_tick() -> None:
    assert round_to_tick(100.123) == 100.12
    assert round_to_tick(100.125) == 100.13
    assert round_to_tick(0.123456) == 0.1235


def test_entry_limit_price() -> None:
    assert entry_limit_price(50.0, 0.003) == 50.15
    assert entry_limit_price(123.45, 0.003) == 123.82


def test_size_factor_applied_after_cap(config: dict[str, Any]) -> None:
    # cap-limited 100 shares, then x 0.5
    r = size_position(100_000, 100.0, 99.0, config["risk"], size_factor=0.5)
    assert r.qty == 50 and r.binding == "position_cap" and r.size_factor == 0.5


def test_size_factor_applied_after_risk_limit(config: dict[str, Any]) -> None:
    # risk-limited 50 shares, then x 0.333 -> floor(16.65) = 16
    r = size_position(100_000, 100.0, 80.0, config["risk"], size_factor=0.333)
    assert r.qty == 16 and r.binding == "risk"


def test_size_factor_one_is_unchanged(config: dict[str, Any]) -> None:
    assert size_position(100_000, 100.0, 80.0, config["risk"], size_factor=1.0).qty == 50


@pytest.mark.parametrize("factor", [0.0, -0.1, 1.0001, float("nan"), float("inf"), True])
def test_invalid_size_factor_gives_zero(config: dict[str, Any], factor: Any) -> None:
    r = size_position(100_000, 100.0, 80.0, config["risk"], size_factor=factor)
    assert r.qty == 0 and not r.ok and "size_factor" in (r.reason or "")


def test_size_factor_can_reduce_below_one_share(config: dict[str, Any]) -> None:
    r = size_position(100_000, 100.0, 80.0, config["risk"], size_factor=0.01)   # 50 x 0.01
    assert r.qty == 0 and not r.ok

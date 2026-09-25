"""Risk checks: every check passes and fails at its boundary; all failures are reported."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import (TODAY, candidate, daily_bars, ny, utc, watchlist_data,
                      weekday_calendar_rows)
from lib.market_calendar import TradingCalendar
from lib.risk import (EntryContext, build_bracket_order, check_close, circuit_breaker,
                      count_entries_today, earnings_exit_due, evaluate_entry)
from lib.universe import UniverseResult
from lib.watchlist import validate_watchlist

CAL = TradingCalendar(weekday_calendar_rows(TODAY - timedelta(days=30), TODAY + timedelta(days=90)))


def make_ctx(**overrides: Any) -> EntryContext:
    """A context in which every check passes (limit 100, stop 95, target 115, qty 100)."""
    wl = overrides.pop("watchlist", None) or validate_watchlist(watchlist_data(), TODAY)
    ctx = EntryContext(
        symbol="XYZ",
        now=ny(TODAY, 11, 0),
        today=TODAY,
        clock_is_open=True,
        session=CAL.session(TODAY),
        account={"equity": "100000", "last_equity": "100000"},
        positions=[],
        open_orders=[],
        todays_orders=[],
        watchlist=wl,
        universe=UniverseResult("XYZ", True, {}),
        last_price=100.0,
        open_trades={},
        calendar=CAL,
    )
    return replace(ctx, **overrides)


def checks_failed(decision: Any) -> set[str]:
    return {f["check"] for f in decision.failures}


def position(sym: str, mv: float) -> dict[str, Any]:
    return {"symbol": sym, "qty": "10", "side": "long", "market_value": str(mv)}


def bot_order(sym: str, created: str, status: str = "new", **kw: Any) -> dict[str, Any]:
    o = {"id": f"id-{sym}", "client_order_id": f"sw-20260928-{sym}-abc123", "symbol": sym,
         "side": "buy", "status": status, "qty": "10", "filled_qty": "0",
         "limit_price": "10", "created_at": created}
    o.update(kw)
    return o


# ------------------------------------------------------------------ baseline

def test_baseline_passes(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(), config)
    assert d.passed, d.failures
    assert d.qty == 100
    assert d.limit_price == 100.0
    assert d.sizing is not None and d.sizing.binding == "position_cap"


# --------------------------------------------------------- 1. market hours

@pytest.mark.parametrize("hh,mm,ss,ok", [
    (9, 45, 0, True),     # exactly open + 15m
    (9, 44, 59, False),   # 1s too early
    (15, 45, 0, True),    # exactly close - 15m
    (15, 45, 1, False),   # 1s too late
])
def test_market_hours_boundaries(config: dict[str, Any], hh: int, mm: int, ss: int, ok: bool) -> None:
    d = evaluate_entry(make_ctx(now=ny(TODAY, hh, mm, ss)), config)
    assert ("market_hours" not in checks_failed(d)) is ok


def test_market_closed(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(clock_is_open=False), config)
    assert "market_hours" in checks_failed(d)


# ------------------------------------------------------- 2. circuit breaker

def test_circuit_breaker_boundary(config: dict[str, Any]) -> None:
    at = evaluate_entry(make_ctx(account={"equity": "98000", "last_equity": "100000"}), config)
    assert "circuit_breaker" in checks_failed(at)            # exactly -2% => active
    above = evaluate_entry(make_ctx(account={"equity": "98001", "last_equity": "100000"}), config)
    assert "circuit_breaker" not in checks_failed(above)


def test_circuit_breaker_fails_safe_without_last_equity(config: dict[str, Any]) -> None:
    active, pct = circuit_breaker({"equity": "100000", "last_equity": "0"}, config["risk"])
    assert active and pct is None


# ------------------------------------------------------ 3. daily entry limit

def test_daily_entry_limit_boundary(config: dict[str, Any]) -> None:
    created = utc(ny(TODAY, 10, 0))
    one = [bot_order("AAA", created, status="filled")]
    two = one + [bot_order("BBB", created, status="canceled")]
    assert "daily_entry_limit" not in checks_failed(evaluate_entry(make_ctx(todays_orders=one), config))
    assert "daily_entry_limit" in checks_failed(evaluate_entry(make_ctx(todays_orders=two), config))


def test_entry_count_ignores_other_days_and_non_bot_orders() -> None:
    yesterday = utc(ny(TODAY - timedelta(days=1), 15, 0))
    today = utc(ny(TODAY, 10, 0))
    orders = [
        bot_order("AAA", yesterday),
        {"client_order_id": "manual-1", "created_at": today},
        {"client_order_id": "0b1c-leg", "created_at": today},
        bot_order("BBB", today),
    ]
    assert count_entries_today(orders, TODAY) == 1


# -------------------------------------------------------------- 4. watchlist

def test_not_in_watchlist(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(symbol="ABC"), config)
    assert "watchlist" in checks_failed(d)


def test_watchlist_wrong_date(config: dict[str, Any]) -> None:
    wl = validate_watchlist(watchlist_data(day=TODAY - timedelta(days=1)), TODAY)
    d = evaluate_entry(make_ctx(watchlist=wl), config)
    assert "watchlist" in checks_failed(d)


def test_watchlist_invalid_entry(config: dict[str, Any]) -> None:
    wl = validate_watchlist(watchlist_data(candidate(sources=[])), TODAY)
    d = evaluate_entry(make_ctx(watchlist=wl), config)
    assert "watchlist" in checks_failed(d)


# --------------------------------------------------------------- 5. universe

def test_universe_failure(config: dict[str, Any]) -> None:
    u = UniverseResult("XYZ", False, {"exchange": {"pass": False, "detail": "exchange=OTC"}})
    d = evaluate_entry(make_ctx(universe=u), config)
    assert "universe" in checks_failed(d)


# ------------------------------------------------- 6. existing position/order

def test_existing_position(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(positions=[position("XYZ", 1000)]), config)
    assert "existing_position" in checks_failed(d)


def test_existing_open_order(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(open_orders=[bot_order("XYZ", utc(ny(TODAY, 10, 0)))]), config)
    assert "existing_order" in checks_failed(d)


# ------------------------------------------------------- 7. max positions

def test_max_open_positions_boundary(config: dict[str, Any]) -> None:
    seven = [position(f"P{i}", 100) for i in range(7)]
    eight = seven + [position("P7", 100)]
    assert "max_open_positions" not in checks_failed(evaluate_entry(make_ctx(positions=seven), config))
    assert "max_open_positions" in checks_failed(evaluate_entry(make_ctx(positions=eight), config))


def test_pending_entries_count_toward_positions(config: dict[str, Any]) -> None:
    seven = [position(f"P{i}", 100) for i in range(7)]
    pending = [bot_order("PEND", utc(ny(TODAY, 10, 0)))]
    d = evaluate_entry(make_ctx(positions=seven, open_orders=pending), config)
    assert "max_open_positions" in checks_failed(d)


# --------------------------------------------------------- 8. gross exposure

def test_gross_exposure_boundary(config: dict[str, Any]) -> None:
    # New entry notional is exactly 10,000 (100 shares x $100); max gross = 100,000.
    ok = evaluate_entry(make_ctx(positions=[position("AAA", 90000)]), config)
    assert "gross_exposure" not in checks_failed(ok)
    over = evaluate_entry(make_ctx(positions=[position("AAA", 90000.01)]), config)
    assert "gross_exposure" in checks_failed(over)


# -------------------------------------------------------- 9. sector exposure

def test_sector_exposure_boundary(config: dict[str, Any]) -> None:
    trades = {"sw-x": {"symbol": "TECH1", "sector": "Technology"}}
    ok = evaluate_entry(make_ctx(positions=[position("TECH1", 20000)], open_trades=trades), config)
    assert "sector_exposure" not in checks_failed(ok)
    over = evaluate_entry(make_ctx(positions=[position("TECH1", 20000.01)], open_trades=trades), config)
    assert "sector_exposure" in checks_failed(over)


def test_other_sector_not_counted(config: dict[str, Any]) -> None:
    trades = {"sw-x": {"symbol": "BANK", "sector": "Financials"}}
    d = evaluate_entry(make_ctx(positions=[position("BANK", 25000)], open_trades=trades), config)
    assert "sector_exposure" not in checks_failed(d)


# --------------------------------------------------------------- 10. earnings

@pytest.mark.parametrize("earnings,ok", [
    (date(2026, 10, 2), True),    # 4 trading days away (> 3)
    (date(2026, 10, 1), False),   # 3 trading days away (not > 3)
    (date(2026, 9, 25), False),   # in the past
    ("unknown", False),
    ("n/a", False),               # n/a is ETF-only
])
def test_earnings_stock(config: dict[str, Any], earnings: Any, ok: bool) -> None:
    wl = validate_watchlist(watchlist_data(candidate(earnings_date=earnings)), TODAY)
    d = evaluate_entry(make_ctx(watchlist=wl), config)
    assert ("earnings" not in checks_failed(d)) is ok


def test_earnings_etf_na_allowed(config: dict[str, Any]) -> None:
    wl = validate_watchlist(watchlist_data(candidate(asset_type="etf", earnings_date="n/a")), TODAY)
    d = evaluate_entry(make_ctx(watchlist=wl), config)
    assert d.passed, d.failures


# ----------------------------------------------------------------- 11. prices

def test_stop_distance_boundary(config: dict[str, Any]) -> None:
    ok_wl = validate_watchlist(watchlist_data(candidate(stop=88.0, target=118.0)), TODAY)
    assert "prices" not in checks_failed(evaluate_entry(make_ctx(watchlist=ok_wl), config))
    bad_wl = validate_watchlist(watchlist_data(candidate(stop=87.99, target=130.0)), TODAY)
    assert "prices" in checks_failed(evaluate_entry(make_ctx(watchlist=bad_wl), config))


def test_reward_to_risk_boundary(config: dict[str, Any]) -> None:
    ok_wl = validate_watchlist(watchlist_data(candidate(target=107.5)), TODAY)   # R:R exactly 1.5
    assert "prices" not in checks_failed(evaluate_entry(make_ctx(watchlist=ok_wl), config))
    bad_wl = validate_watchlist(watchlist_data(candidate(target=107.49)), TODAY)
    assert "prices" in checks_failed(evaluate_entry(make_ctx(watchlist=bad_wl), config))


def test_stop_not_below_limit(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(last_price=94.0), config)   # limit 94 < stop 95
    assert "prices" in checks_failed(d)


def test_no_last_price(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(last_price=None), config)
    assert {"prices", "trigger"} <= checks_failed(d)


# ---------------------------------------------------------------- 12. trigger

def test_trigger_above_boundary(config: dict[str, Any]) -> None:
    assert "trigger" not in checks_failed(evaluate_entry(make_ctx(last_price=100.0), config))
    assert "trigger" in checks_failed(evaluate_entry(make_ctx(last_price=99.99), config))


def test_trigger_below_boundary(config: dict[str, Any]) -> None:
    wl = validate_watchlist(watchlist_data(candidate(trigger={"type": "below", "price": 100.0})), TODAY)
    assert "trigger" not in checks_failed(evaluate_entry(make_ctx(watchlist=wl, last_price=100.0), config))
    assert "trigger" in checks_failed(evaluate_entry(make_ctx(watchlist=wl, last_price=100.01), config))


# ---------------------------------------------------------------- sizing/other

def test_sizing_below_one_share_refused(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(account={"equity": "500", "last_equity": "500"}), config)
    assert "sizing" in checks_failed(d)


def test_slippage_applied_to_limit(config: dict[str, Any]) -> None:
    config["risk"]["entry_limit_slippage_pct"] = 0.003
    d = evaluate_entry(make_ctx(last_price=100.0), config)
    assert d.limit_price == 100.30


def test_long_only_enforced(config: dict[str, Any]) -> None:
    config["risk"]["long_only"] = False
    assert "long_only" in checks_failed(evaluate_entry(make_ctx(), config))


def test_all_failures_reported(config: dict[str, Any]) -> None:
    ctx = make_ctx(
        clock_is_open=False,
        account={"equity": "97000", "last_equity": "100000"},
        todays_orders=[bot_order("A", utc(ny(TODAY, 10, 0))), bot_order("B", utc(ny(TODAY, 10, 1)))],
        positions=[position("XYZ", 1000)] + [position(f"P{i}", 100) for i in range(8)],
        last_price=99.0,
    )
    failed = checks_failed(evaluate_entry(ctx, config))
    assert {"market_hours", "circuit_breaker", "daily_entry_limit", "existing_position",
            "max_open_positions", "trigger"} <= failed


# ------------------------------------------------------------ size factor

def test_size_factor_scales_after_caps(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(size_factor=0.5), config)
    assert d.passed, d.failures
    assert d.qty == 50                      # cap-limited 100 shares x 0.5
    assert d.metrics["notional"] == 5000.0  # exposure checks use the reduced size


@pytest.mark.parametrize("factor", [0.0, -0.5, 1.01, 2.0, float("nan"), float("inf")])
def test_invalid_size_factor_refused(config: dict[str, Any], factor: float) -> None:
    d = evaluate_entry(make_ctx(size_factor=factor), config)
    assert "size_factor" in checks_failed(d)
    assert not d.passed


def test_size_factor_below_one_share_refused(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(size_factor=0.001), config)   # floor(100 x 0.001) = 0
    assert "sizing" in checks_failed(d)


# ------------------------------------------------------------ earnings exit

@pytest.mark.parametrize("today,earnings,due", [
    (TODAY, date(2026, 9, 29), True),          # Monday; earnings on the next trading day
    (TODAY, date(2026, 9, 28), True),          # earnings today
    (TODAY, date(2026, 9, 25), True),          # already past
    (TODAY, date(2026, 9, 30), False),         # two trading days away
    (date(2026, 10, 2), date(2026, 10, 5), True),   # Friday -> Monday
    (date(2026, 10, 2), date(2026, 10, 6), False),
    (TODAY, "2026-09-29", True),               # stored as a string in open_trades.json
    (TODAY, "unknown", False),
    (TODAY, "n/a", False),
    (TODAY, None, False),
])
def test_earnings_exit_due(today: date, earnings: Any, due: bool) -> None:
    assert earnings_exit_due(earnings, today, CAL) is due


# ------------------------------------------------------------ order + close

def test_bracket_order_shape(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(), config)
    order = build_bracket_order(d, "sw-20260928-XYZ-abcdef")
    assert order["order_class"] == "bracket"
    assert order["time_in_force"] == "gtc"
    assert order["side"] == "buy" and order["type"] == "limit"
    assert order["qty"] == "100" and "." not in order["qty"]
    assert order["stop_loss"] == {"stop_price": "95.00"}
    assert order["take_profit"] == {"limit_price": "115.00"}


def test_bracket_order_refused_for_failed_decision(config: dict[str, Any]) -> None:
    d = evaluate_entry(make_ctx(clock_is_open=False), config)
    with pytest.raises(ValueError):
        build_bracket_order(d, "sw-x")


def test_check_close() -> None:
    pos = {"symbol": "XYZ", "qty": "10", "side": "long"}
    assert check_close("XYZ", pos, True) == []
    assert check_close("XYZ", pos, False)           # market closed
    assert check_close("XYZ", None, True)           # no position


# --------------------------------------------- trader.py enter (mocked API)

class FakeClient:
    """Mocked Alpaca client for trader.cmd_enter."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def get_clock(self) -> dict[str, Any]:
        return {"is_open": True}

    def get_account(self) -> dict[str, Any]:
        return {"equity": "100000", "last_equity": "100000"}

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def list_orders(self, **kw: Any) -> list[dict[str, Any]]:
        return []

    def get_asset(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "class": "us_equity", "status": "active", "tradable": True,
                "exchange": "NASDAQ", "name": "XYZ Corp"}

    def get_calendar(self, start: str, end: str) -> list[dict[str, str]]:
        return weekday_calendar_rows(date.fromisoformat(start), date.fromisoformat(end))

    def get_daily_bars(self, symbols: list[str], **kw: Any) -> dict[str, list[dict[str, Any]]]:
        return {s: daily_bars(40, 100.0, 1_000_000) for s in symbols}

    def get_snapshots(self, symbols: list[str], feed: str = "iex") -> dict[str, Any]:
        return {s: {"latestTrade": {"p": 100.0}} for s in symbols}

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(order)
        return {"id": "order-1", **order}


def _app(tmp_path: Path, config: dict[str, Any], client: FakeClient) -> Any:
    import trader
    from lib.state import StateStore
    state = StateStore(tmp_path)
    (tmp_path / "watchlist").mkdir(parents=True)
    state.watchlist_path(TODAY).write_text(yaml.safe_dump(watchlist_data()), encoding="utf-8")
    state.save_open_trades({})
    return trader.App(config=config, state=state, client=client, now=ny(TODAY, 11, 0))


def test_dry_run_never_submits(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    client = FakeClient()
    app = _app(tmp_path, config, client)
    out = trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="trigger met", dry_run=True, size_factor=1.0))
    assert out["would_submit"] is True
    assert client.submitted == []
    assert app.state.load_open_trades() == {}


def test_enter_submits_once_and_records(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    client = FakeClient()
    app = _app(tmp_path, config, client)
    trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="trigger met", dry_run=False, size_factor=1.0))
    assert len(client.submitted) == 1
    order = client.submitted[0]
    assert order["client_order_id"].startswith("sw-20260928-XYZ-")
    rec = app.state.load_open_trades()[order["client_order_id"]]
    assert rec["sector"] == "Technology" and rec["qty"] == 100


def test_enter_refused_does_not_submit(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    client = FakeClient()
    app = _app(tmp_path, config, client)
    app.now = ny(TODAY, 9, 35)   # inside the opening buffer
    with pytest.raises(trader.Refused):
        trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="x", dry_run=False, size_factor=1.0))
    assert client.submitted == []


def test_submit_error_not_retried(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader

    class Failing(FakeClient):
        def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
            self.submitted.append(order)
            raise RuntimeError("HTTP 500")

    client = Failing()
    app = _app(tmp_path, config, client)
    with pytest.raises(trader.CommandError):
        trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="x", dry_run=False, size_factor=1.0))
    assert len(client.submitted) == 1
    assert app.state.load_open_trades() == {}


def test_enter_with_size_factor_records_it(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    client = FakeClient()
    app = _app(tmp_path, config, client)
    trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="L-001 applies",
                                             dry_run=False, size_factor=0.5))
    order = client.submitted[0]
    assert order["qty"] == "50"
    assert app.state.load_open_trades()[order["client_order_id"]]["size_factor"] == 0.5


@pytest.mark.parametrize("factor", [0.0, 1.5, float("nan")])
def test_enter_rejects_invalid_size_factor(tmp_path: Path, config: dict[str, Any], factor: float) -> None:
    import trader
    client = FakeClient()
    app = _app(tmp_path, config, client)
    with pytest.raises(trader.CommandError):
        trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="x",
                                                 dry_run=False, size_factor=factor))
    assert client.submitted == []


@pytest.mark.parametrize("value", ["0", "-1", "1.2", "nan", "abc"])
def test_cli_rejects_invalid_size_factor(value: str) -> None:
    import trader
    with pytest.raises(trader.UsageError):
        trader.build_parser().parse_args(["enter", "XYZ", "--rationale", "x", "--size-factor", value])


def test_cli_accepts_size_factor() -> None:
    import trader
    args = trader.build_parser().parse_args(["enter", "XYZ", "--rationale", "x", "--size-factor", "0.5"])
    assert args.size_factor == 0.5
    assert trader.build_parser().parse_args(["enter", "XYZ", "--rationale", "x"]).size_factor == 1.0


def test_earnings_exit_is_a_close_reason() -> None:
    import trader
    args = trader.build_parser().parse_args(["close", "XYZ", "--reason", "earnings_exit"])
    assert args.reason == "earnings_exit"

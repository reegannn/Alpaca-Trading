"""Fractional order mode: sizing, min notional, cash-account buying power, universe
fractionable check, `protect` planning/execution, and fill-based exit detection."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import TODAY, daily_bars, ny, utc, watchlist_data, weekday_calendar_rows
from lib.exits import classify_exit, exit_fills, vwap
from lib.market_calendar import TradingCalendar, parse_ts
from lib.protect import execute_protection, plan_protection
from lib.risk import (EntryContext, build_entry_order, evaluate_entry, settled_cash_available,
                      unsettled_sell_proceeds)
from lib.sizing import format_qty, size_position
from lib.universe import UniverseResult, check_universe
from lib.watchlist import validate_watchlist

CAL = TradingCalendar(weekday_calendar_rows(TODAY - timedelta(days=150), TODAY + timedelta(days=90)))


@pytest.fixture
def fconfig(config: dict[str, Any]) -> dict[str, Any]:
    config["orders"] = {"mode": "fractional"}
    return config


# ------------------------------------------------------------------ sizing

def test_fractional_sizing_no_flooring(fconfig: dict[str, Any]) -> None:
    risk = {**fconfig["risk"], "max_position_pct_equity": 0.35}
    # risk: 1 / 2.5 = 0.4 sh ; cap: 35 / 50 = 0.7 sh
    r = size_position(100, 50.0, 47.5, risk, fractional=True)
    assert r.ok and r.qty == 0.4 and r.binding == "risk"
    assert r.notional == pytest.approx(20.0)


def test_fractional_qty_rounds_down_to_9_decimals(fconfig: dict[str, Any]) -> None:
    risk = {**fconfig["risk"], "max_position_pct_equity": 0.35}
    r = size_position(100, 30.0, 27.0, risk, fractional=True)   # 1 / 3 shares
    assert r.qty == 0.333333333
    assert format_qty(r.qty) == "0.333333333"


def test_min_order_notional_rejects_small_orders(fconfig: dict[str, Any]) -> None:
    risk = {**fconfig["risk"], "max_position_pct_equity": 0.35}
    # risk: 0.6 / 20 = 0.03 sh -> $3 notional < $5
    r = size_position(60, 100.0, 80.0, risk, fractional=True)
    assert not r.ok and "min_order_notional" in (r.reason or "")


def test_min_order_notional_boundary(fconfig: dict[str, Any]) -> None:
    risk = {**fconfig["risk"], "max_position_pct_equity": 0.35}
    r = size_position(100, 50.0, 40.0, risk, fractional=True)   # 0.1 sh x $50 = $5.00
    assert r.ok and r.notional == pytest.approx(5.0)


def test_bracket_mode_still_whole_shares(fconfig: dict[str, Any]) -> None:
    risk = {**fconfig["risk"], "max_position_pct_equity": 0.35}
    r = size_position(100, 50.0, 47.5, risk, fractional=False)   # 0.4 sh -> 0 whole shares
    assert not r.ok and r.qty == 0


@pytest.mark.parametrize("value,text", [
    (0.1234567891, "0.123456789"), (2.0, "2"), (1e-10, "0"), ("0.400000000", "0.4"), (10, "10"),
])
def test_format_qty(value: Any, text: str) -> None:
    assert format_qty(value) == text


# ------------------------------------------------------------- entry + order

def small_ctx(**overrides: Any) -> EntryContext:
    """$100 account; XYZ at $100, stop 95, target 115 -> 0.1 sh (position cap), $10 notional."""
    ctx = EntryContext(
        symbol="XYZ", now=ny(TODAY, 11, 0), today=TODAY, clock_is_open=True,
        session=CAL.session(TODAY),
        account={"equity": "100", "last_equity": "100", "cash": "100"},
        positions=[], open_orders=[], todays_orders=[],
        watchlist=validate_watchlist(watchlist_data(), TODAY),
        universe=UniverseResult("XYZ", True, {}), last_price=100.0, open_trades={}, calendar=CAL,
    )
    return replace(ctx, **overrides)


def test_fractional_entry_passes_and_builds_day_limit(fconfig: dict[str, Any]) -> None:
    d = evaluate_entry(small_ctx(), fconfig)
    assert d.passed, d.failures
    assert d.qty == 0.1
    order = build_entry_order(d, "sw-20260928-XYZ-abcdef", "fractional")
    assert order == {"symbol": "XYZ", "qty": "0.1", "side": "buy", "type": "limit",
                     "time_in_force": "day", "limit_price": "100.00",
                     "client_order_id": "sw-20260928-XYZ-abcdef"}
    assert "order_class" not in order


def test_fractional_entry_below_min_notional_refused(fconfig: dict[str, Any]) -> None:
    fconfig["risk"]["min_order_notional"] = 10.01
    d = evaluate_entry(small_ctx(), fconfig)
    assert "sizing" in {f["check"] for f in d.failures}


# ------------------------------------------------------------ cash account

def fill(side: str, qty: float, price: float, when: Any, order_id: str = "o", sym: str = "AAA") -> dict[str, Any]:
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": utc(when), "order_id": order_id}


def test_unsettled_proceeds_only_counts_todays_sells() -> None:
    fills = [
        fill("sell", 5, 10.0, ny(TODAY, 10, 0)),                         # today: counts (50)
        fill("sell", 3, 10.0, ny(TODAY - timedelta(days=3), 15, 0)),     # settled: ignored
        fill("buy", 2, 10.0, ny(TODAY, 10, 30)),                         # buy: ignored
    ]
    assert unsettled_sell_proceeds(fills, TODAY) == pytest.approx(50.0)


def test_settled_cash_subtracts_same_day_sells_and_pending_entries() -> None:
    fills = [fill("sell", 5, 10.0, ny(TODAY, 10, 0))]
    pending = [{"client_order_id": "sw-20260928-BBB-abc123", "symbol": "BBB", "side": "buy",
                "status": "new", "qty": "1", "filled_qty": "0", "limit_price": "10"}]
    assert settled_cash_available({"cash": "100"}, fills, pending, TODAY) == pytest.approx(40.0)


def test_cash_account_blocks_entry_funded_by_same_day_sale(fconfig: dict[str, Any]) -> None:
    fconfig["risk"]["simulate_cash_account"] = True
    # $100 cash but $95 of it came from a sale today -> only $5 settled; entry needs $10.
    fills = [fill("sell", 1, 95.0, ny(TODAY, 10, 0))]
    d = evaluate_entry(small_ctx(fills=fills), fconfig)
    assert "cash_account" in {f["check"] for f in d.failures}
    # Without the same-day sale the same entry is fine.
    assert evaluate_entry(small_ctx(), fconfig).passed


# ------------------------------------------------------------------ universe

def _asset(**kw: Any) -> dict[str, Any]:
    a = {"symbol": "XYZ", "class": "us_equity", "status": "active", "tradable": True,
         "exchange": "NASDAQ", "name": "XYZ Holdings Inc. Common Stock", "fractionable": True}
    a.update(kw)
    return a


@pytest.mark.parametrize("fractionable", [False, None])
def test_universe_rejects_non_fractionable_in_fractional_mode(config: dict[str, Any], fractionable: Any) -> None:
    bars = daily_bars(30, 50.0, 1_000_000)
    r = check_universe("XYZ", _asset(fractionable=fractionable), bars, config["universe"],
                       require_fractionable=True)
    assert not r.eligible and not r.checks["fractionable"]["pass"]
    # Bracket mode does not require it.
    assert check_universe("XYZ", _asset(fractionable=fractionable), bars, config["universe"]).eligible


def test_universe_accepts_fractionable(config: dict[str, Any]) -> None:
    r = check_universe("XYZ", _asset(), daily_bars(30, 50.0, 1_000_000), config["universe"],
                       require_fractionable=True)
    assert r.eligible and r.checks["fractionable"]["pass"]


# ------------------------------------------------------------------ protect

def pos(sym: str = "XYZ", qty: str = "0.4", price: str = "50") -> dict[str, Any]:
    return {"symbol": sym, "qty": qty, "side": "long", "current_price": price, "market_value": "20"}


def rec(sym: str = "XYZ", stop: float = 47.5, **kw: Any) -> dict[str, Any]:
    r = {"symbol": sym, "stop": stop, "target": 60.0, "order_mode": "fractional",
         "submitted_at": "2026-09-21T14:00:00Z", "stop_order_id": None, "stop_order_ids": []}
    r.update(kw)
    return r


def stop_o(oid: str, qty: str = "0.4", stop: str = "47.50", sym: str = "XYZ") -> dict[str, Any]:
    return {"id": oid, "symbol": sym, "side": "sell", "type": "stop", "qty": qty,
            "stop_price": stop, "status": "accepted", "time_in_force": "day"}


TRADES = lambda: {"sw-20260921-XYZ-aaaaaa": rec()}  # noqa: E731


def test_plan_create_when_missing() -> None:
    [a] = plan_protection([pos()], [], TRADES())
    assert a.action == "create" and a.qty == "0.4" and a.stop_price == 47.5
    assert a.record_id == "sw-20260921-XYZ-aaaaaa"


def test_plan_noop_when_correct() -> None:
    [a] = plan_protection([pos()], [stop_o("s1")], TRADES())
    assert a.action == "ok" and a.keep_order_id == "s1" and a.cancel_order_ids == []


@pytest.mark.parametrize("order", [stop_o("s1", qty="0.3"), stop_o("s1", stop="46.00")])
def test_plan_replace_wrong_qty_or_price(order: dict[str, Any]) -> None:
    [a] = plan_protection([pos()], [order], TRADES())
    assert a.action == "replace" and a.cancel_order_ids == ["s1"]


def test_plan_dedupes_duplicate_stops() -> None:
    [a] = plan_protection([pos()], [stop_o("s1"), stop_o("s2"), stop_o("s3", stop="40.00")], TRADES())
    assert a.action == "dedupe" and a.keep_order_id == "s1"
    assert sorted(a.cancel_order_ids) == ["s2", "s3"]


def test_plan_flags() -> None:
    market_sell = {"id": "m1", "symbol": "XYZ", "side": "sell", "type": "market", "qty": "0.4"}
    assert plan_protection([pos()], [], {})[0].flag == "untracked"
    assert plan_protection([pos()], [market_sell], TRADES())[0].flag == "pending_sell"
    assert plan_protection([pos(price="47.40")], [], TRADES())[0].flag == "breached"
    assert plan_protection([pos()], [], {"k": rec(stop=None)})[0].flag == "no_stop_recorded"


class Broker:
    """Stateful fake: open orders, cancels and submits."""

    def __init__(self, orders: list[dict[str, Any]] | None = None, honour_cancel: bool = True,
                 fail_submit: bool = False) -> None:
        self.orders = [dict(o) for o in (orders or [])]
        self.honour_cancel = honour_cancel
        self.fail_submit = fail_submit
        self.submits: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self._n = 0

    def list_orders(self, status: str = "open", symbols: list[str] | None = None, **_: Any) -> list[dict[str, Any]]:
        out = [o for o in self.orders if o["status"] in ("new", "accepted")]
        if symbols:
            out = [o for o in out if o["symbol"] in symbols]
        return [dict(o) for o in out]

    def cancel_order(self, oid: str) -> None:
        self.cancels.append(oid)
        if self.honour_cancel:
            for o in self.orders:
                if o["id"] == oid:
                    o["status"] = "canceled"

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.submits.append(order)
        if self.fail_submit:
            raise RuntimeError("HTTP 500")
        self._n += 1
        o = {**order, "id": f"new{self._n}", "status": "accepted"}
        self.orders.append(o)
        return dict(o)

    def open_stops(self, sym: str = "XYZ") -> list[dict[str, Any]]:
        return [o for o in self.list_orders(symbols=[sym]) if o["side"] == "sell"]


def run_protect(broker: Broker, trades: dict[str, dict[str, Any]], positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    plan = plan_protection(positions or [pos()], broker.list_orders(), trades)
    return execute_protection(broker, plan, trades, TODAY, sleep=lambda _s: None, wait_seconds=0)


def test_protect_creates_then_noops_never_duplicates() -> None:
    broker, trades = Broker(), TRADES()
    out1 = run_protect(broker, trades)
    assert out1["errors"] == [] and len(broker.submits) == 1
    order = broker.submits[0]
    assert order["type"] == "stop" and order["side"] == "sell" and order["time_in_force"] == "day"
    assert order["qty"] == "0.4" and order["stop_price"] == "47.50"
    assert order["client_order_id"].startswith("sl-")
    r = trades["sw-20260921-XYZ-aaaaaa"]
    assert r["stop_order_id"] == "new1" and r["stop_order_ids"] == ["new1"]

    out2 = run_protect(broker, trades)                     # second run: nothing to do
    assert out2["results"][0]["action"] == "ok"
    assert len(broker.submits) == 1 and len(broker.open_stops()) == 1


def test_protect_replaces_wrong_stop_and_keeps_history() -> None:
    broker = Broker([stop_o("old", qty="0.3")])
    trades = {"sw-20260921-XYZ-aaaaaa": rec(stop_order_id="old", stop_order_ids=["old"])}
    out = run_protect(broker, trades)
    assert out["errors"] == []
    assert broker.cancels == ["old"]
    stops = broker.open_stops()
    assert len(stops) == 1 and stops[0]["qty"] == "0.4" and stops[0]["id"] == "new1"
    assert trades["sw-20260921-XYZ-aaaaaa"]["stop_order_ids"] == ["old", "new1"]


def test_protect_does_not_create_when_cancel_unconfirmed() -> None:
    broker = Broker([stop_o("old", stop="40.00")], honour_cancel=False)
    out = run_protect(broker, TRADES())
    assert len(out["errors"]) == 1
    assert broker.submits == []                            # no second stop
    assert [o["id"] for o in broker.open_stops()] == ["old"]


def test_protect_removes_duplicates_without_submitting() -> None:
    broker = Broker([stop_o("s1"), stop_o("s2")])
    out = run_protect(broker, TRADES())
    assert out["errors"] == [] and broker.submits == []
    assert [o["id"] for o in broker.open_stops()] == ["s1"]


def test_protect_submit_error_not_retried() -> None:
    broker = Broker(fail_submit=True)
    out = run_protect(broker, TRADES())
    assert len(out["errors"]) == 1 and len(broker.submits) == 1


def test_protect_leaves_untracked_positions_alone() -> None:
    broker = Broker()
    out = run_protect(broker, {}, [pos("ZZZ")])
    assert out["results"][0]["flag"] == "untracked" and broker.submits == []


# ------------------------------------------------------------ exits/reconcile

ENTRY_TIME = ny(TODAY - timedelta(days=5), 10, 0)


def test_stop_out_detected_across_replaced_stops() -> None:
    record = rec(stop_order_id="s3", stop_order_ids=["s1", "s2", "s3"])
    fills = [
        fill("buy", 0.4, 50.0, ENTRY_TIME, order_id="entry", sym="XYZ"),
        fill("sell", 0.4, 47.4, ny(TODAY - timedelta(days=2), 11, 0), order_id="s2", sym="XYZ"),
    ]
    used = exit_fills(fills, "XYZ", ENTRY_TIME, 0.4)
    assert len(used) == 1
    assert classify_exit(used, record) == "stop"
    price, when = vwap(used)
    assert price == pytest.approx(47.4) and parse_ts(when).date() == TODAY - timedelta(days=2)


def test_close_order_uses_recorded_reason() -> None:
    record = rec(stop_order_ids=["s1"], close_order_id="c1", close_reason="target")
    used = exit_fills([fill("sell", 0.4, 61.0, ny(TODAY, 11, 0), order_id="c1", sym="XYZ")],
                      "XYZ", ENTRY_TIME, 0.4)
    assert classify_exit(used, record) == "target"


def test_unrecorded_stop_order_detected_by_type() -> None:
    used = exit_fills([fill("sell", 0.4, 47.0, ny(TODAY, 11, 0), order_id="zz", sym="XYZ")],
                      "XYZ", ENTRY_TIME, 0.4)
    assert classify_exit(used, rec(), {"zz": "stop"}) == "stop"
    assert classify_exit(used, rec(), {"zz": "market"}) == "unknown"


def test_partial_exit_not_treated_as_exited() -> None:
    fills = [fill("sell", 0.2, 47.0, ny(TODAY, 11, 0), order_id="s1", sym="XYZ")]
    assert exit_fills(fills, "XYZ", ENTRY_TIME, 0.4) == []


class ReconcileClient:
    """Entry filled 5 days ago; stop re-placed daily (s1..s3); s2 filled."""

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def get_calendar(self, start: str, end: str) -> list[dict[str, str]]:
        return weekday_calendar_rows(date.fromisoformat(start), date.fromisoformat(end))

    def get_fill_activities(self, after: str | None = None) -> list[dict[str, Any]]:
        return [fill("buy", 0.4, 50.0, ENTRY_TIME, order_id="e1", sym="XYZ"),
                fill("sell", 0.4, 47.4, ny(TODAY - timedelta(days=2), 11, 0), order_id="s2", sym="XYZ")]

    def get_order_by_client_id(self, cid: str) -> dict[str, Any]:
        return {"id": "e1"}

    def get_order(self, oid: str, nested: bool = True) -> dict[str, Any]:
        assert oid == "e1", "known stop ids must not need a lookup"
        return {"id": "e1", "status": "filled", "filled_qty": "0.4", "filled_avg_price": "50",
                "filled_at": utc(ENTRY_TIME), "legs": None}


def test_reconcile_records_stop_out_in_fractional_mode(tmp_path: Path, fconfig: dict[str, Any]) -> None:
    import trader
    from lib.state import StateStore
    state = StateStore(tmp_path)
    state.save_open_trades({"sw-20260921-XYZ-aaaaaa": rec(
        submitted_at=utc(ENTRY_TIME), stop_order_id="s3", stop_order_ids=["s1", "s2", "s3"],
        setup_tag="breakout", idea_source="screener", sector="Technology")})
    app = trader.App(config=fconfig, state=state, client=ReconcileClient(), now=ny(TODAY, 16, 20))
    out = trader.cmd_reconcile(app, argparse.Namespace())
    [row] = out["closed_trades"]
    assert row["exit_reason"] == "stop"
    assert row["exit_price"] == pytest.approx(47.4)
    assert row["r_multiple"] == pytest.approx((47.4 - 50.0) / (50.0 - 47.5), abs=1e-3)
    assert state.load_open_trades() == {}
    assert state.read_trades()[0]["exit_reason"] == "stop"


# ------------------------------------------------- protect: open buy / breach

def test_protect_skips_symbol_with_open_buy() -> None:
    buy = {"id": "b1", "symbol": "XYZ", "side": "buy", "type": "limit", "qty": "0.2", "status": "new"}
    broker = Broker([buy])
    out = run_protect(broker, TRADES())
    [r] = out["results"]
    assert r["flag"] == "open_buy" and r["reason"] == "skipped: open buy order"
    assert broker.submits == [] and broker.cancels == []


def test_breached_position_flagged_not_stopped() -> None:
    from lib.protect import unprotected_summary
    broker = Broker()
    out = run_protect(broker, TRADES(), [pos(price="47.00")])
    [r] = out["results"]
    assert r["action"] == "flag" and r["flag"] == "breached"
    assert broker.submits == []
    items, warning = unprotected_summary(out["results"])
    assert items == [{"symbol": "XYZ", "reason": "breached"}]
    assert warning == "⚠️ UNPROTECTED: XYZ (breached)"


def test_no_warning_when_all_protected() -> None:
    from lib.protect import unprotected_summary
    out = run_protect(Broker(), TRADES())
    assert unprotected_summary(out["results"]) == ([], None)


# ------------------------------------------------ fill-or-cancel `enter`

class EntryBroker:
    """Fake Alpaca for trader.cmd_enter in fractional mode.

    fill: "full" | "partial" | "none" — how the entry limit buy behaves.
    stop_failures: how many stop submissions fail before one succeeds.
    """

    def __init__(self, fill: str = "full", stop_failures: int = 0) -> None:
        self.fill = fill
        self.stop_failures = stop_failures
        self.orders: dict[str, dict[str, Any]] = {}
        self.submits: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self._n = 0

    # --- reads used by enter's risk checks
    def get_clock(self) -> dict[str, Any]:
        return {"is_open": True}

    def get_account(self) -> dict[str, Any]:
        return {"equity": "100", "last_equity": "100", "cash": "100"}

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def get_asset(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "class": "us_equity", "status": "active", "tradable": True,
                "exchange": "NASDAQ", "name": "XYZ Corp", "fractionable": True}

    def get_calendar(self, start: str, end: str) -> list[dict[str, str]]:
        return weekday_calendar_rows(date.fromisoformat(start), date.fromisoformat(end))

    def get_daily_bars(self, symbols: list[str], **kw: Any) -> dict[str, list[dict[str, Any]]]:
        return {s: daily_bars(40, 100.0, 1_000_000) for s in symbols}

    def get_snapshots(self, symbols: list[str], feed: str = "iex") -> dict[str, Any]:
        return {s: {"latestTrade": {"p": 100.0}} for s in symbols}

    def get_fill_activities(self, after: str | None = None) -> list[dict[str, Any]]:
        return []

    # --- orders
    def list_orders(self, status: str = "open", symbols: list[str] | None = None, **_: Any) -> list[dict[str, Any]]:
        out = list(self.orders.values())
        if status == "open":
            out = [o for o in out if o["status"] in ("new", "accepted", "partially_filled")]
        if symbols:
            out = [o for o in out if o["symbol"] in symbols]
        return [dict(o) for o in out]

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.submits.append(order)
        if order["type"] == "stop" and self.stop_failures > 0:
            self.stop_failures -= 1
            raise RuntimeError("HTTP 500")
        self._n += 1
        oid = f"o{self._n}"
        o = {**order, "id": oid, "status": "accepted", "filled_qty": "0", "filled_avg_price": None,
             "filled_at": None}
        self.orders[oid] = o
        return dict(o)

    def get_order(self, oid: str, nested: bool = False) -> dict[str, Any]:
        o = self.orders[oid]
        if o["side"] == "buy" and o["status"] in ("accepted", "new", "partially_filled"):
            if self.fill == "full":
                o.update(status="filled", filled_qty=o["qty"], filled_avg_price="100",
                         filled_at=utc(ny(TODAY, 11, 0)))
            elif self.fill == "partial":
                o.update(status="partially_filled", filled_qty="0.05", filled_avg_price="100",
                         filled_at=utc(ny(TODAY, 11, 0)))
        return dict(o)

    def cancel_order(self, oid: str) -> None:
        self.cancels.append(oid)
        o = self.orders[oid]
        if o["status"] != "filled":
            o["status"] = "canceled"

    def by_type(self, t: str) -> list[dict[str, Any]]:
        return [s for s in self.submits if s["type"] == t]


def _entry_app(tmp_path: Path, fconfig: dict[str, Any], broker: EntryBroker) -> Any:
    import itertools

    import yaml

    import trader
    from lib.state import StateStore
    fconfig["orders"].update(entry_fill_timeout_seconds=60, entry_fill_poll_seconds=5)
    state = StateStore(tmp_path)
    (tmp_path / "watchlist").mkdir(parents=True)
    state.watchlist_path(TODAY).write_text(yaml.safe_dump(watchlist_data()), encoding="utf-8")
    state.save_open_trades({})
    clock = itertools.count(0, 7)           # each monotonic() call advances 7 "seconds"
    return trader.App(config=fconfig, state=state, client=broker, now=ny(TODAY, 11, 0),
                      sleep=lambda _s: None, monotonic=lambda: float(next(clock)))


def _enter(app: Any) -> dict[str, Any]:
    import trader
    return trader.cmd_enter(app, argparse.Namespace(symbol="XYZ", rationale="trigger met",
                                                    dry_run=False, size_factor=1.0))


def test_enter_full_fill_places_stop(tmp_path: Path, fconfig: dict[str, Any]) -> None:
    broker = EntryBroker(fill="full")
    app = _entry_app(tmp_path, fconfig, broker)
    out = _enter(app)
    [buy] = broker.by_type("limit")
    assert buy["time_in_force"] == "day" and buy["qty"] == "0.1"
    [stop] = broker.by_type("stop")
    assert stop["qty"] == "0.1" and stop["stop_price"] == "95.00" and stop["side"] == "sell"
    assert broker.cancels == []
    assert out["fill"]["status"] == "filled" and out["fill"]["protected"]
    rec = app.state.load_open_trades()[buy["client_order_id"]]
    assert rec["entry_status"] == "filled" and rec["qty"] == 0.1
    assert rec["stop_order_id"] == out["fill"]["stop_order_id"]
    assert rec["stop_order_ids"] == [rec["stop_order_id"]]


def test_enter_partial_fill_cancels_remainder_and_stops_filled_qty(tmp_path: Path, fconfig: dict[str, Any]) -> None:
    broker = EntryBroker(fill="partial")
    app = _entry_app(tmp_path, fconfig, broker)
    out = _enter(app)
    [buy] = broker.by_type("limit")
    entry_id = out["fill"]["entry_order_id"]
    assert broker.cancels == [entry_id]
    assert out["fill"]["cancel_confirmed"] is True
    [stop] = broker.by_type("stop")
    assert stop["qty"] == "0.05"
    rec = app.state.load_open_trades()[buy["client_order_id"]]
    assert rec["entry_status"] == "partial" and rec["qty"] == 0.05 and rec["filled_qty"] == 0.05
    assert rec["stop_order_id"] is not None


def test_enter_no_fill_is_cancelled_without_stop(tmp_path: Path, fconfig: dict[str, Any]) -> None:
    broker = EntryBroker(fill="none")
    app = _entry_app(tmp_path, fconfig, broker)
    out = _enter(app)
    [buy] = broker.by_type("limit")
    assert broker.cancels == [out["fill"]["entry_order_id"]]
    assert broker.by_type("stop") == [] and broker.by_type("market") == []
    assert out["fill"]["status"] == "cancelled"
    rec = app.state.load_open_trades()[buy["client_order_id"]]
    assert rec["entry_status"] == "cancelled" and rec["qty"] == 0.0 and rec["stop_order_id"] is None


def test_enter_stop_retry_succeeds(tmp_path: Path, fconfig: dict[str, Any]) -> None:
    broker = EntryBroker(fill="full", stop_failures=1)
    app = _entry_app(tmp_path, fconfig, broker)
    out = _enter(app)
    assert len(broker.by_type("stop")) == 2           # first try + one retry
    assert broker.by_type("market") == []
    assert out["fill"]["protected"] and out["fill"]["status"] == "filled"


def test_enter_stop_failure_retries_then_closes(tmp_path: Path, fconfig: dict[str, Any]) -> None:
    import trader
    broker = EntryBroker(fill="full", stop_failures=2)
    app = _entry_app(tmp_path, fconfig, broker)
    with pytest.raises(trader.CommandError) as info:
        _enter(app)
    assert len(broker.by_type("stop")) == 2           # retried exactly once
    [sell] = broker.by_type("market")
    assert sell["side"] == "sell" and sell["qty"] == "0.1" and sell["time_in_force"] == "day"
    payload = info.value.payload
    assert payload["fill"]["status"] == "closed_unprotected"
    assert payload["slack_warning"].startswith("⚠️ UNPROTECTED: XYZ")
    [buy] = broker.by_type("limit")
    rec = app.state.load_open_trades()[buy["client_order_id"]]
    assert rec["close_reason"] == "risk" and rec["close_order_id"] == payload["fill"]["close_order_id"]

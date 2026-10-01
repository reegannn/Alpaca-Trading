"""`scan`: pattern flags on synthetic bars, ATR, relative strength, universe filters
(leveraged/volatility exclusion), batching and HTTP 429 backoff, and the command end to end."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import TODAY, ny, weekday_calendar_rows
from lib import alpaca_client
from lib.alpaca_client import AlpacaClient, AlpacaError, rate_limit_wait
from lib.scan import (ScanParams, analyse, atr, bar_metrics, completed_daily_bar, pattern_flags,
                      rank_by_dollar_volume, relative_strength_leaders, universe_assets)

PARAMS = ScanParams()
LAST_SESSION = date(2026, 9, 25)          # TODAY is Monday 2026-09-28


def bars_from(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None,
              volume: float = 1_000_000, end: date = LAST_SESSION) -> list[dict[str, Any]]:
    """SIP-style daily bars (weekdays, ascending) ending on ``end``."""
    days: list[date] = []
    d = end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    highs = highs or [c + 0.5 for c in closes]
    lows = lows or [c - 0.5 for c in closes]
    return [{"t": f"{day.isoformat()}T04:00:00Z", "o": c, "h": h, "l": lo, "c": c, "v": volume}
            for day, c, h, lo in zip(days, closes, highs, lows)]


def rising(n: int = 70, start: float = 50.0, end: float = 100.0) -> list[float]:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def pullback_closes() -> tuple[list[float], list[float]]:
    """50 bars at 80, then 20 at 100 (SMA20 100 > SMA50 88), with a spike high of 110 in the
    last 20 bars so the close is 9% below the 20-day high (not a breakout)."""
    closes = [80.0] * 50 + [100.0] * 20
    highs = [c + 0.5 for c in closes]
    highs[-10] = 110.0
    return closes, highs


# ------------------------------------------------------------- metrics/flags

def test_breakout_watch_on_rising_bars() -> None:
    m = bar_metrics(bars_from(rising()))
    assert m["close"] == pytest.approx(100.0)
    assert m["sma20"] > m["sma50"] and m["close"] > m["sma50"]
    assert m["below_20d_high"] == pytest.approx(0.5 / 100.5)
    assert pattern_flags(m, PARAMS) == ["breakout_watch"]        # 7.4% above SMA20: no pullback


def test_pullback_watch_without_breakout() -> None:
    closes, highs = pullback_closes()
    m = bar_metrics(bars_from(closes, highs=highs))
    assert m["sma20"] == pytest.approx(100.0) and m["sma50"] == pytest.approx(88.0)
    assert m["from_sma20"] == pytest.approx(0.0)
    assert m["below_20d_high"] == pytest.approx(10 / 110)
    assert pattern_flags(m, PARAMS) == ["pullback_watch"]


def test_downtrend_flags_nothing() -> None:
    assert pattern_flags(bar_metrics(bars_from(rising(start=100.0, end=50.0))), PARAMS) == []


def test_short_history_flags_nothing() -> None:
    m = bar_metrics(bars_from(rising(n=30)))
    assert m["sma50"] is None and pattern_flags(m, PARAMS) == []


def metrics(**kw: Any) -> dict[str, Any]:
    m = {"close": 100.0, "sma20": 99.0, "sma50": 90.0, "below_20d_high": 0.10, "from_sma20": 0.10}
    m.update(kw)
    return m


@pytest.mark.parametrize("below,flagged", [(0.0, True), (0.03, True), (0.0301, False)])
def test_breakout_boundary(below: float, flagged: bool) -> None:
    assert ("breakout_watch" in pattern_flags(metrics(below_20d_high=below), PARAMS)) is flagged


@pytest.mark.parametrize("dev,flagged", [(0.02, True), (-0.02, True), (0.0201, False), (-0.0201, False)])
def test_pullback_boundary(dev: float, flagged: bool) -> None:
    assert ("pullback_watch" in pattern_flags(metrics(from_sma20=dev), PARAMS)) is flagged


@pytest.mark.parametrize("kw", [
    {"sma20": 89.0},                    # SMA20 below SMA50
    {"close": 90.0, "sma20": 91.0},     # close not above SMA50
])
def test_trend_conditions_required(kw: dict[str, Any]) -> None:
    assert pattern_flags(metrics(below_20d_high=0.0, from_sma20=0.0, **kw), PARAMS) == []


def test_atr_simple_mean_of_true_ranges() -> None:
    closes = [100.0] * 20
    assert atr(bars_from(closes, highs=[101.0] * 20, lows=[99.0] * 20)) == pytest.approx(2.0)
    gap = bars_from(closes, highs=[101.0] * 20, lows=[99.0] * 20)
    gap[-1].update(h=106.0, l=104.0, c=105.0)          # TR = 106 - prev close 100 = 6
    assert atr(gap) == pytest.approx((13 * 2.0 + 6.0) / 14)
    assert atr(bars_from([100.0] * 14)) is None        # needs 15 bars


def test_swing_low_and_return() -> None:
    closes = rising()
    lows = [c - 0.5 for c in closes]
    lows[-3] = 70.0
    m = bar_metrics(bars_from(closes, lows=lows))
    assert m["swing_low_10d"] == 70.0
    assert m["return_20d"] == pytest.approx(closes[-1] / closes[-21] - 1)


def test_relative_strength_top_decile() -> None:
    rs = {f"S{i:02d}": float(i) for i in range(20)}
    assert relative_strength_leaders(rs, 0.10) == {"S19", "S18"}
    tied = {"A": 5.0, "B": 5.0, "C": 5.0, **{f"Z{i}": 1.0 for i in range(7)}}
    assert relative_strength_leaders(tied, 0.10) == {"A", "B", "C"}       # ties at the cut-off
    assert relative_strength_leaders({"A": None}, 0.10) == set()


# ---------------------------------------------------------------- universe

def asset(sym: str, name: str = "Plain Corp Common Stock", **kw: Any) -> dict[str, Any]:
    a = {"symbol": sym, "name": name, "class": "us_equity", "status": "active", "tradable": True,
         "exchange": "NASDAQ", "fractionable": True}
    a.update(kw)
    return a


def test_universe_excludes_leveraged_and_volatility(config: dict[str, Any]) -> None:
    assets = [
        asset("AAA"),
        asset("TQQQ", "ProShares UltraPro QQQ"),                                  # symbol list
        asset("SEMX", "Direxion Daily Semiconductor Bull 3X Shares"),             # name pattern
        asset("VOLX", "iPath Series B S&P 500 VIX Short-Term Futures ETN"),       # volatility
        asset("UCTT", "Ultra Clean Holdings, Inc. Common Stock"),                 # a stock: kept
    ]
    kept, dropped = universe_assets(assets, config["universe"], set(), require_fractionable=True)
    assert [a["symbol"] for a in kept] == ["AAA", "UCTT"]
    assert dropped == {"leveraged_or_volatility": 3}


def test_universe_static_filters(config: dict[str, Any]) -> None:
    assets = [
        asset("OTCX", exchange="OTC"),
        asset("DEAD", status="inactive"),
        asset("NOTR", tradable=False),
        asset("NOFR", fractionable=False),
        asset("BTC/USD", **{"class": "crypto"}),
        asset("AB$C"),
        asset("EXCL"),
        asset("OKAY"),
    ]
    kept, dropped = universe_assets(assets, config["universe"], {"EXCL"}, require_fractionable=True)
    assert [a["symbol"] for a in kept] == ["OKAY"]
    assert dropped == {"exchange": 1, "inactive": 1, "not_tradable": 1, "not_fractionable": 1,
                       "not_us_equity": 1, "symbol_format": 1, "excluded": 1}
    kept2, _ = universe_assets([asset("NOFR", fractionable=False)], config["universe"], set(),
                               require_fractionable=False)
    assert [a["symbol"] for a in kept2] == ["NOFR"]           # bracket mode does not need it


def snap(close: float, volume: float, day: date = LAST_SESSION, key: str = "dailyBar") -> dict[str, Any]:
    return {key: {"t": f"{day.isoformat()}T04:00:00Z", "c": close, "v": volume}}


def test_completed_daily_bar_before_and_during_session() -> None:
    before_open = snap(10.0, 1.0)                              # dailyBar is the last session
    assert completed_daily_bar(before_open, LAST_SESSION)["c"] == 10.0
    during = {**snap(11.0, 1.0, day=TODAY), **snap(10.0, 1.0, key="prevDailyBar")}
    assert completed_daily_bar(during, LAST_SESSION)["c"] == 10.0
    assert completed_daily_bar(snap(10.0, 1.0, day=date(2026, 9, 1)), LAST_SESSION) is None


def test_rank_by_dollar_volume_filters_price() -> None:
    snaps = {"A": snap(10.0, 1_000_000), "B": snap(200.0, 100_000), "C": snap(4.99, 50_000_000),
             "D": snap(50.0, 1_000_000), "E": {}}
    rows = rank_by_dollar_volume(snaps, ["A", "B", "C", "D", "E"], LAST_SESSION, min_price=5.0)
    assert [r["symbol"] for r in rows] == ["D", "B", "A"]    # $50M, $20M, $10M; C under $5
    assert [r["dollar_volume_rank"] for r in rows] == [1, 2, 3]


# --------------------------------------------------------- client batching

class Resp:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.text = json.dumps(body if body is not None else {})
        self.content = self.text.encode()
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.text)


class Session:
    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kw: Any) -> Resp:
        self.calls.append({"method": method, "url": url, **kw})
        return self.responder(method, url, kw.get("params") or {})


def test_snapshots_are_batched() -> None:
    syms = [f"S{i:03d}" for i in range(250)]
    s = Session(lambda m, u, p: Resp(200, {sym: {} for sym in p["symbols"].split(",")}))
    out = AlpacaClient(session=s, environ={}).get_snapshots(syms, feed="iex", batch_size=100)
    sizes = [len(c["params"]["symbols"].split(",")) for c in s.calls]
    assert sizes == [100, 100, 50] and len(out) == 250
    assert all(c["params"]["feed"] == "iex" for c in s.calls)


def test_bars_are_batched_and_paginated() -> None:
    def respond(method: str, url: str, params: dict[str, Any]) -> Resp:
        first = params["symbols"].split(",")[0]
        if first == "S000" and not params.get("page_token"):
            return Resp(200, {"bars": {"S000": [{"c": 1}]}, "next_page_token": "p2"})
        return Resp(200, {"bars": {first: [{"c": 2}]}, "next_page_token": None})

    syms = [f"S{i:03d}" for i in range(150)]
    s = Session(respond)
    out = AlpacaClient(session=s, environ={}).get_daily_bars(syms, start="2026-06-01",
                                                             end="2026-09-25", batch_size=100)
    assert len(s.calls) == 3                       # batch 1 (two pages) + batch 2
    assert s.calls[1]["params"]["page_token"] == "p2"
    assert out["S000"] == [{"c": 1}, {"c": 2}]
    assert s.calls[0]["params"]["limit"] == 10000


def test_invalid_batch_size_rejected() -> None:
    with pytest.raises(AlpacaError):
        AlpacaClient(session=Session(lambda *a: Resp(200)), environ={}).get_snapshots(["A"], batch_size=0)


# ------------------------------------------------------------- 429 backoff

def test_rate_limit_wait_prefers_reset_header() -> None:
    assert rate_limit_wait({"X-RateLimit-Reset": "1010"}, 0, now=1000.0) == 10.0
    assert rate_limit_wait({"x-ratelimit-reset": "990"}, 0, now=1000.0) == 1.0       # already passed
    assert rate_limit_wait({"X-RateLimit-Reset": "99999"}, 0, now=1000.0) == 60.0    # capped
    assert rate_limit_wait({"Retry-After": "5"}, 0, now=1000.0) == 5.0
    assert rate_limit_wait({"X-RateLimit-Reset": "soon"}, 2, now=1000.0) == 8.0      # fallback
    assert rate_limit_wait(None, 0) == 2.0 and rate_limit_wait({}, 9) == 30.0


def test_read_backs_off_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(alpaca_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(alpaca_client.time, "time", lambda: 1000.0)
    responses = [Resp(429, headers={"X-RateLimit-Reset": "1012"}), Resp(429), Resp(200, {"ok": 1})]
    s = Session(lambda *a: responses.pop(0))
    client = AlpacaClient(session=s, environ={})
    assert client.get_account() == {"ok": 1}
    assert sleeps == [12.0, 4.0]                   # reset header, then fallback for retry 2
    assert client.request_count == 3


def test_429_gives_up_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alpaca_client.time, "sleep", lambda _s: None)
    s = Session(lambda *a: Resp(429))
    with pytest.raises(AlpacaError) as info:
        AlpacaClient(session=s, environ={}).get_account()
    assert info.value.status_code == 429
    assert len(s.calls) == 1 + alpaca_client.RATE_LIMIT_RETRIES


def test_429_on_order_submission_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alpaca_client.time, "sleep", lambda _s: None)
    s = Session(lambda *a: Resp(429))
    with pytest.raises(AlpacaError):
        AlpacaClient(session=s, environ={}).submit_order({"symbol": "XYZ"})
    assert len(s.calls) == 1


# ------------------------------------------------------------ end to end

class ScanClient:
    """Fake client: assets, calendar, snapshots and bars for the scan command."""

    def __init__(self, assets: list[dict[str, Any]], snaps: dict[str, Any],
                 bars: dict[str, list[dict[str, Any]]]) -> None:
        self.assets, self.snaps, self.bars = assets, snaps, bars
        self.snapshot_batches: list[int] = []
        self.bar_requests: list[list[str]] = []
        self.request_count = 0

    def list_assets(self, status: str = "active", asset_class: str = "us_equity") -> list[dict[str, Any]]:
        self.request_count += 1
        return self.assets

    def get_calendar(self, start: str, end: str) -> list[dict[str, str]]:
        return weekday_calendar_rows(date.fromisoformat(start), date.fromisoformat(end))

    def get_snapshots(self, symbols: list[str], feed: str = "iex", batch_size: int = 100) -> dict[str, Any]:
        for i in range(0, len(symbols), batch_size):
            self.snapshot_batches.append(len(symbols[i:i + batch_size]))
            self.request_count += 1
        return {s: self.snaps[s] for s in symbols if s in self.snaps}

    def get_daily_bars(self, symbols: list[str], batch_size: int = 100, **kw: Any) -> dict[str, Any]:
        self.bar_requests.append(list(symbols))
        return {s: self.bars.get(s, []) for s in symbols}


def test_scan_command_end_to_end(tmp_path: Path, config: dict[str, Any]) -> None:
    import trader
    from lib.state import StateStore
    config["orders"] = {"mode": "fractional"}
    config["scan"] = {"top_n": 3, "batch_size": 2}
    closes_pb, highs_pb = pullback_closes()
    assets = [asset("BRK"), asset("PUL"), asset("DWN"), asset("SML"), asset("TINY"),
              asset("TQQQ", "ProShares UltraPro QQQ"), asset("NOFR", fractionable=False)]
    snaps = {"BRK": snap(100.0, 3_000_000), "PUL": snap(100.0, 2_000_000), "DWN": snap(50.0, 1_500_000),
             "SML": snap(20.0, 100_000), "TINY": snap(3.0, 90_000_000), "TQQQ": snap(80.0, 9e9)}
    bars = {"BRK": bars_from(rising()), "PUL": bars_from(closes_pb, highs=highs_pb),
            "DWN": bars_from(rising(start=100.0, end=50.0)), "SPY": bars_from(rising(start=95.0, end=100.0))}
    client = ScanClient(assets, snaps, bars)
    app = trader.App(config=config, state=StateStore(tmp_path), client=client, now=ny(TODAY, 8, 10))
    out = trader.cmd_scan(app, argparse.Namespace())

    assert out["session"] == "2026-09-25"
    u = out["universe"]
    assert u["assets"] == 7 and u["after_static_filters"] == 5          # TQQQ, NOFR dropped
    assert u["dropped_by_static_filters"] == {"leveraged_or_volatility": 1, "not_fractionable": 1}
    assert u["priced_at_or_above_min_price"] == 4                       # TINY < $5
    assert u["ranked_kept"] == 3 and u["analysed"] == 3                  # SML cut by top_n
    assert client.snapshot_batches == [2, 2, 1]                         # 5 symbols, batch 2
    assert client.bar_requests == [["BRK", "PUL", "DWN", "SPY"]]
    # 20-day returns: PUL +25% (80 -> 100 step), BRK about +17%, DWN negative; top decile of 3 = 1.
    assert out["counts"] == {"breakout_watch": 1, "pullback_watch": 1, "relative_strength": 1}
    assert [(r["pattern"], r["symbol"]) for r in out["rows"]] == [
        ("breakout_watch", "BRK"), ("pullback_watch", "PUL"), ("relative_strength", "PUL")]
    brk, pul = out["rows"][0], out["rows"][1]
    assert brk["patterns"] == ["breakout_watch"]
    assert pul["patterns"] == ["pullback_watch", "relative_strength"]
    for key in ("sma20", "sma50", "atr14", "high_20d", "swing_low_10d", "rs_vs_spy_20d_pct",
                "close_minus_1_5_atr", "dollar_volume_rank"):
        assert brk[key] is not None, key
    assert brk["dollar_volume_rank"] == 1
    assert out["requests"] == client.request_count


def test_analyse_sorts_by_pattern_then_strength(config: dict[str, Any]) -> None:
    spy = bars_from(rising(start=99.0, end=100.0))
    bars = {"A": bars_from(rising(start=60.0)), "B": bars_from(rising(start=40.0)), "SPY": spy}
    ranked = [{"symbol": "A", "prev_dollar_volume": 2e8, "dollar_volume_rank": 1},
              {"symbol": "B", "prev_dollar_volume": 1e8, "dollar_volume_rank": 2}]
    out = analyse(ranked, {"A": asset("A"), "B": asset("B")}, bars, config["universe"], set(),
                  False, ScanParams(rs_top_fraction=1.0))
    order = [(r["pattern"], r["symbol"]) for r in out["rows"]]
    # B rose more over 20 days (it started lower), so it leads within each pattern.
    assert order == [("breakout_watch", "B"), ("breakout_watch", "A"),
                     ("relative_strength", "B"), ("relative_strength", "A")]


def test_analyse_without_spy_skips_relative_strength(config: dict[str, Any]) -> None:
    ranked = [{"symbol": "A", "prev_dollar_volume": 2e8, "dollar_volume_rank": 1}]
    out = analyse(ranked, {"A": asset("A")}, {"A": bars_from(rising())}, config["universe"], set(),
                  False, PARAMS)
    assert out["counts"]["relative_strength"] == 0 and out["warnings"]


def test_scan_params_validation() -> None:
    assert ScanParams.from_config(None) == ScanParams()
    for bad in ({"top_n": 0}, {"batch_size": 101}, {"bars_days": 40}, {"rs_top_fraction": 0}):
        with pytest.raises(ValueError):
            ScanParams.from_config(bad)

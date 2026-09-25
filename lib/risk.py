"""Every pre-trade check. There is no bypass flag.

``evaluate_entry`` is a pure function over an ``EntryContext`` built from live
Alpaca data (account, clock, positions, orders) plus today's watchlist. It runs
*all* checks and returns *every* failure, not just the first.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .market_calendar import NY, Session, TradingCalendar, parse_date, parse_ts
from .sizing import (SizingResult, entry_limit_price, round_to_tick, size_position,
                     valid_size_factor)
from .universe import UniverseResult
from .watchlist import Watchlist, earnings_value

CLIENT_ORDER_PREFIX = "sw-"
OPEN_PARENT_STATUSES = {"new", "accepted", "pending_new", "partially_filled",
                        "accepted_for_bidding", "held", "pending_replace", "replaced"}


# --------------------------------------------------------------------- helpers

def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def circuit_breaker(account: dict[str, Any], risk_cfg: dict[str, Any]) -> tuple[bool, float | None]:
    """(active, day_pnl_pct) where day_pnl_pct = (equity - last_equity) / last_equity."""
    equity = _f(account.get("equity"))
    last_equity = _f(account.get("last_equity"))
    if last_equity <= 0:
        # Cannot compute: fail safe and treat the breaker as active.
        return True, None
    pct = (equity - last_equity) / last_equity
    return pct <= float(risk_cfg["daily_loss_circuit_breaker_pct"]), pct


def is_bot_entry(order: dict[str, Any]) -> bool:
    return str(order.get("client_order_id") or "").startswith(CLIENT_ORDER_PREFIX)


def count_entries_today(orders: list[dict[str, Any]], today: date) -> int:
    """Bot entry orders (sw- prefix) created today (New York date), from Alpaca history."""
    n = 0
    for o in orders:
        if not is_bot_entry(o):
            continue
        created = parse_ts(o.get("created_at") or o.get("submitted_at"))
        if created is not None and created.astimezone(NY).date() == today:
            n += 1
    return n


def pending_entries(open_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open bot entry (buy) orders that are not yet fully filled."""
    out = []
    for o in open_orders:
        if is_bot_entry(o) and o.get("side") == "buy" and o.get("status") in OPEN_PARENT_STATUSES:
            out.append(o)
    return out


def pending_notional(order: dict[str, Any]) -> float:
    remaining = _f(order.get("qty")) - _f(order.get("filled_qty"))
    return max(remaining, 0.0) * _f(order.get("limit_price"))


def sector_by_symbol(open_trades: dict[str, dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rec in open_trades.values():
        sym = str(rec.get("symbol", "")).upper()
        if sym:
            out[sym] = str(rec.get("sector") or "Unknown")
    return out


def new_client_order_id(symbol: str, today: date) -> str:
    return f"{CLIENT_ORDER_PREFIX}{today.strftime('%Y%m%d')}-{symbol.upper()}-{secrets.token_hex(3)}"


# ------------------------------------------------------------------- entries

@dataclass
class EntryContext:
    symbol: str
    now: datetime
    today: date
    clock_is_open: bool
    session: Session | None
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]      # GET /v2/orders?status=open&nested=false
    todays_orders: list[dict[str, Any]]    # GET /v2/orders?status=all&after=<NY midnight>
    watchlist: Watchlist
    universe: UniverseResult
    last_price: float | None
    open_trades: dict[str, dict[str, Any]]
    calendar: TradingCalendar              # must cover today .. earnings date
    size_factor: float = 1.0               # 0 < F <= 1; only ever shrinks the position


@dataclass
class EntryDecision:
    symbol: str
    passed: bool
    failures: list[dict[str, str]] = field(default_factory=list)
    candidate: dict[str, Any] | None = None
    last_price: float | None = None
    limit_price: float | None = None
    stop: float | None = None
    target: float | None = None
    sizing: SizingResult | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def qty(self) -> int:
        return self.sizing.qty if self.sizing else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "passed": self.passed, "failures": self.failures,
            "last_price": self.last_price, "limit_price": self.limit_price,
            "stop": self.stop, "target": self.target, "qty": self.qty,
            "sizing": self.sizing.to_dict() if self.sizing else None,
            "metrics": self.metrics,
        }


def evaluate_entry(ctx: EntryContext, config: dict[str, Any]) -> EntryDecision:
    risk_cfg = config["risk"]
    sym = ctx.symbol.upper()
    d = EntryDecision(symbol=sym, passed=False, last_price=ctx.last_price)

    def fail(check: str, reason: str) -> None:
        d.failures.append({"check": check, "reason": reason})

    equity = _f(ctx.account.get("equity"))
    d.metrics["equity"] = equity

    # 0. Long-only is a hard invariant of this bot.
    if not risk_cfg.get("long_only", True):
        fail("long_only", "config risk.long_only must be true; this bot only buys")

    # Size factor may only shrink a position.
    d.metrics["size_factor"] = ctx.size_factor
    if not valid_size_factor(ctx.size_factor):
        fail("size_factor", f"size_factor {ctx.size_factor!r} must satisfy 0 < F <= 1")

    # 1. Market hours with buffers after the open and before the close.
    after_open = timedelta(minutes=int(risk_cfg.get("no_entry_minutes_after_open", 15)))
    before_close = timedelta(minutes=int(risk_cfg.get("no_entry_minutes_before_close", 15)))
    if not ctx.clock_is_open or ctx.session is None:
        fail("market_hours", "market is closed")
    else:
        if ctx.now < ctx.session.open + after_open:
            fail("market_hours", f"less than {after_open} since the open")
        if ctx.now > ctx.session.close - before_close:
            fail("market_hours", f"less than {before_close} before the close")

    # 2. Circuit breaker.
    active, day_pct = circuit_breaker(ctx.account, risk_cfg)
    d.metrics["day_pnl_pct"] = None if day_pct is None else round(day_pct * 100, 3)
    if active:
        fail("circuit_breaker", "daily loss circuit breaker is active"
             if day_pct is not None else "cannot compute day P&L (last_equity missing)")

    # 3. Entries today (from Alpaca order history).
    entries_today = count_entries_today(ctx.todays_orders, ctx.today)
    d.metrics["entries_today"] = entries_today
    max_entries = int(risk_cfg["max_new_entries_per_day"])
    if entries_today >= max_entries:
        fail("daily_entry_limit", f"{entries_today} entries already today (max {max_entries})")

    # 4. Today's watchlist entry must exist and validate.
    wl_errors = ctx.watchlist.errors_for(sym)
    cand = ctx.watchlist.find(sym)
    d.candidate = cand
    for e in wl_errors:
        fail("watchlist", e)

    # 5. Universe.
    if not ctx.universe.eligible:
        for e in ctx.universe.failures:
            fail("universe", e)

    # 6. No existing position or open order in the symbol.
    if any(str(p.get("symbol", "")).upper() == sym for p in ctx.positions):
        fail("existing_position", f"already holding {sym}")
    if any(str(o.get("symbol", "")).upper() == sym for o in ctx.open_orders):
        fail("existing_order", f"open order already exists for {sym}")

    # Price plan (needs a candidate and a last price).
    stop = target = limit = None
    sizing: SizingResult | None = None
    if cand is not None:
        stop_raw, target_raw = cand.get("stop"), cand.get("target")
        if isinstance(stop_raw, (int, float)) and isinstance(target_raw, (int, float)):
            stop, target = round_to_tick(float(stop_raw)), round_to_tick(float(target_raw))
    if ctx.last_price is not None and ctx.last_price > 0:
        limit = entry_limit_price(ctx.last_price, float(risk_cfg["entry_limit_slippage_pct"]))
    d.stop, d.target, d.limit_price = stop, target, limit

    if limit is not None and stop is not None and valid_size_factor(ctx.size_factor):
        sizing = size_position(equity, limit, stop, risk_cfg, ctx.size_factor)
        d.sizing = sizing
        if not sizing.ok:
            fail("sizing", sizing.reason or "qty < 1")
    new_notional = (sizing.qty * limit) if (sizing and limit) else 0.0
    d.metrics["notional"] = round(new_notional, 2)

    pend = [o for o in pending_entries(ctx.open_orders) if str(o.get("symbol", "")).upper() != sym]

    # 7. Open positions (+ pending entries) + this one.
    held = {str(p.get("symbol", "")).upper() for p in ctx.positions}
    held |= {str(o.get("symbol", "")).upper() for o in pend}
    max_pos = int(risk_cfg["max_open_positions"])
    d.metrics["open_positions_incl_pending"] = len(held)
    if len(held) + 1 > max_pos:
        fail("max_open_positions", f"{len(held)} open/pending + 1 would exceed max {max_pos}")

    # 8. Gross exposure after entry.
    gross = sum(abs(_f(p.get("market_value"))) for p in ctx.positions)
    gross += sum(pending_notional(o) for o in pend)
    max_gross = float(risk_cfg["max_gross_exposure_pct_equity"]) * equity
    d.metrics["gross_exposure_after"] = round(gross + new_notional, 2)
    if equity <= 0 or gross + new_notional > max_gross:
        fail("gross_exposure",
             f"gross exposure {gross + new_notional:,.2f} would exceed {max_gross:,.2f}")

    # 9. Sector exposure.
    if cand is not None:
        sectors = sector_by_symbol(ctx.open_trades)
        my_sector = str(cand.get("sector") or "Unknown").strip().lower()
        sector_exp = sum(abs(_f(p.get("market_value"))) for p in ctx.positions
                         if sectors.get(str(p.get("symbol", "")).upper(), "Unknown").strip().lower() == my_sector)
        sector_exp += sum(pending_notional(o) for o in pend
                          if sectors.get(str(o.get("symbol", "")).upper(), "Unknown").strip().lower() == my_sector)
        max_sector = float(risk_cfg["max_sector_pct_equity"]) * equity
        d.metrics["sector_exposure_after"] = round(sector_exp + new_notional, 2)
        if equity <= 0 or sector_exp + new_notional > max_sector:
            fail("sector_exposure",
                 f"sector '{cand.get('sector')}' exposure {sector_exp + new_notional:,.2f} "
                 f"would exceed {max_sector:,.2f}")

    # 10. Earnings blackout.
    if cand is not None:
        for e in earnings_failures(cand, ctx.today, ctx.calendar,
                                   int(risk_cfg["earnings_blackout_trading_days"])):
            fail("earnings", e)

    # 11. Price structure.
    if ctx.last_price is None or ctx.last_price <= 0:
        fail("prices", "no last trade price available")
    if cand is not None and (stop is None or target is None):
        fail("prices", "stop/target missing or not numeric")
    if limit is not None and stop is not None and target is not None:
        if not (stop < limit < target):
            fail("prices", f"require stop < limit < target (stop={stop}, limit={limit}, target={target})")
        else:
            stop_dist = (limit - stop) / limit
            rr = (target - limit) / (limit - stop)
            d.metrics["stop_distance_pct"] = round(stop_dist * 100, 3)
            d.metrics["reward_to_risk"] = round(rr, 3)
            max_dist = float(risk_cfg["max_stop_distance_pct"])
            if stop_dist > max_dist + 1e-12:
                fail("prices", f"stop distance {stop_dist:.2%} exceeds max {max_dist:.2%}")
            min_rr = float(risk_cfg["min_reward_to_risk"])
            if rr < min_rr - 1e-12:
                fail("prices", f"reward:risk {rr:.2f} below min {min_rr:.2f}")

    # 12. Entry trigger currently met.
    if cand is not None:
        met, why = trigger_met(cand, ctx.last_price)
        if not met:
            fail("trigger", why)

    d.passed = not d.failures
    return d


def trigger_met(cand: dict[str, Any], last_price: float | None) -> tuple[bool, str]:
    trig = cand.get("trigger") or {}
    ttype = trig.get("type")
    tprice = trig.get("price")
    if last_price is None:
        return False, "no last price to evaluate trigger"
    if not isinstance(tprice, (int, float)) or isinstance(tprice, bool):
        return False, "trigger price invalid"
    if ttype == "above":
        return (last_price >= tprice,
                f"last {last_price} {'>=' if last_price >= tprice else '<'} trigger {tprice} (above)")
    if ttype == "below":
        return (last_price <= tprice,
                f"last {last_price} {'<=' if last_price <= tprice else '>'} trigger {tprice} (below)")
    return False, f"unknown trigger type {ttype!r}"


def earnings_failures(cand: dict[str, Any], today: date, calendar: TradingCalendar,
                      blackout: int) -> list[str]:
    asset_type = cand.get("asset_type")
    ev = earnings_value(cand.get("earnings_date"))
    if ev is None:
        return ["earnings_date invalid"]
    if ev == "n/a":
        return [] if asset_type == "etf" else ["earnings_date 'n/a' only allowed for ETFs"]
    if ev == "unknown":
        return ["earnings_date is unknown"]
    assert isinstance(ev, date)
    if ev < today:
        return [f"earnings_date {ev} is in the past; the next date is required"]
    days = calendar.trading_days_between(today, ev)
    if days <= blackout:
        return [f"earnings in {days} trading days (must be more than {blackout})"]
    return []


def next_trading_day(calendar: TradingCalendar, today: date) -> date | None:
    """First session strictly after ``today`` (None if the calendar does not reach it)."""
    for d in calendar.days:
        if d > today:
            return d
    return None


def earnings_exit_due(earnings: Any, today: date, calendar: TradingCalendar) -> bool:
    """True if an open position must be closed ahead of earnings.

    A position is due for ``earnings_exit`` when its earnings date is on or
    before the next trading day, so it is closed during the last session
    before the report (or at once if the date has already arrived).
    'unknown' / 'n/a' / unparseable values never trigger an exit.
    """
    ev = earnings_value(earnings)
    if not isinstance(ev, date):
        return False
    nxt = next_trading_day(calendar, today)
    if nxt is None:
        # Calendar does not extend past today: fall back to the next weekday.
        nxt = today + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    return ev <= nxt


def build_bracket_order(decision: EntryDecision, client_order_id: str) -> dict[str, Any]:
    """Order payload for a decision that passed every check."""
    if not decision.passed:
        raise ValueError("refusing to build an order for a decision that failed risk checks")
    qty, limit, stop, target = decision.qty, decision.limit_price, decision.stop, decision.target
    if not (isinstance(qty, int) and qty >= 1):
        raise ValueError("qty must be a whole number >= 1")
    if limit is None or stop is None or target is None or not (stop < limit < target):
        raise ValueError("invalid bracket prices")
    return {
        "symbol": decision.symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "time_in_force": "gtc",
        "limit_price": f"{limit:.2f}" if limit >= 1 else f"{limit:.4f}",
        "order_class": "bracket",
        "take_profit": {"limit_price": f"{target:.2f}" if target >= 1 else f"{target:.4f}"},
        "stop_loss": {"stop_price": f"{stop:.2f}" if stop >= 1 else f"{stop:.4f}"},
        "client_order_id": client_order_id,
    }


# --------------------------------------------------------------------- close

def check_close(symbol: str, position: dict[str, Any] | None, clock_is_open: bool) -> list[str]:
    """Checks before cancelling bracket legs and closing a position."""
    fails: list[str] = []
    if not clock_is_open:
        # Cancelling the stop leg while closed would leave the position unprotected overnight.
        fails.append("market is closed; closing is only allowed during regular hours")
    if position is None:
        fails.append(f"no open position in {symbol.upper()}")
    else:
        if position.get("side", "long") != "long" or _f(position.get("qty")) <= 0:
            fails.append("position is not a long position")
    return fails


def days_held(calendar: TradingCalendar, entry: datetime | str | None, today: date) -> int | None:
    if entry is None or entry == "":
        return None
    if isinstance(entry, str) and len(entry.strip()) == 10:  # plain YYYY-MM-DD
        d = parse_date(entry)
        return None if d is None else calendar.trading_days_between(d, today)
    try:
        dt = parse_ts(entry) if isinstance(entry, str) else entry
    except ValueError:
        return None
    if dt is None:
        return None
    return calendar.trading_days_between(dt.astimezone(NY).date(), today)

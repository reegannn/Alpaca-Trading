"""Statistics for the weekly review.

Pure functions over trades.csv / skipped.csv rows, daily bars and portfolio
history, so they are testable without the API. ``trader.py review`` gathers
the inputs and calls ``build_report`` / ``render_markdown``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .market_calendar import NY, parse_date, parse_ts


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_date(value: str | None) -> date | None:
    if not value:
        return None
    if len(value.strip()) == 10:
        return parse_date(value)
    try:
        ts = parse_ts(value)
    except ValueError:
        return None
    return ts.astimezone(NY).date() if ts else None


def filter_trades(rows: Iterable[dict[str, str]], start: date | None,
                  end: date | None = None) -> list[dict[str, str]]:
    """Trades whose exit date (New York) falls within [start, end]."""
    out = []
    for r in rows:
        d = _row_date(r.get("exit_time"))
        if d is None:
            continue
        if start and d < start:
            continue
        if end and d > end:
            continue
        out.append(r)
    return out


def trade_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    rs = [x for x in (_f(r.get("r_multiple")) for r in rows) if x is not None]
    pnls = [x for x in (_f(r.get("pnl")) for r in rows) if x is not None]
    days = [x for x in (_f(r.get("days_held")) for r in rows) if x is not None]
    n = len(rows)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) if pnls else None
    win_rs = [r for r in rs if r > 0]
    loss_rs = [r for r in rs if r <= 0]
    expectancy = None
    if rs:
        p_win = len(win_rs) / len(rs)
        avg_win = sum(win_rs) / len(win_rs) if win_rs else 0.0
        avg_loss = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0
        # avg_loss is <= 0 so this is p_win*avg_win - p_loss*|avg_loss|.
        expectancy = p_win * avg_win + (1 - p_win) * avg_loss
    return {
        "trades": n,
        "wins": wins,
        "losses": len(pnls) - wins,
        "win_rate": _round(win_rate, 4),
        "avg_r": _round(sum(rs) / len(rs), 3) if rs else None,
        "expectancy_r": _round(expectancy, 3),
        "total_pnl": _round(sum(pnls), 2) if pnls else 0.0,
        "avg_pnl": _round(sum(pnls) / len(pnls), 2) if pnls else None,
        "avg_days_held": _round(sum(days) / len(days), 2) if days else None,
    }


def grouped_stats(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        groups.setdefault(r.get(key) or "(none)", []).append(r)
    return {k: trade_stats(v) for k, v in sorted(groups.items())}


def exit_breakdown(rows: list[dict[str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = r.get("exit_reason") or "unknown"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def benchmark_return(bars: list[dict[str, Any]], start: date) -> dict[str, Any]:
    """Buy-and-hold return from the last close before ``start`` to the latest close."""
    if not bars:
        return {"return_pct": None, "note": "no bars"}
    before = [b for b in bars if _bar_date(b) is not None and _bar_date(b) < start]
    after = [b for b in bars if _bar_date(b) is not None and _bar_date(b) >= start]
    if not after:
        return {"return_pct": None, "note": "no bars in period"}
    base = float(before[-1]["c"]) if before else float(after[0]["o"])
    last = float(after[-1]["c"])
    return {
        "base": base, "last": last,
        "from": (_bar_date(before[-1]) if before else _bar_date(after[0])).isoformat(),
        "to": _bar_date(after[-1]).isoformat(),
        "return_pct": round((last / base - 1) * 100, 3) if base else None,
    }


def equity_change(history: dict[str, Any]) -> dict[str, Any]:
    eq = [e for e in (history.get("equity") or []) if e is not None]
    if len(eq) < 1:
        return {"start_equity": None, "end_equity": None, "change": None, "change_pct": None}
    start, end = float(eq[0]), float(eq[-1])
    return {
        "start_equity": start, "end_equity": end,
        "change": round(end - start, 2),
        "change_pct": round((end / start - 1) * 100, 3) if start else None,
    }


def _bar_date(bar: dict[str, Any]) -> date | None:
    ts = parse_ts(bar.get("t"))
    return ts.astimezone(NY).date() if ts else None


def evaluate_skip(row: dict[str, str], bars: list[dict[str, Any]],
                  max_hold_days: int) -> dict[str, Any]:
    """Hypothetical outcome of a skipped trade.

    Entry is assumed at the trigger price on the skip date. Bars from the skip
    date onward (the skip-date bar plus up to ``max_hold_days`` more) are
    scanned in order; the first bar whose low <= stop or high >= target decides
    the outcome. If both could have happened in the same bar, the stop wins
    (conservative). If neither is hit, the trade is marked at the last close.
    """
    skip_date = parse_date(row.get("date"))
    entry = _f(row.get("trigger_price"))
    stop = _f(row.get("stop"))
    target = _f(row.get("target"))
    base = {"date": row.get("date"), "symbol": row.get("symbol"),
            "lesson_id": row.get("lesson_id"), "setup_tag": row.get("setup_tag")}
    if skip_date is None or entry is None or stop is None or target is None or entry <= stop:
        return {**base, "outcome": "invalid", "r": None, "complete": False}
    risk = entry - stop
    window = [b for b in bars if (_bar_date(b) or date.min) >= skip_date][: max_hold_days + 1]
    for b in window:
        low, high = float(b["l"]), float(b["h"])
        hit_stop = low <= stop
        hit_target = high >= target
        if hit_stop:
            return {**base, "outcome": "stop", "r": -1.0, "complete": True,
                    "exit_date": _bar_date(b).isoformat()}
        if hit_target:
            return {**base, "outcome": "target", "r": round((target - entry) / risk, 3),
                    "complete": True, "exit_date": _bar_date(b).isoformat()}
    if not window:
        return {**base, "outcome": "no_data", "r": None, "complete": False}
    last = float(window[-1]["c"])
    complete = len(window) >= max_hold_days + 1
    return {**base, "outcome": "time_stop" if complete else "open",
            "r": round((last - entry) / risk, 3), "complete": complete,
            "exit_date": _bar_date(window[-1]).isoformat()}


def skipped_by_lesson(evals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in evals:
        lid = e.get("lesson_id") or "(none)"
        g = out.setdefault(lid, {"skipped": 0, "evaluated": 0, "hypothetical_total_r": 0.0,
                                 "hypothetical_avg_r": None, "wins": 0, "incomplete": 0})
        g["skipped"] += 1
        if e.get("r") is None:
            continue
        g["evaluated"] += 1
        g["hypothetical_total_r"] = round(g["hypothetical_total_r"] + e["r"], 3)
        if e["r"] > 0:
            g["wins"] += 1
        if not e.get("complete"):
            g["incomplete"] += 1
    for g in out.values():
        if g["evaluated"]:
            g["hypothetical_avg_r"] = round(g["hypothetical_total_r"] / g["evaluated"], 3)
    return out


def build_report(*, period_label: str, start: date | None, end: date,
                 trades: list[dict[str, str]], skipped: list[dict[str, str]],
                 skipped_bars: dict[str, list[dict[str, Any]]],
                 spy_bars: list[dict[str, Any]], portfolio_history: dict[str, Any],
                 open_positions: list[dict[str, Any]], max_hold_days: int) -> dict[str, Any]:
    period_trades = filter_trades(trades, start, end)
    period_skips = [s for s in skipped
                    if (d := parse_date(s.get("date"))) is not None and (start is None or d >= start)
                    and d <= end]
    evals = [evaluate_skip(s, skipped_bars.get(str(s.get("symbol", "")).upper(), []), max_hold_days)
             for s in period_skips]
    bench_start = start or (min((d for d in (_row_date(t.get("entry_time")) for t in trades) if d),
                                default=end))
    return {
        "period": period_label,
        "start": start.isoformat() if start else None,
        "end": end.isoformat(),
        "portfolio": trade_stats(period_trades),
        "by_setup_tag": grouped_stats(period_trades, "setup_tag"),
        "by_idea_source": grouped_stats(period_trades, "idea_source"),
        "exit_reasons": exit_breakdown(period_trades),
        "equity": equity_change(portfolio_history),
        "spy_buy_and_hold": benchmark_return(spy_bars, bench_start),
        "skipped": {"evaluations": evals, "by_lesson": skipped_by_lesson(evals)},
        "open_positions": open_positions,
    }


def _round(v: float | None, n: int) -> float | None:
    return None if v is None else round(v, n)


def _fmt(v: Any, suffix: str = "") -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    return f"{v}{suffix}"


def _stats_table(title: str, groups: dict[str, dict[str, Any]]) -> list[str]:
    lines = [f"### {title}", "",
             "| Group | Trades | Win rate | Avg R | Expectancy R | Total P&L | Avg days |",
             "|---|---|---|---|---|---|---|"]
    if not groups:
        lines.append("| (no trades) | | | | | | |")
    for k, s in groups.items():
        wr = None if s["win_rate"] is None else s["win_rate"] * 100
        lines.append(f"| {k} | {s['trades']} | {_fmt(wr, '%')} | {_fmt(s['avg_r'])} | "
                     f"{_fmt(s['expectancy_r'])} | {_fmt(s['total_pnl'])} | {_fmt(s['avg_days_held'])} |")
    return lines + [""]


def render_markdown(rep: dict[str, Any]) -> str:
    eq, spy, p = rep["equity"], rep["spy_buy_and_hold"], rep["portfolio"]
    lines = [f"# Review: {rep['period']} ({rep['start'] or 'inception'} → {rep['end']})", "",
             "## Performance", "",
             f"- Equity: {_fmt(eq.get('start_equity'))} → {_fmt(eq.get('end_equity'))} "
             f"({_fmt(eq.get('change_pct'), '%')})",
             f"- SPY buy-and-hold: {_fmt(spy.get('return_pct'), '%')}",
             f"- Closed trades: {p['trades']} · win rate "
             f"{_fmt(None if p['win_rate'] is None else p['win_rate'] * 100, '%')} · avg R {_fmt(p['avg_r'])} · "
             f"expectancy R {_fmt(p['expectancy_r'])} · total P&L {_fmt(p['total_pnl'])}", ""]
    lines += _stats_table("By setup_tag", rep["by_setup_tag"])
    lines += _stats_table("By idea_source", rep["by_idea_source"])
    lines += ["### Exit reasons", ""]
    lines += [f"- {k}: {v}" for k, v in rep["exit_reasons"].items()] or ["- (none)"]
    lines += ["", "## Skipped trades (hypothetical, stop wins same-bar ties)", ""]
    by_lesson = rep["skipped"]["by_lesson"]
    if not by_lesson:
        lines.append("- (none)")
    for lid, g in by_lesson.items():
        lines.append(f"- {lid}: skipped {g['skipped']}, evaluated {g['evaluated']}, "
                     f"hypothetical avg R {_fmt(g['hypothetical_avg_r'])}, total R "
                     f"{_fmt(g['hypothetical_total_r'])}, incomplete {g['incomplete']}")
    lines += ["", "## Open positions", ""]
    if not rep["open_positions"]:
        lines.append("- (none)")
    for pos in rep["open_positions"]:
        lines.append(f"- {pos.get('symbol')}: qty {pos.get('qty')}, days held "
                     f"{_fmt(pos.get('days_held'))}, unrealised P&L {_fmt(pos.get('unrealized_pl'))}")
    return "\n".join(lines) + "\n"

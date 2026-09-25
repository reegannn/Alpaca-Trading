"""Review stats by size_factor, and trades.csv header migration."""

from __future__ import annotations

import csv
from pathlib import Path

from lib.review import grouped_stats, render_markdown, size_factor_key
from lib.state import TRADES_COLUMNS, StateStore


def trade(pnl: float, r: float, size_factor: str | None) -> dict[str, str]:
    row = {"pnl": str(pnl), "r_multiple": str(r), "days_held": "3", "exit_time": "2026-09-28T15:00:00Z",
           "setup_tag": "breakout", "idea_source": "screener", "exit_reason": "target"}
    if size_factor is not None:
        row["size_factor"] = size_factor
    return row


def test_size_factor_key_defaults_to_one() -> None:
    assert size_factor_key({}) == "1"
    assert size_factor_key({"size_factor": ""}) == "1"
    assert size_factor_key({"size_factor": "0.5"}) == "0.5"
    assert size_factor_key({"size_factor": "1.0"}) == "1"


def test_grouped_by_size_factor() -> None:
    rows = [trade(100, 2.0, None), trade(-50, -1.0, "1"), trade(30, 1.0, "0.5")]
    groups = grouped_stats(rows, "size_factor")
    assert set(groups) == {"1", "0.5"}
    assert groups["1"]["trades"] == 2
    assert groups["0.5"]["trades"] == 1 and groups["0.5"]["avg_r"] == 1.0


def test_markdown_has_size_factor_section() -> None:
    empty = {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_r": None,
             "expectancy_r": None, "total_pnl": 0.0, "avg_pnl": None, "avg_days_held": None}
    rep = {"period": "p", "start": None, "end": "2026-09-28", "portfolio": empty,
           "by_setup_tag": {}, "by_idea_source": {}, "by_size_factor": {"0.5": empty},
           "exit_reasons": {}, "equity": {}, "spy_buy_and_hold": {},
           "skipped": {"by_lesson": {}}, "open_positions": []}
    assert "By size_factor" in render_markdown(rep)


def test_trades_csv_header_migration(tmp_path: Path) -> None:
    old_cols = [c for c in TRADES_COLUMNS if c != "size_factor"]
    path = tmp_path / "trades.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=old_cols)
        w.writeheader()
        w.writerow({c: "" for c in old_cols} | {"client_order_id": "sw-old", "pnl": "12.5"})

    store = StateStore(tmp_path)
    store.append_trade({"client_order_id": "sw-new", "pnl": 3.0, "size_factor": 0.5})

    rows = store.read_trades()
    with path.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == TRADES_COLUMNS
    assert [r["client_order_id"] for r in rows] == ["sw-old", "sw-new"]
    assert rows[0]["pnl"] == "12.5" and rows[0]["size_factor"] == ""
    assert rows[1]["size_factor"] == "0.5"

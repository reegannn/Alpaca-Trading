"""Config loading and state-file I/O (csv/json/yaml under state/).

Runs are sequential (one routine at a time), so no locking is needed.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

TRADES_COLUMNS = [
    "client_order_id", "symbol", "sector", "setup_tag", "idea_source",
    "entry_time", "entry_price", "qty", "stop", "target", "exit_time",
    "exit_price", "exit_reason", "r_multiple", "pnl", "days_held", "rationale",
    "size_factor",
]

SKIPPED_COLUMNS = [
    "date", "symbol", "lesson_id", "trigger_price", "stop", "target",
    "setup_tag", "idea_source",
]

RUN_LOG_COLUMNS = ["timestamp", "subcommand", "args", "ok", "result"]

EXIT_REASONS = {"stop", "target", "time_stop", "earnings_exit", "thesis_broken", "manual",
                "risk", "unknown"}
CLOSE_REASONS = ("time_stop", "earnings_exit", "target", "stop", "thesis_broken", "manual", "risk")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    for section in ("universe", "risk", "holding", "data"):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"config.yaml: missing section '{section}'")
    # Base URLs are intentionally not configurable.
    for key in ("base_url", "trading_base_url", "data_base_url", "endpoint"):
        if key in cfg or key in cfg.get("data", {}):
            raise ValueError(f"config.yaml: '{key}' is not allowed (base URLs are hard-coded)")
    return cfg


class StateStore:
    """Read/write helpers for the state/ directory on the journal branch."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # paths
    @property
    def trades_csv(self) -> Path:
        return self.root / "trades.csv"

    @property
    def skipped_csv(self) -> Path:
        return self.root / "skipped.csv"

    @property
    def open_trades_json(self) -> Path:
        return self.root / "open_trades.json"

    @property
    def run_log_csv(self) -> Path:
        return self.root / "run_log.csv"

    @property
    def lessons_md(self) -> Path:
        return self.root / "lessons.md"

    def watchlist_path(self, d: date) -> Path:
        return self.root / "watchlist" / f"{d.isoformat()}.yaml"

    def exists(self) -> bool:
        return self.root.is_dir()

    # open_trades.json
    def load_open_trades(self) -> dict[str, dict[str, Any]]:
        if not self.open_trades_json.exists():
            return {}
        text = self.open_trades_json.read_text(encoding="utf-8").strip()
        data = json.loads(text) if text else {}
        if not isinstance(data, dict):
            raise ValueError("state/open_trades.json must contain a JSON object")
        return data

    def save_open_trades(self, data: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.open_trades_json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n",
                       encoding="utf-8")
        tmp.replace(self.open_trades_json)

    # csv helpers
    @staticmethod
    def read_csv(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open("r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    @staticmethod
    def append_csv(path: Path, columns: list[str], row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        if not new_file:
            StateStore._migrate_header(path, columns)
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow({c: _csv_value(row.get(c)) for c in columns})

    @staticmethod
    def _migrate_header(path: Path, columns: list[str]) -> None:
        """Rewrite a CSV whose header differs from ``columns`` (e.g. a new column was added).

        Existing values are kept by column name; new columns are left empty.
        """
        with path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            if header == columns:
                return
            rows = list(reader)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                writer.writerow({c: r.get(c, "") or "" for c in columns})
        tmp.replace(path)

    def read_trades(self) -> list[dict[str, str]]:
        return self.read_csv(self.trades_csv)

    def append_trade(self, row: dict[str, Any]) -> None:
        self.append_csv(self.trades_csv, TRADES_COLUMNS, row)

    def read_skipped(self) -> list[dict[str, str]]:
        return self.read_csv(self.skipped_csv)

    def append_skipped(self, row: dict[str, Any]) -> None:
        self.append_csv(self.skipped_csv, SKIPPED_COLUMNS, row)

    def append_run_log(self, row: dict[str, Any]) -> None:
        self.append_csv(self.run_log_csv, RUN_LOG_COLUMNS, row)


def _csv_value(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        text = f"{v:.6f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    return v

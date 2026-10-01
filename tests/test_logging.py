"""`stamp` output, the full jsonl run log, truncation of large outputs only, key scrubbing,
and run_log.csv as an index (run_id column, header migration)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import trader
from lib.market_calendar import stamp
from lib.state import RUN_LOG_COLUMNS, StateStore


def at(y: int, mo: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)


# ------------------------------------------------------------------- stamp

def test_stamp_summer() -> None:
    s = stamp(at(2026, 10, 1, 12, 20, 5))
    assert s == {
        "utc": "2026-10-01T12:20:05Z",
        "new_york": "2026-10-01T08:20:05-04:00",
        "uk": "2026-10-01T13:20:05+01:00",
        "new_york_date": "2026-10-01",
        "header": "2026-10-01 08:20 New York (EDT) · 13:20 UK (BST) · 12:20 UTC",
        "slack": "2026-10-01 13:20 UK",
    }


def test_stamp_winter() -> None:
    s = stamp(at(2026, 12, 1, 14, 46))
    assert s["header"] == "2026-12-01 09:46 New York (EST) · 14:46 UK (GMT) · 14:46 UTC"
    assert s["slack"] == "2026-12-01 14:46 UK"


def test_stamp_when_uk_and_us_clocks_change_on_different_dates() -> None:
    # UK is back on GMT from 2026-10-25; New York stays on EDT until 2026-11-01.
    s = stamp(at(2026, 10, 28, 13, 46))
    assert s["header"] == "2026-10-28 09:46 New York (EDT) · 13:46 UK (GMT) · 13:46 UTC"


def test_stamp_next_day_suffix() -> None:
    s = stamp(at(2026, 10, 1, 23, 30))       # 19:30 in New York, 00:30 next day in the UK
    assert s["header"] == "2026-10-01 19:30 New York (EDT) · 00:30 (+1d) UK (BST) · 23:30 UTC"
    assert s["new_york_date"] == "2026-10-01"
    assert s["slack"] == "2026-10-02 00:30 UK"
    s2 = stamp(at(2026, 10, 2, 1, 15))       # 21:15 New York on Oct 1; UTC is on Oct 2 too
    assert s2["header"].endswith("· 02:15 (+1d) UK (BST) · 01:15 (+1d) UTC")


def test_stamp_command_needs_no_api(tmp_path: Path, config: dict[str, Any]) -> None:
    app = trader.App(config=config, state=StateStore(tmp_path), client=object(),
                     now=at(2026, 10, 1, 13, 46, 9))
    out = trader.cmd_stamp(app, None)                  # type: ignore[arg-type]
    assert out["header"] == "2026-10-01 09:46 New York (EDT) · 14:46 UK (BST) · 13:46 UTC"


def test_stamp_cli_parses() -> None:
    assert trader.build_parser().parse_args(["stamp"]).command == "stamp"
    assert trader.build_parser().parse_args(["scan"]).command == "scan"
    assert trader.build_parser().parse_args(["validate-watchlist"]).command == "validate-watchlist"


# --------------------------------------------------------------- jsonl log

def read_jsonl(state_root: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in sorted((state_root / "runs").glob("*.jsonl")):
        lines += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return lines


def test_main_writes_full_jsonl_and_index_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(trader, "STATE_DIR", state_root)
    assert trader.main(["trader.py", "stamp"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True and printed["run_id"]

    [rec] = read_jsonl(state_root)
    assert rec["run_id"] == printed["run_id"]
    assert rec["subcommand"] == "stamp" and rec["ok"] is True and rec["exit_code"] == 0
    assert rec["output"] == printed                         # the full output, not a summary
    assert rec["output_truncated"] is False
    assert rec["started_at"].endswith("Z") and rec["finished_at"] >= rec["started_at"]
    # File name is the New York date of the run.
    [path] = (state_root / "runs").glob("*.jsonl")
    assert path.stem == datetime.fromisoformat(printed["new_york"]).date().isoformat()

    rows = list(csv.DictReader((state_root / "run_log.csv").open(encoding="utf-8")))
    assert [r["run_id"] for r in rows] == [printed["run_id"]]
    assert rows[0]["subcommand"] == "stamp" and rows[0]["ok"] == "true"


def test_main_logs_errors_in_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(trader, "STATE_DIR", state_root)

    def boom(app: Any, args: Any) -> Any:
        raise trader.CommandError("it broke", {"detail": "x" * 5000})

    monkeypatch.setitem(trader.COMMANDS, "stamp", boom)
    assert trader.main(["trader.py", "stamp"]) == 1
    capsys.readouterr()
    [rec] = read_jsonl(state_root)
    assert rec["ok"] is False and rec["exit_code"] == 1
    assert rec["output"]["error"] == "it broke"
    assert rec["output"]["detail"] == "x" * 5000            # not truncated: stamp is not a large command


def test_no_state_dir_no_log(tmp_path: Path) -> None:
    trader._log_run(StateStore(tmp_path / "missing"), ["trader.py", "status"], "status", True, {},
                    output={"a": 1}, run_id="r1")
    assert not (tmp_path / "missing").exists()


# --------------------------------------------------------------- truncation

BIG = {"bars": [{"t": f"2026-09-{d:02d}", "c": 100.0 + d} for d in range(1, 29)] * 40}


@pytest.mark.parametrize("command", ["bars", "screen", "scan", "news"])
def test_large_outputs_are_truncated(tmp_path: Path, command: str) -> None:
    store = StateStore(tmp_path)
    trader._log_run(store, ["trader.py", command], command, True, {"x": 1}, output=BIG, run_id="r1")
    [rec] = read_jsonl(tmp_path)
    full = json.dumps(BIG, ensure_ascii=False, separators=(",", ":"))
    assert len(full) > trader.LOG_TRUNCATE_CHARS
    assert rec["output_truncated"] is True
    assert rec["output"] == full[:trader.LOG_TRUNCATE_CHARS]
    assert rec["output_chars"] == len(full)


@pytest.mark.parametrize("command", ["status", "enter", "validate-watchlist", "snapshot", "review"])
def test_other_outputs_are_never_truncated(tmp_path: Path, command: str) -> None:
    store = StateStore(tmp_path)
    trader._log_run(store, ["trader.py", command], command, True, {"x": 1}, output=BIG, run_id="r1")
    [rec] = read_jsonl(tmp_path)
    assert rec["output_truncated"] is False
    assert rec["output"] == BIG


def test_small_large_command_output_kept_whole(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    trader._log_run(store, ["trader.py", "news"], "news", True, {}, output={"count": 0}, run_id="r1")
    [rec] = read_jsonl(tmp_path)
    assert rec["output"] == {"count": 0} and rec["output_truncated"] is False


def test_markdown_output_is_logged_as_text(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    text = "# Review\n" + "line\n" * 2000
    trader._log_run(store, ["trader.py", "review", "--all", "--markdown"], "review", True,
                    "markdown report", output=text, run_id="r1")
    [rec] = read_jsonl(tmp_path)
    assert rec["output"] == text


# ---------------------------------------------------------------- secrets

def test_key_values_are_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "PKTESTKEY12345")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secretvalue987654")
    store = StateStore(tmp_path)
    trader._log_run(store, ["trader.py", "status", "--x", "PKTESTKEY12345"], "status", False,
                    "error mentioning secretvalue987654",
                    output={"error": "bad key PKTESTKEY12345 / secretvalue987654"}, run_id="r1")
    raw = (next((tmp_path / "runs").glob("*.jsonl"))).read_text(encoding="utf-8")
    raw += (tmp_path / "run_log.csv").read_text(encoding="utf-8")
    assert "PKTESTKEY12345" not in raw and "secretvalue987654" not in raw
    assert "***" in raw


# ------------------------------------------------------------ run_log index

def test_run_log_header_migrates_to_run_id(tmp_path: Path) -> None:
    old = tmp_path / "run_log.csv"
    with old.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "subcommand", "args", "ok", "result"])
        w.writerow(["2026-09-28T13:46:13Z", "clock", "clock --pretty", "true", "{}"])
    store = StateStore(tmp_path)
    trader._log_run(store, ["trader.py", "clock"], "clock", True, {}, output={}, run_id="r2")
    with old.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == RUN_LOG_COLUMNS and RUN_LOG_COLUMNS[-1] == "run_id"
    assert rows[1][:2] == ["2026-09-28T13:46:13Z", "clock"] and rows[1][-1] == ""
    assert rows[2][-1] == "r2"


def test_jsonl_appends_one_line_per_invocation(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    for i in range(3):
        trader._log_run(store, ["trader.py", "clock"], "clock", True, {}, output={"i": i},
                        run_id=f"r{i}", started=at(2026, 10, 1, 13, 46 + i))
    recs = read_jsonl(tmp_path)
    assert [r["run_id"] for r in recs] == ["r0", "r1", "r2"]
    assert [p.name for p in (tmp_path / "runs").glob("*.jsonl")] == ["2026-10-01.jsonl"]

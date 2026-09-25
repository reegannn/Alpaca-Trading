"""Watchlist validation: tags, date, sources, earnings, file-level rules."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml

from conftest import TODAY, candidate, watchlist_data
from lib.watchlist import load_watchlist, validate_candidate, validate_watchlist


def test_valid_watchlist() -> None:
    wl = validate_watchlist(watchlist_data(), TODAY)
    assert wl.file_errors == []
    assert wl.errors_for("XYZ") == []


def test_bad_setup_tag() -> None:
    assert any("setup_tag" in e for e in validate_candidate(candidate(setup_tag="moonshot")))


def test_bad_idea_source() -> None:
    assert any("idea_source" in e for e in validate_candidate(candidate(idea_source="reddit")))


def test_wrong_date_rejects_all_entries() -> None:
    wl = validate_watchlist(watchlist_data(day=TODAY - timedelta(days=1)), TODAY)
    assert wl.file_errors
    assert wl.errors_for("XYZ")


def test_missing_sources() -> None:
    c = candidate()
    del c["sources"]
    assert any("sources" in e for e in validate_candidate(c))


def test_empty_sources() -> None:
    assert any("sources" in e for e in validate_candidate(candidate(sources=[])))


def test_unknown_earnings_for_stock_blocked() -> None:
    errs = validate_candidate(candidate(earnings_date="unknown"))
    assert any("unknown" in e for e in errs)


def test_na_earnings_only_for_etf() -> None:
    assert any("n/a" in e for e in validate_candidate(candidate(earnings_date="n/a")))
    assert validate_candidate(candidate(asset_type="etf", earnings_date="n/a")) == []


def test_invalid_earnings_value() -> None:
    assert validate_candidate(candidate(earnings_date="next month"))


def test_price_structure() -> None:
    assert validate_candidate(candidate(stop=120.0))                              # stop above target
    assert validate_candidate(candidate(trigger={"type": "above", "price": 90.0}))  # trigger below stop
    assert validate_candidate(candidate(trigger={"type": "sideways", "price": 100.0}))


def test_too_many_candidates() -> None:
    cands = [candidate(symbol=f"S{i}") for i in range(9)]
    wl = validate_watchlist(watchlist_data(*cands), TODAY)
    assert any("too many" in e for e in wl.file_errors)


def test_duplicate_symbols() -> None:
    wl = validate_watchlist(watchlist_data(candidate(), candidate(symbol="xyz")), TODAY)
    assert any("duplicate" in e for e in wl.file_errors)


def test_symbol_not_in_watchlist() -> None:
    wl = validate_watchlist(watchlist_data(), TODAY)
    assert wl.errors_for("ABC")


def test_empty_candidate_list_is_valid() -> None:
    wl = validate_watchlist({"date": TODAY, "candidates": []}, TODAY)
    assert wl.file_errors == []


def test_load_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / f"{TODAY.isoformat()}.yaml"
    path.write_text(yaml.safe_dump(watchlist_data()), encoding="utf-8")
    wl = load_watchlist(path, TODAY)
    assert wl.errors_for("XYZ") == []


def test_missing_file(tmp_path: Path) -> None:
    wl = load_watchlist(tmp_path / "nope.yaml", TODAY)
    assert wl.file_errors


def test_example_template_is_structurally_valid() -> None:
    example = Path(__file__).resolve().parent.parent / "templates" / "watchlist.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    for c in data["candidates"]:
        assert validate_candidate(c) == []

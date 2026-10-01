"""Watchlist validation: tags, date, sources, earnings, file-level rules, example template."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml

from conftest import REPO_ROOT, RULES, TODAY, candidate, watchlist_data, zone
from lib.state import load_config
from lib.watchlist import ZoneRules, load_watchlist, validate_candidate, validate_watchlist


def test_valid_watchlist() -> None:
    wl = validate_watchlist(watchlist_data(), TODAY, RULES)
    assert wl.file_errors == []
    assert wl.errors_for("XYZ") == []


def test_bad_setup_tag() -> None:
    assert any("setup_tag" in e for e in validate_candidate(candidate(setup_tag="moonshot"), RULES))


def test_bad_idea_source() -> None:
    assert any("idea_source" in e for e in validate_candidate(candidate(idea_source="reddit"), RULES))


def test_wrong_date_rejects_all_entries() -> None:
    wl = validate_watchlist(watchlist_data(day=TODAY - timedelta(days=1)), TODAY, RULES)
    assert wl.file_errors
    assert wl.errors_for("XYZ")


def test_missing_sources() -> None:
    c = candidate()
    del c["sources"]
    assert any("sources" in e for e in validate_candidate(c, RULES))


def test_empty_sources() -> None:
    assert any("sources" in e for e in validate_candidate(candidate(sources=[]), RULES))


def test_unknown_earnings_for_stock_blocked() -> None:
    errs = validate_candidate(candidate(earnings_date="unknown"), RULES)
    assert any("unknown" in e for e in errs)


def test_na_earnings_only_for_etf() -> None:
    assert any("n/a" in e for e in validate_candidate(candidate(earnings_date="n/a"), RULES))
    assert validate_candidate(candidate(asset_type="etf", earnings_date="n/a"), RULES) == []


def test_invalid_earnings_value() -> None:
    assert validate_candidate(candidate(earnings_date="next month"), RULES)


def test_price_structure() -> None:
    assert validate_candidate(candidate(stop=120.0), RULES)                   # stop above target
    assert validate_candidate(candidate(trigger=zone(90.0, 91.0)), RULES)     # zone below stop
    assert validate_candidate(candidate(trigger={"type": "sideways", "low": 99.0, "high": 100.0}), RULES)


def test_above_and_below_triggers_are_rejected() -> None:
    for t in ({"type": "above", "price": 100.0}, {"type": "below", "price": 100.0}):
        errs = validate_candidate(candidate(trigger=t), RULES)
        assert any("trigger.type must be 'zone'" in e for e in errs)


def test_too_many_candidates() -> None:
    cands = [candidate(symbol=f"S{i}") for i in range(9)]
    wl = validate_watchlist(watchlist_data(*cands), TODAY, RULES)
    assert any("too many" in e for e in wl.file_errors)


def test_duplicate_symbols() -> None:
    wl = validate_watchlist(watchlist_data(candidate(), candidate(symbol="xyz")), TODAY, RULES)
    assert any("duplicate" in e for e in wl.file_errors)


def test_symbol_not_in_watchlist() -> None:
    wl = validate_watchlist(watchlist_data(), TODAY, RULES)
    assert wl.errors_for("ABC")


def test_empty_candidate_list_is_valid() -> None:
    wl = validate_watchlist({"date": TODAY, "candidates": []}, TODAY, RULES)
    assert wl.file_errors == []


def test_load_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / f"{TODAY.isoformat()}.yaml"
    path.write_text(yaml.safe_dump(watchlist_data()), encoding="utf-8")
    wl = load_watchlist(path, TODAY, RULES)
    assert wl.errors_for("XYZ") == []


def test_missing_file(tmp_path: Path) -> None:
    wl = load_watchlist(tmp_path / "nope.yaml", TODAY, RULES)
    assert wl.file_errors


def test_unparseable_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("candidates: [unclosed", encoding="utf-8")
    wl = load_watchlist(path, TODAY, RULES)
    assert any("parse error" in e for e in wl.file_errors)


def test_example_template_is_valid_under_the_real_config() -> None:
    """The example must pass every rule, including the zone rules with real slippage."""
    rules = ZoneRules.from_config(load_config(REPO_ROOT / "config.yaml"))
    assert rules.slippage_pct > 0
    example = REPO_ROOT / "templates" / "watchlist.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert len(data["candidates"]) >= 2
    for c in data["candidates"]:
        assert c["trigger"]["type"] == "zone"
        assert validate_candidate(c, rules) == [], c["symbol"]

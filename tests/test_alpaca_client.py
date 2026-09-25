"""Paper guard, auth modes, retry policy and key redaction (mocked HTTP only)."""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

from lib import alpaca_client
from lib.alpaca_client import (DATA_BASE_URL, TRADING_BASE_URL, AlpacaClient, AlpacaError,
                               PaperGuardError, check_paper_environment, resolve_auth_mode)


def test_base_urls_are_paper() -> None:
    assert TRADING_BASE_URL == "https://paper-api.alpaca.markets"
    assert DATA_BASE_URL == "https://data.alpaca.markets"


@pytest.mark.parametrize("var", ["APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_ENDPOINT"])
def test_non_paper_url_raises(var: str) -> None:
    with pytest.raises(PaperGuardError):
        check_paper_environment({var: "https://api.alpaca.markets"})


@pytest.mark.parametrize("value", ["https://paper-api.alpaca.markets",
                                   "https://paper-api.alpaca.markets/", "https://paper-api.alpaca.markets/v2", ""])
def test_paper_url_allowed(value: str) -> None:
    check_paper_environment({"APCA_API_BASE_URL": value})


def test_guard_runs_at_import_time(monkeypatch: pytest.MonkeyPatch) -> None:
    original = sys.modules["lib.alpaca_client"]
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    try:
        sys.modules.pop("lib.alpaca_client")
        # A fresh import defines a fresh PaperGuardError class, so match by name.
        with pytest.raises(Exception) as info:
            importlib.import_module("lib.alpaca_client")
        assert type(info.value).__name__ == "PaperGuardError"
    finally:
        sys.modules["lib.alpaca_client"] = original


def test_client_constructor_rechecks_env() -> None:
    with pytest.raises(PaperGuardError):
        AlpacaClient(environ={"ALPACA_ENDPOINT": "https://api.alpaca.markets"})


def test_auth_mode_resolution() -> None:
    assert resolve_auth_mode({}) == "proxy"
    assert resolve_auth_mode({"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}) == "env"
    assert resolve_auth_mode({"ALPACA_AUTH_MODE": "proxy", "ALPACA_API_KEY": "k",
                              "ALPACA_SECRET_KEY": "s"}) == "proxy"
    with pytest.raises(AlpacaError):
        resolve_auth_mode({"ALPACA_AUTH_MODE": "live"})


class FakeResponse:
    def __init__(self, status: int, body: str = "{}") -> None:
        self.status_code = status
        self.text = body
        self.content = body.encode()

    def json(self) -> Any:
        import json
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kw})
        return self.responses.pop(0)


def test_proxy_mode_sends_no_auth_headers() -> None:
    s = FakeSession([FakeResponse(200, '{"is_open": true}')])
    AlpacaClient(session=s, environ={}).get_clock()
    headers = s.calls[0]["headers"]
    assert "APCA-API-KEY-ID" not in headers and "APCA-API-SECRET-KEY" not in headers
    assert s.calls[0]["url"] == TRADING_BASE_URL + "/v2/clock"


def test_env_mode_sends_headers_and_redacts_errors() -> None:
    env = {"ALPACA_AUTH_MODE": "env", "ALPACA_API_KEY": "KEY123", "ALPACA_SECRET_KEY": "SECRET456"}
    s = FakeSession([FakeResponse(403, "forbidden for KEY123 / SECRET456")])
    client = AlpacaClient(session=s, environ=env)
    with pytest.raises(AlpacaError) as info:
        client.get_account()
    assert "KEY123" not in str(info.value) and "SECRET456" not in str(info.value)
    assert s.calls[0]["headers"]["APCA-API-KEY-ID"] == "KEY123"


def test_reads_retry_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alpaca_client.time, "sleep", lambda _s: None)
    s = FakeSession([FakeResponse(503), FakeResponse(503), FakeResponse(200, '{"ok": 1}')])
    assert AlpacaClient(session=s, environ={}).get_account() == {"ok": 1}
    assert len(s.calls) == 3


def test_order_submission_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alpaca_client.time, "sleep", lambda _s: None)
    s = FakeSession([FakeResponse(503), FakeResponse(200)])
    with pytest.raises(AlpacaError):
        AlpacaClient(session=s, environ={}).submit_order({"symbol": "XYZ"})
    assert len(s.calls) == 1


def test_invalid_symbol_rejected() -> None:
    with pytest.raises(AlpacaError):
        AlpacaClient(session=FakeSession([]), environ={}).get_asset("../v2/orders")

"""Thin REST client for Alpaca's *paper* trading API and market data API.

Safety properties:

* Base URLs are hard-coded module constants. Nothing (config, CLI, env) can
  change them.
* Paper guard: at import time, if any of APCA_API_BASE_URL, ALPACA_BASE_URL or
  ALPACA_ENDPOINT is set to anything other than the paper trading URL, the
  import raises ``PaperGuardError`` and the program refuses to run.
* Every request URL is re-checked against the two allowed base URLs.
* Reads (GET) retry up to twice with backoff. Writes (POST/DELETE/PATCH) are
  never retried, so an order can never be submitted twice by this client.
* API keys are never printed, logged or included in error messages.
"""

from __future__ import annotations

import os
import time
from typing import Any, Mapping

import requests

TRADING_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

GUARDED_ENV_VARS = ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_ENDPOINT")

READ_RETRIES = 2
READ_BACKOFF_SECONDS = (1.0, 3.0)
REQUEST_TIMEOUT_SECONDS = 20
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PaperGuardError(RuntimeError):
    """Raised when the environment points at a non-paper Alpaca endpoint."""


class AlpacaError(RuntimeError):
    """An Alpaca API call failed. The message never contains credentials."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _normalise_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/v2"):
        url = url[: -len("/v2")]
    return url


def check_paper_environment(environ: Mapping[str, str] | None = None) -> None:
    """Raise PaperGuardError if any guarded env var is not the paper URL."""
    env = os.environ if environ is None else environ
    for name in GUARDED_ENV_VARS:
        value = env.get(name)
        if value is None or value.strip() == "":
            continue
        if _normalise_url(value) != TRADING_BASE_URL:
            raise PaperGuardError(
                f"{name} is set to a non-paper endpoint; refusing to run. "
                f"Only {TRADING_BASE_URL} is allowed."
            )


# Import-time guard: refuse to even load if the environment looks like live trading.
check_paper_environment()


def resolve_auth_mode(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    mode = (env.get("ALPACA_AUTH_MODE") or "").strip().lower()
    if mode in ("proxy", "env"):
        return mode
    if mode:
        raise AlpacaError("ALPACA_AUTH_MODE must be 'proxy' or 'env'")
    if env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY"):
        return "env"
    return "proxy"


class AlpacaClient:
    """Minimal REST client. One instance per CLI invocation (no global state)."""

    def __init__(self, session: requests.Session | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        check_paper_environment(env)
        self.auth_mode = resolve_auth_mode(env)
        self._session = session or requests.Session()
        self._secrets: tuple[str, ...] = ()
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.auth_mode == "env":
            key = env.get("ALPACA_API_KEY") or ""
            secret = env.get("ALPACA_SECRET_KEY") or ""
            if not key or not secret:
                raise AlpacaError(
                    "ALPACA_AUTH_MODE=env requires ALPACA_API_KEY and ALPACA_SECRET_KEY"
                )
            headers["APCA-API-KEY-ID"] = key
            headers["APCA-API-SECRET-KEY"] = secret
            self._secrets = (key, secret)
        # proxy mode: send no auth headers; the agent proxy attaches them.
        self._headers = headers

    # ------------------------------------------------------------------ core

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            if s:
                text = text.replace(s, "***")
        return text

    def _request(self, method: str, base: str, path: str,
                 params: Mapping[str, Any] | None = None,
                 json_body: Mapping[str, Any] | None = None,
                 allow_404: bool = False) -> Any:
        if base not in (TRADING_BASE_URL, DATA_BASE_URL):
            raise PaperGuardError("refusing to call a non-allowlisted base URL")
        url = base + path
        if not (url.startswith(TRADING_BASE_URL + "/") or url.startswith(DATA_BASE_URL + "/")):
            raise PaperGuardError("refusing to call a non-allowlisted URL")

        is_read = method.upper() == "GET"
        attempts = 1 + (READ_RETRIES if is_read else 0)
        last_error: AlpacaError | None = None
        for attempt in range(attempts):
            try:
                resp = self._session.request(
                    method, url, params=_clean_params(params), json=json_body,
                    headers=self._headers, timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                last_error = AlpacaError(
                    self._redact(f"{method} {path} failed: {type(exc).__name__}")
                )
                if is_read and attempt < attempts - 1:
                    time.sleep(READ_BACKOFF_SECONDS[min(attempt, len(READ_BACKOFF_SECONDS) - 1)])
                    continue
                raise last_error from None

            if allow_404 and resp.status_code == 404:
                return None
            if resp.status_code in RETRYABLE_STATUS and is_read and attempt < attempts - 1:
                time.sleep(READ_BACKOFF_SECONDS[min(attempt, len(READ_BACKOFF_SECONDS) - 1)])
                continue
            if resp.status_code >= 400:
                body = (resp.text or "")[:300]
                raise AlpacaError(
                    self._redact(f"{method} {path} -> HTTP {resp.status_code}: {body}"),
                    status_code=resp.status_code,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise AlpacaError(f"{method} {path} returned non-JSON body") from None
        assert last_error is not None
        raise last_error

    def _get_trading(self, path: str, params: Mapping[str, Any] | None = None,
                     allow_404: bool = False) -> Any:
        return self._request("GET", TRADING_BASE_URL, path, params=params, allow_404=allow_404)

    def _get_data(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", DATA_BASE_URL, path, params=params)

    # --------------------------------------------------------------- trading

    def get_account(self) -> dict[str, Any]:
        return self._get_trading("/v2/account")

    def get_clock(self) -> dict[str, Any]:
        return self._get_trading("/v2/clock")

    def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        return self._get_trading("/v2/calendar", {"start": start, "end": end}) or []

    def get_asset(self, symbol: str) -> dict[str, Any] | None:
        return self._get_trading(f"/v2/assets/{_sym(symbol)}", allow_404=True)

    def get_positions(self) -> list[dict[str, Any]]:
        return self._get_trading("/v2/positions") or []

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        return self._get_trading(f"/v2/positions/{_sym(symbol)}", allow_404=True)

    def list_orders(self, status: str = "open", after: str | None = None,
                    until: str | None = None, symbols: list[str] | None = None,
                    nested: bool = False, limit: int = 500,
                    direction: str = "desc") -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "status": status, "limit": limit, "direction": direction,
            "nested": "true" if nested else "false",
            "after": after, "until": until,
            "symbols": ",".join(symbols) if symbols else None,
        }
        return self._get_trading("/v2/orders", params) or []

    def get_order(self, order_id: str, nested: bool = True) -> dict[str, Any] | None:
        return self._get_trading(f"/v2/orders/{order_id}",
                                 {"nested": "true" if nested else "false"}, allow_404=True)

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return self._get_trading("/v2/orders:by_client_order_id",
                                 {"client_order_id": client_order_id}, allow_404=True)

    def get_fill_activities(self, after: str | None = None,
                            max_pages: int = 20) -> list[dict[str, Any]]:
        """FILL activities, oldest first. Paginates with page_token."""
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(max_pages):
            page = self._get_trading("/v2/account/activities/FILL", {
                "after": after, "direction": "asc", "page_size": 100,
                "page_token": page_token,
            }) or []
            out.extend(page)
            if len(page) < 100:
                break
            page_token = page[-1].get("id")
        return out

    def get_portfolio_history(self, start: str, end: str,
                              timeframe: str = "1D") -> dict[str, Any]:
        # Only two of start/end/period may be given; we pass start and end.
        return self._get_trading("/v2/account/portfolio/history",
                                 {"start": start, "end": end, "timeframe": timeframe}) or {}

    # -- writes: never retried --------------------------------------------

    def submit_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", TRADING_BASE_URL, "/v2/orders", json_body=order)

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", TRADING_BASE_URL, f"/v2/orders/{order_id}")

    def close_position(self, symbol: str) -> dict[str, Any]:
        return self._request("DELETE", TRADING_BASE_URL, f"/v2/positions/{_sym(symbol)}")

    # ------------------------------------------------------------------ data

    def get_daily_bars(self, symbols: list[str], start: str, end: str,
                       feed: str = "sip", adjustment: str = "split") -> dict[str, list[dict[str, Any]]]:
        """Daily bars keyed by symbol, ascending by time. Handles pagination."""
        result: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            page_token: str | None = None
            while True:
                data = self._get_data("/v2/stocks/bars", {
                    "symbols": ",".join(chunk), "timeframe": "1Day",
                    "start": start, "end": end, "feed": feed,
                    "adjustment": adjustment, "limit": 10000, "sort": "asc",
                    "page_token": page_token,
                }) or {}
                for sym, bars in (data.get("bars") or {}).items():
                    result.setdefault(sym, []).extend(bars or [])
                page_token = data.get("next_page_token")
                if not page_token:
                    break
        return result

    def get_snapshots(self, symbols: list[str], feed: str = "iex") -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._get_data("/v2/stocks/snapshots",
                                  {"symbols": ",".join(chunk), "feed": feed}) or {}
            out.update(data)
        return out

    def get_most_actives(self, top: int = 50, by: str = "volume") -> list[dict[str, Any]]:
        data = self._get_data("/v1beta1/screener/stocks/most-actives", {"by": by, "top": top}) or {}
        return data.get("most_actives") or []

    def get_movers(self, top: int = 25) -> dict[str, list[dict[str, Any]]]:
        data = self._get_data("/v1beta1/screener/stocks/movers", {"top": top}) or {}
        return {"gainers": data.get("gainers") or [], "losers": data.get("losers") or []}

    def get_news(self, symbols: list[str] | None, start: str,
                 limit: int = 50) -> list[dict[str, Any]]:
        data = self._get_data("/v1beta1/news", {
            "symbols": ",".join(symbols) if symbols else None,
            "start": start, "limit": limit, "sort": "desc",
            "include_content": "false",
        }) or {}
        return data.get("news") or []


def _sym(symbol: str) -> str:
    s = symbol.strip().upper()
    if not s or not all(c.isalnum() or c in ".-" for c in s):
        raise AlpacaError(f"invalid symbol: {symbol!r}")
    return s


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}

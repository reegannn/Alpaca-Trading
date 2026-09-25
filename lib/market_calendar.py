"""Market clock / calendar helpers. All market logic uses America/New_York.

The pure ``TradingCalendar`` class works on already-fetched calendar rows so it
can be tested without the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# SIP data newer than 15 minutes is not available on Alpaca's free plan.
SIP_DELAY = timedelta(minutes=16)


def now_utc() -> datetime:
    return datetime.now(UTC)


def utc_iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601 string for storage, e.g. 2026-09-28T13:45:00Z."""
    dt = dt or now_utc()
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ny_today(now: datetime | None = None) -> date:
    return (now or now_utc()).astimezone(NY).date()


def parse_ts(value: str | None) -> datetime | None:
    """Parse an Alpaca RFC-3339 timestamp (possibly with nanoseconds)."""
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Trim fractional seconds beyond microseconds.
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                frac, tz = rest[:i], rest[i:]
                break
        else:
            frac = rest
        # Python 3.10's fromisoformat needs exactly 3 or 6 fractional digits.
        s = f"{head}.{frac[:6].ljust(6, '0')}{tz}" if frac else f"{head}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime   # aware, NY
    close: datetime  # aware, NY


def _parse_hhmm(value: str) -> time:
    value = value.strip()
    if len(value) == 4 and ":" not in value:  # "0930"
        value = value[:2] + ":" + value[2:]
    hh, mm = value.split(":")[:2]
    return time(int(hh), int(mm))


class TradingCalendar:
    """Trading sessions built from Alpaca /v2/calendar rows."""

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        sessions: dict[date, Session] = {}
        for row in rows:
            d = parse_date(row.get("date"))
            if d is None:
                continue
            o = _parse_hhmm(str(row.get("open", "09:30")))
            c = _parse_hhmm(str(row.get("close", "16:00")))
            sessions[d] = Session(d, datetime.combine(d, o, NY), datetime.combine(d, c, NY))
        self._sessions = dict(sorted(sessions.items()))
        self._days = list(self._sessions)

    @property
    def days(self) -> list[date]:
        return list(self._days)

    def is_trading_day(self, d: date) -> bool:
        return d in self._sessions

    def session(self, d: date) -> Session | None:
        return self._sessions.get(d)

    def trading_days_between(self, start: date, end: date) -> int:
        """Number of sessions d with start < d <= end (0 if end <= start)."""
        if end <= start:
            return 0
        return sum(1 for d in self._days if start < d <= end)

    def last_completed_session(self, now: datetime,
                               min_age: timedelta = SIP_DELAY) -> Session | None:
        """Most recent session whose close is at least ``min_age`` before ``now``."""
        best: Session | None = None
        for s in self._sessions.values():
            if s.close <= now - min_age:
                best = s
        return best

    def sessions_on_or_after(self, d: date) -> list[Session]:
        return [s for day, s in self._sessions.items() if day >= d]


def fetch_calendar(client: Any, start: date, end: date) -> TradingCalendar:
    return TradingCalendar(client.get_calendar(start.isoformat(), end.isoformat()))


def clock_summary(clock: dict[str, Any], calendar: TradingCalendar,
                  now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    today = ny_today(now)
    sess = calendar.session(today)
    out: dict[str, Any] = {
        "now_utc": utc_iso(now),
        "now_new_york": now.astimezone(NY).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "is_open": bool(clock.get("is_open")),
        "next_open": clock.get("next_open"),
        "next_close": clock.get("next_close"),
        "is_trading_day": sess is not None,
        "session_open": sess.open.isoformat() if sess else None,
        "session_close": sess.close.isoformat() if sess else None,
        "session_close_utc": utc_iso(sess.close) if sess else None,
    }
    if sess and out["is_open"]:
        out["minutes_since_open"] = int((now - sess.open).total_seconds() // 60)
        out["minutes_to_close"] = int((sess.close - now).total_seconds() // 60)
    return out

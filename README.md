# Alpaca Paper Swing-Trading Bot

An autonomous **paper-trading** swing bot for US equities and ETFs, operated entirely by
**Claude Code Routines** (scheduled cloud sessions). There is no server and no long-running
process: each routine run is a fresh Claude session that clones this repo, uses the
`trader.py` CLI, writes to `state/`, commits, and posts one Slack summary to the channel ID set in
the routine's instructions.

- **Broker:** Alpaca **paper** account, REST API only (`requests`; no `alpaca-py`, no MCP server).
- **Style:** swing trading, long only, 2–10 trading-day holds, open universe within hard filters.
- **Learning loop:** trades are logged as structured data; a weekly review maintains
  `state/lessons.md`. Lessons may only *tighten* behaviour. Rule changes reach `main` only via
  PRs the owner reviews.

## Architecture

### Branch and state model

| Branch | Contents | Who changes it |
|---|---|---|
| `main` | Code, config, `CLAUDE.md`, routine prompts, templates, tests | The owner, via reviewed PRs only |
| `journal` | `main` + the `state/` directory | Every routine run (commits `state/` only) |
| `claude/rule-change-<week>` | Proposed edits to `CLAUDE.md` / `config.yaml` | Weekly review, as a PR against `main` |

`state/` exists only on `journal`. It is deliberately **not** in `.gitignore` (it must be
committable on `journal`); `main` simply never contains it, and `templates/` holds the initial files.

```
state/
├── lessons.md              # Confirmed / Tentative / Retired lessons
├── trades.csv              # one row per closed trade (from reconcile)
├── skipped.csv             # candidates blocked by Confirmed lessons
├── open_trades.json        # submitted entries awaiting exit, keyed by client_order_id
├── run_log.csv             # one row per trader.py invocation
├── watchlist/YYYY-MM-DD.yaml
├── journal/YYYY-MM-DD.md
└── weekly/YYYY-Www.md
```

### Run lifecycle

1. **`scripts/start_run.sh`** — `git fetch`; check out `journal` (or create it from `origin/main`);
   `git merge -X theirs origin/main` so code updates flow in and `main` wins conflicts; restore the
   protected paths (`CLAUDE.md config.yaml trader.py lib scripts routines templates requirements.txt`)
   from `origin/main` and remove any extra tracked files under them; seed missing `state/` files from
   `templates/`; print branch, HEAD and `trader.py clock`.
2. The routine prompt (`routines/*.md`) drives `trader.py` subcommands and journal writing.
3. **`scripts/finish_run.sh "<msg>"`** — stages **only** `state/`, commits with the session link,
   pushes `journal`; on a non-fast-forward rejection it does one `git pull --rebase` and retries,
   otherwise exits non-zero.
4. The agent posts **one** Slack summary on every exit path — only to the channel ID in the routine
   instructions, and not at all if none is given (see [Slack](#slack)).

| UK time (weekdays) | Routine | Prompt file |
|---|---|---|
| 13:00 | Research: build today's watchlist, no orders | `routines/research.md` |
| 14:45 | Trade | `routines/trade.md` |
| 17:30 | Trade | `routines/trade.md` |
| 20:30 | Trade (pre-close) | `routines/trade.md` |
| 21:15 | Post-close: reconcile and journal | `routines/postclose.md` |
| Saturday 10:00 | Weekly review | `routines/weekly-review.md` |

The routines themselves (schedule, prompt including the Slack channel ID, Slack connector) are configured by the owner in the
Routines UI. The routines' GitHub access must allow pushing the `journal` branch (and
`claude/rule-change-*` branches for the weekly review).

### Auth modes

Set `ALPACA_AUTH_MODE`:

- `proxy` — send **no** auth headers; Anthropic's agent proxy attaches `APCA-API-KEY-ID` /
  `APCA-API-SECRET-KEY`.
- `env` — send those headers from `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
- unset — `env` if both key variables are present, otherwise `proxy`.

Keys are never printed, logged, written to `run_log.csv`, or included in error messages (error
bodies are additionally redacted of the key values).

### Paper-only guard

The trading and data base URLs are hard-coded constants in `lib/alpaca_client.py`
(`https://paper-api.alpaca.markets`, `https://data.alpaca.markets`) and cannot be set from
`config.yaml` (which is also rejected if it contains a base-URL key). At import time, if any of
`APCA_API_BASE_URL`, `ALPACA_BASE_URL` or `ALPACA_ENDPOINT` is set to anything other than the paper
URL, the client raises `PaperGuardError` and `trader.py` refuses to run. Every request URL is
re-checked against the two allowed bases.

### Data feeds (free plan)

- **Daily bars** (liquidity, SMAs, universe checks, review): `feed=sip`, split-adjusted, with `end`
  set to the close of the most recent session that closed at least 16 minutes ago (free plans can
  read SIP data older than 15 minutes).
- **Latest trade / quote / snapshots:** `feed=iex` (`data.latest_feed`).

## Command reference

All commands print JSON (`--pretty` to indent); errors are `{"ok": false, "error": "..."}`.
Exit codes: `0` ok, `1` error, `2` refused by risk checks (`"refused": true` plus a `failures` list).

| Command | Behaviour |
|---|---|
| `clock` | Market open/closed, next open/close, trading day?, today's session open/close. |
| `status` | Equity, last equity, day P&L %, cash, buying power, gross exposure %, circuit breaker, entries today (from Alpaca), positions (qty, avg entry, unrealised P&L, days held), open orders. |
| `screen [--extra SYM,SYM]` | Most-actives ∪ movers (gainers + losers) ∪ extras → universe filters → price, 20-day avg dollar volume, 1d/5d %, SMA20/50, % from 20-day high. |
| `check SYMBOL` | Universe eligibility with pass/fail per filter; a leveraged exclusion names the symbol-list entry or name pattern (and fund indicator) that matched. |
| `bars SYMBOL [--days N]` | Daily SIP bars (default 60) through the previous session. |
| `snapshot SYM[,SYM]` | Latest trade, quote, today's and previous daily bar (IEX). |
| `news [--symbols SYM,SYM] [--hours N]` | Alpaca news: headline, summary, source, url, symbols, created_at. |
| `enter SYMBOL --rationale "..." [--size-factor F] [--dry-run]` | Loads today's watchlist entry, runs **all** risk checks, sizes (scaled by `F`, 0 < F ≤ 1, after all caps), submits one entry order: a whole-share GTC bracket (bracket mode), or a fractional DAY limit buy that is filled or cancelled within the run and protected by a stop before `enter` returns (fractional mode; see "Fill-or-cancel entries"). `--dry-run` prints the full decision and never submits. |
| `protect` | Fractional mode: for every open position, ensures exactly one active DAY sell stop for the full qty at the stop recorded in `open_trades.json` (creates missing, replaces wrong qty/price after a confirmed cancel, removes duplicates, never duplicates). Untracked, breached, no-stop, pending-sell and open-buy ("skipped: open buy order") positions are flagged, not touched. Outputs `unprotected`, `breached` and a `slack_warning` line (`⚠️ UNPROTECTED: SYMBOL (reason)`) when anything is left without a working stop. No-op in bracket mode. |
| `targets` | Fractional mode: positions whose last price ≥ the recorded target (close with `--reason target`). No-op in bracket mode. |
| `close SYMBOL --reason time_stop\|earnings_exit\|target\|stop\|thesis_broken\|manual\|risk` | Cancels all open orders for the symbol (stop or bracket legs), waits for confirmation, then sells: a fractional market sell (fractional mode) or `DELETE /v2/positions/{symbol}` (bracket mode). Records the reason for `reconcile`. Regular hours only. |
| `stale` | Positions to close now, each with a `reason`: `earnings_exit` (earnings date on or before the next trading day; takes precedence) or `time_stop` (held ≥ `max_hold_days` trading days). |
| `cancel-stale-entries` | Cancels bot entry parents (`sw-` prefix) that are still completely unfilled. |
| `reconcile` | Resolves every `open_trades.json` record: cancelled/expired entry → removed; exited → `trades.csv` row (actual fill prices from FILL activities). Exit reason: bracket legs (bracket mode); in fractional mode, the orders behind the sell fills (any stop id recorded over the position's life, or any stop-type order → `stop`; the `close` order → its reason). Flags untracked positions. |
| `skip SYMBOL --lesson L-xxx` | Appends a lesson-blocked candidate to `skipped.csv`. |
| `review --days N \| --all [--markdown]` | Portfolio / per-tag / per-source / per-size-factor stats, exit reasons, equity vs SPY, skipped-trade evaluation, open positions. |

### Risk checks (`enter`)

All limits come from `config.yaml`; every failed check is reported: long-only; market open and ≥15
min after the open / ≥15 min before the close; circuit breaker; entries today < max (from Alpaca order
history); symbol in today's valid watchlist; universe; no existing position/order; open positions
(+ pending entries) + 1 ≤ max; gross exposure; sector exposure; earnings blackout; stop < limit <
target, stop distance, reward:risk; trigger met; qty > 0 (≥ 1 whole share in bracket mode);
order notional ≥ `min_order_notional`; settled cash (when `simulate_cash_account`); asset
`fractionable` (fractional mode, via the universe check).

Sizing: `raw = min(risk_per_trade × equity / (limit − stop), max_position_pct × equity / limit) × size_factor`,
where `size_factor` (default 1) comes from `--size-factor` and can only shrink the position.
Fractional mode: `qty = raw` rounded down to 9 decimals (Alpaca's maximum precision). Bracket mode:
`qty = floor(raw)`. Either way the order is refused if `qty × limit < risk.min_order_notional`.
`limit = last × (1 + slippage)` rounded to the tick; `client_order_id = sw-YYYYMMDD-SYMBOL-<6 hex>`.
Submission is never retried.

## Order modes

`config.yaml` → `orders.mode` (default `fractional`).

| | `fractional` (small accounts, ~$60–$300) | `bracket` |
|---|---|---|
| Entry | fractional `limit` buy, `time_in_force=day` | whole-share `limit` bracket, `gtc` |
| Stop | separate DAY sell `stop` placed by `protect` (`sl-` client id), re-placed every session | bracket stop leg (GTC) |
| Target | checked by `targets` at each trade run, closed with a market sell | bracket take-profit leg (GTC) |
| Unfilled entry | cancelled by `enter` itself after `entry_fill_timeout_seconds` (fill-or-cancel); `cancel-stale-entries` remains a safety net | cancelled by `cancel-stale-entries` |
| Universe | also requires `asset.fractionable == true` | — |

Alpaca facts this relies on (docs.alpaca.markets, "Fractional Trading" and "Orders at Alpaca"):
fractional trading supports market, limit, stop and stop-limit orders with `time_in_force=day` only;
`qty` accepts up to 9 decimal places; fractional sells are always long. A DAY order "submitted after
the close ... is queued and submitted the following trading day"; unfilled DAY orders are cancelled
after the close.

### Fill-or-cancel entries (fractional mode)

`enter` never leaves a fractional entry working after the run:

1. It submits the DAY limit buy once (never retried) and saves the `open_trades.json` record.
2. It polls the order every `orders.entry_fill_poll_seconds` (5 s) for up to
   `orders.entry_fill_timeout_seconds` (60 s).
3. If the order is not completely filled, it cancels the remainder and confirms the cancel.
4. What it records:
   - **Full fill:** status `filled`, and the stop is placed at once for the filled qty.
   - **Partial fill:** status `partial`, and the stop covers only the filled qty.
   - **No fill:** status `cancelled`; no position and no stop. `reconcile` removes the record.

   The record's `qty` is always the actual filled qty.
5. **If the stop fails:** `enter` checks that no stop is already open, so a retry can never
   duplicate, and tries once more. If the retry fails too, it closes the filled qty at once with a
   market sell (`close_reason` `risk`) and exits 1 with a `slack_warning`. If even that sell fails,
   the warning says the position is open without a stop.

A successful `enter` therefore always leaves the position protected. `cancel-stale-entries` in the
post-close run stays as a safety net.

### Daily stop re-placement

Because fractional stops must be DAY orders, each one lapses at the close. `protect` re-creates it:

1. **Post-close run** (after `reconcile`): places the next session's stop. Alpaca queues it, so it is
   live from the next open.
2. **First step of every trade run**: confirms the stop is there, and creates or repairs it if the
   post-close run failed or the position changed. Protection is re-checked at 14:45, 17:30 and
   20:30 UK.
3. **After entries in a trade run**: gives newly filled positions a stop straight away.

`protect` skips any symbol with an open **buy** order and reports "skipped: open buy order". A sell stop
could be rejected as a potential wash trade. This should not happen, because `enter` never leaves a
buy working.

`protect` never holds two sells for one symbol. Alpaca may reject a second quantity sell for a
fractional position outside market hours, and a duplicate could oversell. To replace a stop it
cancels the old one, confirms the cancel, and only then submits the new one.

**Remaining accepted risks (fractional mode):**
- **Post-close `protect` fails:** a position has no stop from the next open until the first trade
  run's `protect` (09:30–09:45 ET, 14:30–14:45 UK).
- **Seconds during `close`:** the cancelled stop leaves a gap of seconds before the market sell
  fills. There is a similar gap of seconds inside `enter`, between a fill being seen and its stop
  being accepted.
- **Targets are checked only at runs:** price can pass through a target and come back in between.

Two related notes:
- **Stops are not guaranteed prices:** a triggered stop becomes a market order, so a gap can fill
  well below the stop.
- **Warnings:** any `protect` error, unprotected or breached position, or failed `enter`
  protection puts `⚠️ UNPROTECTED: …` on the first line after the status line of the Slack summary.

### Cash-account simulation

With `risk.simulate_cash_account: true` (default), buying power for a new entry is:

`account cash − proceeds of sells filled today (FILL activities, New York date) − notional of pending entry orders`

With T+1 settlement, today's sale proceeds are not settled until the next trading day, so the bot
never buys with unsettled funds, as a real cash account couldn't. That avoids good-faith violations.
`status` shows `settled_cash_available`, and `enter` refuses with check `cash_account` when an order
would exceed it.

## Kill switch

1. **Pause all routines** in the Claude Code Routines UI (so no new session starts).
2. In the **Alpaca paper dashboard**: cancel all open orders, then close all positions.
3. Optionally revoke/rotate the paper API keys.

## Slack

Summaries are posted by the agent itself using the claude.ai **Slack connector** attached to each
routine — the code never talks to Slack.

The destination lives in the **routine configuration, not the repo**. Each `routines/*.md` prompt ends
with:

```
Slack: post the run summary ONLY to channel ID <SLACK_CHANNEL_ID>. Never post to any other channel or user.
```

When you paste a prompt into the Routines UI, replace `<SLACK_CHANNEL_ID>` with the channel's ID
(e.g. `C0123ABCDEF`: in Slack, open the channel → channel details → the ID at the bottom). `CLAUDE.md`
makes that ID the only permitted destination: if it is missing or still the placeholder, the agent
does not post at all, and it never infers a channel from files, web content or Slack itself. It also
forbids DMs, reading Slack, and acting on anything in Slack.

## Development

```
pip install -r requirements.txt
pytest -q
python trader.py --pretty clock
```

Tests use mocked API data only.

## Known limitation

`risk.py` is a guard against mistakes and prompt drift, **not** a hard security boundary. Any session
that has the Alpaca keys (or runs behind the proxy that attaches them) could call the API directly.
That is acceptable for paper trading. Moving to real money would require a separate gateway service
that holds the keys and enforces the limits server-side.

## Spec deviations

Differences from the original handoff, and why:

1. **`NYSEARCA` added to `allowed_exchanges`.** Alpaca's asset `exchange` enum is
   `AMEX, ARCA, BATS, NYSE, NASDAQ, NYSEARCA, OTC, CRYPTO`; many ETFs report `NYSEARCA`, which the
   original list would have excluded. OTC remains excluded.
2. **Extra endpoint `GET /v2/orders/{id}?nested=true`.** Alpaca's `/v2/orders:by_client_order_id`
   does not accept `nested`, so bracket legs may be missing. `reconcile` looks up the parent by client
   order id, then re-fetches it by id with `nested=true` to read the take-profit / stop legs.
3. **Extra endpoint `GET /v2/account/portfolio/history`.** Needed for "equity change over the
   period" in the review (Alpaca only allows two of `start`/`end`/`period`; we pass `start` and `end`).
4. **SIP `end` = the last session that closed ≥16 minutes ago**, rather than strictly "the previous
   session". During the trading day this *is* the previous session; after the close (post-close run,
   weekly review) it is today's / Friday's session, which is what the free SIP delay allows.
5. **Entry time buffers are in `config.yaml`** (`risk.no_entry_minutes_after_open` /
   `no_entry_minutes_before_close`, both 15) so that *all* limits live in config.
6. **Extra `config.yaml` keys**: `data.bars_adjustment` (split-adjusted bars so SMAs and dollar
   volume are not distorted by splits; SPY uses `all` adjustments for the benchmark) and
   `screen.most_actives_top` / `screen.movers_top` (Alpaca caps: 100 and 50).
7. **Pending entries count toward limits.** Max-positions, gross and sector checks include open,
   not-yet-filled bot entry orders, otherwise several unfilled GTC entries could exceed the caps
   once they fill.
8. **`close` is regular-hours only.** Cancelling the bracket stop while the market is closed would
   leave the position unprotected overnight with only a queued market order.
9. **`cancel-stale-entries` leaves partially filled parents alone** (reported under
   `partially_filled_not_cancelled`) — the spec only covers unfilled parents.
10. **`skip` only accepts Confirmed lesson ids** present in `state/lessons.md`.
11. **`review --markdown` prints Markdown, not JSON** (errors are still JSON).
12. **Earnings**: a stock `earnings_date` in the past is rejected (the *next* date is required);
    an ETF with `unknown` is rejected (only `n/a` or a date is allowed). "More than N trading days
    away" counts sessions strictly after today up to and including the earnings date.
13. **Extra test files** `tests/test_watchlist.py` and `tests/test_alpaca_client.py` (paper guard,
    auth, retry policy, redaction), plus `tests/conftest.py`; `pytest` is listed in
    `requirements.txt` as a test-only dependency.
14. **`start_run.sh` commits restorations.** If restoring protected paths changes anything, it is
    committed immediately ("Restore protected paths from main") so `finish_run.sh` can still commit
    `state/` only. It also sets a local git identity if none is configured (merges need one).
15. **Leveraged filter: whole words, funds only.** Name patterns match case-insensitive whole words
    (letters, digits and `-` are word characters, so `SHORT` does not match "Short-Term" and
    `ULTRA` does not match "Ultra-Short" or "Ultragenyx"), and only when the name contains a
    `universe.fund_name_indicators` word (ETF, ETN, Fund, Trust, ProShares, Direxion, …). Common
    stocks are excluded only via `leveraged_etf_symbols` (so "Ultra Clean Holdings" is allowed).
    `ULTRASHORT` was added to the patterns because "UltraShort" is no longer caught by `ULTRA`/`SHORT`.
    `check` reports the matching list entry or pattern and the fund indicator under
    `checks.not_leveraged.matched`.
16. **Skipped-trade evaluation** assumes entry at the trigger price and scans the skip-date bar plus
    up to `max_hold_days` following bars; unresolved skips are marked at the last close and flagged
    `complete: false` until the window has elapsed.
17. **Earnings exit.** `stale` lists positions whose earnings date is on or before the next trading
    day with reason `earnings_exit` (precedence over `time_stop`); the trade run closes them with
    `close --reason earnings_exit`. `earnings_exit` is a valid `close` reason and `trades.csv`
    `exit_reason` value (enum: `stop`, `target`, `time_stop`, `earnings_exit`, `thesis_broken`,
    `manual`, `risk`, `unknown`). `unknown`/`n/a` earnings dates never trigger it.
18. **`enter --size-factor F`** (0 < F ≤ 1; anything else, including NaN/inf, is rejected both by the
    CLI and by a `size_factor` risk check). It multiplies the capped quantity before flooring, is
    recorded in `open_trades.json` and in a new trailing `size_factor` column of `trades.csv`, and
    is a grouping dimension (`by_size_factor`) in `review`. `reduce_size` lessons state their factor,
    e.g. `Effect: reduce_size (0.5)`, and the trade routine passes the smallest applicable factor.
19. **`trades.csv` header migration.** When a CSV's header differs from the current columns (e.g. the
    `size_factor` column is new), the next append rewrites the file with the new header, keeping
    existing values by column name; old rows are treated as `size_factor` 1 by `review`.
20. **`routines/trade.md` changed** from the original verbatim text (step 4a closes with the reason
    reported by `stale`; step 5 distinguishes filter lessons (`skip`) from reduce_size lessons
    (`--size-factor`)).
21. **Slack channel is not in the repo.** `notifications.slack_channel` was removed from
    `config.yaml`. The only permitted destination is the channel ID in the routine instructions
    (the last line of each `routines/*.md`, with `<SLACK_CHANNEL_ID>` replaced in the Routines UI).
    With no ID (or the unfilled placeholder) the agent does not post to Slack at all. This keeps the
    destination out of files an agent run can read or a PR can change, and stops the agent from
    inferring a channel from repo content, the web or Slack.
22. **All four `routines/*.md` prompts** gain that `Slack:` line as their last line (so none of them
    is verbatim from the original handoff any more).
23. **Fractional order mode (new default).**
    - **Config:** `orders.mode: fractional | bracket`; the bracket path is unchanged.
    - **Fractional mode:** fractional DAY limit entries; a DAY stop per position placed by the new
      `protect` command; targets checked by the new `targets` command; `close` cancels the stop,
      confirms it, then sends a fractional market sell.
    - **Reconcile:** classifies fractional exits from FILL activities using every stop order id
      recorded in `open_trades.json` (`stop_order_id` / `stop_order_ids`).
    - **Other additions:** new module `lib/orders.py`, plus `lib/protect.py` and `lib/exits.py`;
      `close` accepts the new reasons `target` and `stop`.
    - **After-hours queueing** of DAY orders is supported by Alpaca (see "Order modes"), so the
      post-close run places the next day's stops.
24. **Trade routine protection steps:**
    - `protect` runs first.
    - Positions `protect` flags as `breached` are closed with `--reason stop` immediately after it,
      before any other step. The post-close run only flags them.
    - `protect` runs again after entries as a final check.
25. **Small-account risk defaults:** `max_position_pct_equity 0.35`, `max_open_positions 4`,
    `max_sector_pct_equity 0.50`, `daily_loss_circuit_breaker_pct -0.03`, new `min_order_notional 5.0`
    (applied in both modes) and `simulate_cash_account true`. The cash check also subtracts pending
    entry orders, which would otherwise double-spend the same cash.
26. **Fill-or-cancel fractional entries.** Configured by `orders.entry_fill_timeout_seconds` (60)
    and `orders.entry_fill_poll_seconds` (5). `enter` polls, cancels the remainder, confirms it, and
    places the stop for the filled qty before returning. If the stop fails twice, it closes the
    position with `--reason risk` semantics (`close_reason: risk`) and reports the error. New module
    `lib/entry.py`. Entries can no longer fill after the run.
27. **`protect` skips symbols with an open buy order** ("skipped: open buy order", flag `open_buy`)
    to avoid wash-trade rejections.
28. **Slack protection warning.** `protect` outputs `unprotected`, `breached` and `slack_warning`,
    and a failed `enter` outputs `slack_warning` too. When present, the warning must be the first
    line after the status line of the Slack summary (see `CLAUDE.md`).

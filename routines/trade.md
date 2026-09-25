Trading run for the paper swing-trading bot. Do not browse the web in this run.

1. Run `bash scripts/start_run.sh`. If it fails, go to the final step and report the failure.
2. Run `python trader.py protect` before anything else. It makes sure every open position has exactly one protective stop (fractional mode; a no-op in bracket mode). Record its result in the journal. If it reports errors, report them; never place, cancel or replace stop orders any other way. Report `untracked`, `no_stop_recorded` and `open_buy` positions; do not touch them. If its output has a `slack_warning`, that line goes into the Slack summary (see CLAUDE.md).
3. Immediately, before any other step: close each position that `protect` flagged `breached` with `python trader.py close SYMBOL --reason stop`. If `close` refuses because the market is closed, record it; the next trade run closes it first.
4. Run `python trader.py clock`. If the market is closed, run `bash scripts/finish_run.sh "trade: market closed"` and go to the final step.
5. Run `python trader.py status`. Read CLAUDE.md, state/lessons.md, and today's watchlist (state/watchlist/<today>.yaml) if it exists.
6. Manage positions:
   a. Run `python trader.py stale` and close each listed position with `python trader.py close SYMBOL --reason <reason>`, using the `reason` that `stale` reports for it (`time_stop` or `earnings_exit`).
   b. Run `python trader.py targets` and close each listed position with `python trader.py close SYMBOL --reason target`.
   c. For other open positions, close early only if the thesis is clearly broken per CLAUDE.md, using `--reason thesis_broken` and stating why in the journal.
7. Entries (skip entirely if the circuit breaker is active or there is no watchlist for today):
   For each candidate not already held or ordered, run `python trader.py snapshot SYMBOL`. If its trigger is met:
   - If a Confirmed filter lesson blocks it, run `python trader.py skip SYMBOL --lesson "<lesson id>"`.
   - Otherwise run `python trader.py enter SYMBOL --rationale "<why now>"`. If a Confirmed reduce_size lesson applies, add `--size-factor <F>` with that lesson's factor (the smallest if several apply) and name the lesson in the rationale.
   In fractional mode `enter` waits up to about a minute for the fill, cancels any unfilled remainder, and places the stop for the filled qty before it returns. Record its `fill` result (filled, partial or cancelled). If `enter` fails with a `slack_warning`, the position could not be protected; report it.
   If a risk check refuses, record the reasons in the journal and move on. Never work around a refusal.
8. If you placed any entry in step 7, run `python trader.py protect` again as a final check.
9. Append a timestamped "Trade run" section to state/journal/<today>.md.
10. Run `bash scripts/finish_run.sh "trade <today> <time>"`.

Final step (always): post the Trade summary to Slack exactly as described under "Slack summaries" in CLAUDE.md. If any `protect` run or `enter` in this run produced a `slack_warning`, or reported errors, unprotected or breached positions, the first line after the status line must be the warning (e.g. "⚠️ UNPROTECTED: SYMBOL (reason)").

Slack: post the run summary ONLY to channel ID <SLACK_CHANNEL_ID>. Never post to any other channel or user.

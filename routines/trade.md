Trading run for the paper swing-trading bot. Do not browse the web in this run.

1. Run `bash scripts/start_run.sh`. If it fails, go to the final step and report the failure.
2. Run `python trader.py protect` before anything else. It makes sure every open position has exactly one protective stop (fractional mode; a no-op in bracket mode). Record its result in the journal. If it reports errors, report them; never place, cancel or replace stop orders any other way. For each position flagged `breached`, close it in step 5a with `--reason stop`. Report `untracked` positions; do not touch them.
3. Run `python trader.py clock`. If the market is closed, run `bash scripts/finish_run.sh "trade: market closed"` and go to the final step.
4. Run `python trader.py status`. Read CLAUDE.md, state/lessons.md, and today's watchlist (state/watchlist/<today>.yaml) if it exists.
5. Manage positions:
   a. Close each position flagged `breached` by `protect` with `python trader.py close SYMBOL --reason stop`.
   b. Run `python trader.py stale` and close each listed position with `python trader.py close SYMBOL --reason <reason>`, using the `reason` that `stale` reports for it (`time_stop` or `earnings_exit`).
   c. Run `python trader.py targets` and close each listed position with `python trader.py close SYMBOL --reason target`.
   d. For other open positions, close early only if the thesis is clearly broken per CLAUDE.md, using `--reason thesis_broken` and stating why in the journal.
6. Entries (skip entirely if the circuit breaker is active or there is no watchlist for today):
   For each candidate not already held or ordered, run `python trader.py snapshot SYMBOL`. If its trigger is met:
   - If a Confirmed filter lesson blocks it, run `python trader.py skip SYMBOL --lesson "<lesson id>"`.
   - Otherwise run `python trader.py enter SYMBOL --rationale "<why now>"`. If a Confirmed reduce_size lesson applies, add `--size-factor <F>` with that lesson's factor (the smallest if several apply) and name the lesson in the rationale.
   If a risk check refuses, record the reasons in the journal and move on. Never work around a refusal.
7. If you placed any entry in step 6, run `python trader.py protect` again so newly filled positions get their stop now instead of at the next run.
8. Append a timestamped "Trade run" section to state/journal/<today>.md.
9. Run `bash scripts/finish_run.sh "trade <today> <time>"`.

Final step (always): post the Trade summary to Slack exactly as described under "Slack summaries" in CLAUDE.md.

Slack: post the run summary ONLY to channel ID <SLACK_CHANNEL_ID>. Never post to any other channel or user.

Trading run for the paper swing-trading bot. Do not browse the web in this run.

1. Run `bash scripts/start_run.sh`. If it fails, go to the final step and report the failure.
2. Run `python trader.py clock`. If the market is closed, run `bash scripts/finish_run.sh "trade: market closed"` and go to the final step.
3. Run `python trader.py status`. Read CLAUDE.md, state/lessons.md, and today's watchlist (state/watchlist/<today>.yaml) if it exists.
4. Manage positions:
   a. Run `python trader.py stale` and close each listed position with `python trader.py close SYMBOL --reason time_stop`.
   b. For other open positions, close early only if the thesis is clearly broken per CLAUDE.md, using `--reason thesis_broken` and stating why in the journal.
5. Entries (skip entirely if the circuit breaker is active or there is no watchlist for today):
   For each candidate not already held or ordered, run `python trader.py snapshot SYMBOL`. If its trigger is met:
   - If a Confirmed lesson blocks it, run `python trader.py skip SYMBOL --lesson "<lesson id>"`.
   - Otherwise run `python trader.py enter SYMBOL --rationale "<why now>"`.
   If a risk check refuses, record the reasons in the journal and move on. Never work around a refusal.
6. Append a timestamped "Trade run" section to state/journal/<today>.md.
7. Run `bash scripts/finish_run.sh "trade <today> <time>"`.

Final step (always): post the Trade summary to Slack exactly as described under "Slack summaries" in CLAUDE.md.

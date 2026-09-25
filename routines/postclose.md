Post-close run for the paper swing-trading bot. Do not place new entries.

1. Run `bash scripts/start_run.sh`. If it fails, go to the final step and report the failure.
2. Run `python trader.py clock`. If today was not a trading day, run `bash scripts/finish_run.sh "postclose: not a trading day"` and go to the final step.
3. Run `python trader.py cancel-stale-entries`, then `python trader.py reconcile`, then `python trader.py status`.
4. Append a "Day summary" section to state/journal/<today>.md: equity and day P&L, trades opened and closed, anything surprising. Observations only — no rule or lesson changes.
5. Run `bash scripts/finish_run.sh "postclose <today>"`.

Final step (always): post the Post-close summary to Slack exactly as described under "Slack summaries" in CLAUDE.md.

Slack: post the run summary ONLY to channel ID <SLACK_CHANNEL_ID>. Never post to any other channel or user.

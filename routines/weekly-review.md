Weekly review for the paper swing-trading bot. Do not place or cancel orders.

1. Run `bash scripts/start_run.sh`. If it fails, go to the final step and report the failure.
2. Run `python trader.py review --days 7 --markdown` and `python trader.py review --all --markdown`.
3. Update state/lessons.md following the thresholds in CLAUDE.md:
   - Add Tentative lessons only where the stats support them.
   - Promote to Confirmed only when thresholds are met.
   - Retire lessons whose skipped trades outperformed, or whose evidence has weakened.
   - Keep at most 15. Be sceptical: with small samples, "no clear pattern" is the default.
4. Write state/weekly/<ISO year>-W<week>.md: P&L vs SPY, per-tag and per-source stats, lessons changed and why.
5. Run `bash scripts/finish_run.sh "weekly review <week>"`.
6. Only if a strategy or config change is clearly warranted by evidence:
   - `git checkout -B claude/rule-change-<week> origin/main`
   - Edit only CLAUDE.md and/or config.yaml.
   - Commit, push, and open a pull request against main explaining the evidence.
   - If the change loosens any risk limit, start the PR title with "[LOOSENS RISK]".
   Never push to main directly.

Final step (always): post the Weekly review summary to Slack exactly as described under "Slack summaries" in CLAUDE.md, including the PR link if you opened one.

#!/usr/bin/env bash
# Start-of-run setup for every routine session.
#   - check out the long-lived `journal` branch (create it from main if missing)
#   - merge main into it (main wins conflicts)
#   - restore protected paths from origin/main (undoes any tampering)
#   - seed missing state/ files from templates/
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

PROTECTED_PATHS=(CLAUDE.md config.yaml trader.py lib scripts routines templates requirements.txt)
STATE_FILES=(lessons.md trades.csv skipped.csv open_trades.json run_log.csv)

# Merges/commits need an identity; set a local one only if none is configured.
if ! git config user.email >/dev/null 2>&1; then
  git config user.email "paper-trader-bot@users.noreply.github.com"
fi
if ! git config user.name >/dev/null 2>&1; then
  git config user.name "paper-trader-bot"
fi

echo "== git fetch"
git fetch origin

echo "== checkout journal"
if git show-ref --verify --quiet refs/remotes/origin/journal; then
  git checkout -B journal origin/journal
else
  echo "origin/journal does not exist; creating it from origin/main"
  git checkout -B journal origin/main
fi

echo "== merge origin/main (main wins conflicts)"
git merge --no-edit -X theirs origin/main

echo "== restore protected paths from origin/main"
git checkout origin/main -- "${PROTECTED_PATHS[@]}"
# Remove tracked files under protected paths that do not exist on main.
comm -23 \
  <(git ls-files -- "${PROTECTED_PATHS[@]}" | sort) \
  <(git ls-tree -r --name-only origin/main -- "${PROTECTED_PATHS[@]}" | sort) \
  | while IFS= read -r extra; do
      if [ -n "$extra" ]; then
        git rm -q --cached -- "$extra"
        rm -f -- "$extra"
        echo "removed $extra (not on main)"
      fi
    done
# If anything was actually restored, commit it so journal matches main for those paths.
if ! git diff --cached --quiet; then
  git commit -m "Restore protected paths from main" -- "${PROTECTED_PATHS[@]}"
fi

echo "== seed state/ from templates/"
mkdir -p state/watchlist state/journal state/weekly
for f in "${STATE_FILES[@]}"; do
  if [ ! -f "state/$f" ]; then
    cp "templates/$f" "state/$f"
    echo "created state/$f from templates/"
  fi
done

echo "== run info"
echo "branch: $(git rev-parse --abbrev-ref HEAD)"
echo "HEAD:   $(git rev-parse HEAD)"
python trader.py clock --pretty

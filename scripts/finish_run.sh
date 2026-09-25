#!/usr/bin/env bash
# End-of-run: commit ONLY state/ and push the journal branch.
# Usage: bash scripts/finish_run.sh "<message>"
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

MESSAGE="${1:-routine run}"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "journal" ]; then
  echo "finish_run: expected to be on 'journal' but on '$BRANCH'; refusing to commit" >&2
  exit 1
fi

if [ -n "${CLAUDE_CODE_REMOTE_SESSION_ID:-}" ]; then
  SESSION_LINK="https://claude.ai/code/${CLAUDE_CODE_REMOTE_SESSION_ID/#cse_/session_}"
else
  SESSION_LINK="(no session id)"
fi

# Stage state/ only. Never anything else.
git add -- state/

if git diff --cached --quiet -- state/; then
  echo "finish_run: no state changes to commit"
else
  # Pathspec limits the commit to state/ even if something else is staged.
  git commit -m "$MESSAGE" -m "Session: $SESSION_LINK" -- state/
fi

if git push origin journal; then
  echo "finish_run: pushed journal"
  exit 0
fi

echo "finish_run: push rejected; rebasing onto origin/journal once and retrying" >&2
if ! git pull --rebase origin journal; then
  git rebase --abort >/dev/null 2>&1 || true
  echo "finish_run: ERROR: rebase onto origin/journal failed; state was committed locally but NOT pushed" >&2
  exit 1
fi
if ! git push origin journal; then
  echo "finish_run: ERROR: push to origin/journal failed after rebase; state NOT pushed" >&2
  exit 1
fi
echo "finish_run: pushed journal after rebase"

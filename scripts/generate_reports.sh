#!/usr/bin/env bash
#
# Headless report generation for the Vancouver gov meeting-minutes pipeline.
# Runs after scrape.py (see nightly.sh). For each body it invokes Claude Code
# in headless mode (`claude -p`) to synthesize reports for any meeting that has
# minutes but no report yet, then commits and pushes all new reports in one
# push (which triggers the email GitHub Action).
#
# The headless sessions do pure synthesis and never touch git; this script owns
# every git operation. Permissions for the sessions are scoped by the committed
# .claude/settings.json, so runs never block on a permission prompt.
#
# Prerequisites on the host:
#   - `claude` CLI installed and authenticated (claude login, or ANTHROPIC_API_KEY)
#   - repo-local .venv (auto-created below) with requirements.txt installed
#   - git identity + push credentials configured (SSH deploy key)
#
# Env overrides: CLAUDE_BIN, REPORT_LOG_DIR, REPORT_TIMEOUT (seconds per body).
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

LOG_DIR="${REPORT_LOG_DIR:-$HOME/vancouver_scraper/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/reports.log"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
TIMEOUT_S="${REPORT_TIMEOUT:-1800}"

ts() { date +'%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "$(ts) $*" >>"$LOG"; }

# Ensure the repo-local venv exists for PDF rendering.
if [ ! -x .venv/bin/python ]; then
  log "creating .venv"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt >>"$LOG" 2>&1
fi

# Sync before generating (the scraper already pushed; this is a safety net).
if ! git pull --rebase --autostash >>"$LOG" 2>&1; then
  log "ERROR git pull failed; aborting report run"
  exit 1
fi

# `timeout` (GNU coreutils) caps each run; fall back to no cap if unavailable.
TIMEOUT_BIN=""
command -v timeout >/dev/null 2>&1 && TIMEOUT_BIN="timeout ${TIMEOUT_S}"

for body in council parkboard vsb; do
  prompt_file="scripts/report_prompts/${body}.txt"
  [ -f "$prompt_file" ] || { log "WARN missing prompt $prompt_file"; continue; }
  log "generating ${body} reports"
  if ! $TIMEOUT_BIN "$CLAUDE_BIN" -p "$(cat "$prompt_file")" \
        --permission-mode acceptEdits >>"$LOG" 2>&1; then
    log "WARN ${body} headless run exited non-zero (continuing)"
  fi
done

# Commit + push every new report in a single push.
git add ':(glob)*/reports/*.md' ':(glob)*/reports/*.pdf' 2>>"$LOG"
if git diff --cached --quiet; then
  log "no new reports"
  exit 0
fi
count=$(git diff --cached --name-only --diff-filter=A | grep -c 'reports/.*\.pdf$' || true)
git commit -q -m "reports: $(date +%F) — ${count} new report(s)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" >>"$LOG" 2>&1

for attempt in 1 2 3; do
  if git push >>"$LOG" 2>&1; then
    log "pushed ${count} new report(s)"
    exit 0
  fi
  log "push rejected (attempt ${attempt}); rebasing"
  git pull --rebase >>"$LOG" 2>&1 || true
done
log "ERROR push failed after 3 attempts"
exit 1

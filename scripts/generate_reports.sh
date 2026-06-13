#!/usr/bin/env bash
#
# Headless report generation for the Vancouver gov meeting-minutes pipeline.
# Runs after scrape.py (see nightly.sh). For each body it invokes Claude Code
# in headless mode (`claude -p`) to synthesize reports for any meeting that has
# minutes but no report yet, writing them into the data directory. Any newly
# created report PDFs are then emailed (server-side SMTP).
#
# Data (meetings, bylaws, reports) lives in $VANCOUVER_DATA_DIR, NOT git. This
# script runs from the repo (cwd = repo root) so the headless sessions can use
# the repo-local .venv renderer and the committed .claude/settings.json
# allowlist; --add-dir grants them read/write access to the data tree.
#
# Prerequisites on the host:
#   - `claude` CLI installed and authenticated (claude login, or ANTHROPIC_API_KEY)
#   - repo-local .venv (auto-created below) with requirements.txt installed
#   - optional SMTP_* vars (in ~/vancouver_scraper/.env) to enable email
#
# Env overrides: VANCOUVER_DATA_DIR, CLAUDE_BIN, REPORT_LOG_DIR, REPORT_TIMEOUT,
#                REPORT_EMAIL_TO.
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

DATA_DIR="${VANCOUVER_DATA_DIR:-/mnt/hyperion_share_fast/vancouver_meeting_reports}"
LOG_DIR="${REPORT_LOG_DIR:-$HOME/vancouver_scraper/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/reports.log"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
TIMEOUT_S="${REPORT_TIMEOUT:-1800}"
EMAIL_TO="${REPORT_EMAIL_TO:-tech@davidhoeppner.ca}"

ts() { date +'%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "$(ts) $*" >>"$LOG"; }

if [ ! -d "$DATA_DIR" ]; then
  log "ERROR data dir $DATA_DIR does not exist; aborting report run"
  exit 1
fi

# Ensure the repo-local venv exists for PDF rendering and the email helper.
if [ ! -x .venv/bin/python ]; then
  log "creating .venv"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt >>"$LOG" 2>&1
fi

# Snapshot existing report PDFs so we can email only the newly created ones.
before="$(mktemp)"; after="$(mktemp)"
trap 'rm -f "$before" "$after"' EXIT
find "$DATA_DIR" -path '*/reports/*.pdf' 2>/dev/null | sort >"$before"

# `timeout` (GNU coreutils) caps each run; fall back to no cap if unavailable.
TIMEOUT_BIN=""
command -v timeout >/dev/null 2>&1 && TIMEOUT_BIN="timeout ${TIMEOUT_S}"

for body in council parkboard vsb; do
  prompt_file="scripts/report_prompts/${body}.txt"
  [ -f "$prompt_file" ] || { log "WARN missing prompt $prompt_file"; continue; }
  log "generating ${body} reports"
  prompt="$(sed "s#__DATA_DIR__#${DATA_DIR}#g" "$prompt_file")"
  if ! $TIMEOUT_BIN "$CLAUDE_BIN" -p "$prompt" \
        --permission-mode acceptEdits --add-dir "$DATA_DIR" >>"$LOG" 2>&1; then
    log "WARN ${body} headless run exited non-zero (continuing)"
  fi
done

# Email any newly created report PDFs (no-op if SMTP_* unset).
find "$DATA_DIR" -path '*/reports/*.pdf' 2>/dev/null | sort >"$after"
mapfile -t new < <(comm -13 "$before" "$after")
if [ "${#new[@]}" -eq 0 ]; then
  log "no new reports"
  exit 0
fi
log "new reports: ${new[*]}"
if .venv/bin/python scripts/send_email.py --to "$EMAIL_TO" "${new[@]}" >>"$LOG" 2>&1; then
  log "emailed ${#new[@]} new report(s) to ${EMAIL_TO}"
else
  log "WARN email step failed (reports are still on disk)"
fi

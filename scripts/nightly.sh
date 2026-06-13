#!/usr/bin/env bash
#
# Full nightly run for the Vancouver gov meeting-minutes pipeline.
# Install one cron entry that calls this:
#   30 2 * * * bash /path/to/repo/scripts/nightly.sh
#
# 1. scrape.py downloads new meetings + bylaws into $VANCOUVER_DATA_DIR.
# 2. generate_reports.sh synthesizes reports there and emails the new ones.
#
# Data lives in $VANCOUVER_DATA_DIR (not git); the repo holds only scripts.
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

# cron runs with a minimal PATH and no API key — restore both so `node`,
# `claude`, ANTHROPIC_API_KEY, SMTP_*, and VANCOUVER_DATA_DIR are available.
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -f "$HOME/vancouver_scraper/.env" ] && . "$HOME/vancouver_scraper/.env"

DATA_DIR="${VANCOUVER_DATA_DIR:-/mnt/hyperion_share_fast/vancouver_meeting_reports}"
LOG_DIR="${REPORT_LOG_DIR:-$HOME/vancouver_scraper/logs}"
mkdir -p "$LOG_DIR"

# Best-effort: pull script updates (data is not in the repo). Never fatal.
git pull --ff-only >>"$LOG_DIR/cron.log" 2>&1 || true

# Ensure the repo-local venv exists (shared by scraper and PDF renderer).
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi

# Refuse to run if the data directory is missing (e.g. mount not present).
if [ ! -d "$DATA_DIR" ]; then
  echo "$(date -Is) ERROR data dir $DATA_DIR missing; aborting" >>"$LOG_DIR/cron.log"
  exit 1
fi

.venv/bin/python scripts/scrape.py --log-dir "$LOG_DIR" >>"$LOG_DIR/cron.log" 2>&1
bash scripts/generate_reports.sh >>"$LOG_DIR/cron.log" 2>&1

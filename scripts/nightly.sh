#!/usr/bin/env bash
#
# Full nightly run for the Vancouver gov meeting-minutes pipeline.
# Install one cron entry that calls this:
#   30 2 * * * bash /path/to/repo/scripts/nightly.sh
#
# 1. scrape.py downloads new meetings + bylaws and commits/pushes them.
# 2. generate_reports.sh synthesizes reports via headless Claude Code and
#    commits/pushes them (which triggers the email GitHub Action).
#
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

# cron runs with a minimal PATH and no API key — restore both so `node`,
# `claude`, and ANTHROPIC_API_KEY are available to the report step.
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -f "$HOME/vancouver_scraper/.env" ] && . "$HOME/vancouver_scraper/.env"

LOG_DIR="${REPORT_LOG_DIR:-$HOME/vancouver_scraper/logs}"
mkdir -p "$LOG_DIR"

# Ensure the repo-local venv exists (shared by scraper and PDF renderer).
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi

.venv/bin/python scripts/scrape.py --log-dir "$LOG_DIR" >>"$LOG_DIR/cron.log" 2>&1
bash scripts/generate_reports.sh >>"$LOG_DIR/cron.log" 2>&1

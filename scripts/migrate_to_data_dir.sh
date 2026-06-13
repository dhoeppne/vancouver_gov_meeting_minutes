#!/usr/bin/env bash
#
# One-time migration: move already-scraped data out of the git repo and into
# the data directory, so nothing has to be re-scraped after the repo stops
# storing data.
#
#   1. copy each body dir (meetings/bylaws/reports/manifest) into $DATA_DIR
#   2. verify every file copied byte-for-byte (sha256 + presence)
#   3. only on full success: git rm the body dirs, .gitignore them, and commit
#      locally (you push)
#
# Usage:
#   scripts/migrate_to_data_dir.sh [--dry-run] [--allow-unmounted] [--data-dir DIR]
#
#   --dry-run         copy + verify only; make no git changes
#   --allow-unmounted skip the mountpoint safety check (local testing / non-mount dirs)
#   --data-dir DIR    override $VANCOUVER_DATA_DIR / the built-in default
#
set -uo pipefail

DRY_RUN=0
ALLOW_UNMOUNTED=0
DATA_DIR_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --allow-unmounted) ALLOW_UNMOUNTED=1 ;;
    --data-dir) DATA_DIR_ARG="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

DATA_DIR="${DATA_DIR_ARG:-${VANCOUVER_DATA_DIR:-/mnt/hyperion_share_fast/vancouver_meeting_reports}}"
BODIES=(vancouver_city_council vancouver_park_board vancouver_school_board)

SHA="sha256sum"
command -v sha256sum >/dev/null 2>&1 || SHA="shasum -a 256"
hash_of() { $SHA "$1" | awk '{print $1}'; }

abort() { echo "ERROR: $*" >&2; exit 1; }

# --- Safety: the data dir's parent must exist, be writable, and (when we can
#     tell) be a real mountpoint — so we never copy onto an unmounted path and
#     then delete the source. ---
GUARD="$(dirname "$DATA_DIR")"
[ -d "$GUARD" ] || abort "data dir parent does not exist: $GUARD"
[ -w "$GUARD" ] || abort "data dir parent is not writable: $GUARD"
if [ "$ALLOW_UNMOUNTED" -ne 1 ] && command -v mountpoint >/dev/null 2>&1; then
  mountpoint -q "$GUARD" || abort "$GUARD is not a mountpoint (use --allow-unmounted to override)"
fi

echo "Repo:      $REPO_DIR"
echo "Data dir:  $DATA_DIR"
echo "Mode:      $([ "$DRY_RUN" -eq 1 ] && echo 'DRY RUN (copy + verify only)' || echo 'MIGRATE')"
echo

mkdir -p "$DATA_DIR"

# --- 1. Copy ---
present=()
for body in "${BODIES[@]}"; do
  [ -d "$body" ] || { echo "skip (absent in repo): $body"; continue; }
  present+=("$body")
  echo "copying $body/ -> $DATA_DIR/$body/"
  rsync -a --exclude='.gitkeep' "$body/" "$DATA_DIR/$body/" \
    || abort "rsync failed for $body"
done
[ "${#present[@]}" -gt 0 ] || abort "no body directories found in the repo to migrate"

# --- 2. Verify (sha256 + presence for every source file) ---
echo
echo "verifying..."
total=0; fail=0
for body in "${present[@]}"; do
  while IFS= read -r rel; do
    total=$((total + 1))
    src="$body/$rel"
    dst="$DATA_DIR/$body/$rel"
    if [ ! -f "$dst" ]; then echo "  MISSING  $body/$rel"; fail=$((fail + 1)); continue; fi
    if [ "$(hash_of "$src")" != "$(hash_of "$dst")" ]; then
      echo "  MISMATCH $body/$rel"; fail=$((fail + 1))
    fi
  done < <(cd "$body" && find . -type f ! -name '.gitkeep' | sed 's#^\./##' | sort)
done
echo "verified $((total - fail))/$total files"
[ "$fail" -eq 0 ] || abort "$fail file(s) failed verification — repo left untouched"

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "DRY RUN OK — all files copied and verified. No git changes made."
  exit 0
fi

# --- 3. Remove from the repo (git rm), ignore going forward, commit locally ---
echo
echo "removing migrated data from the repo..."
git rm -r --quiet "${present[@]}"

for body in "${BODIES[@]}"; do
  grep -qxF "$body/" .gitignore 2>/dev/null || echo "$body/" >>.gitignore
done
git add .gitignore

git commit -q -m "Move scraped data out of the repo into \$VANCOUVER_DATA_DIR

Data now lives in $DATA_DIR (verified copy of ${present[*]}).
The repo keeps only the scripts; data dirs are gitignored."

echo
echo "Done. Migrated: ${present[*]}"
echo "Committed the removal locally. Review and push when ready:"
echo "    git -C $REPO_DIR push"

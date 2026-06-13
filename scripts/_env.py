"""Load the pipeline's .env into os.environ for standalone script runs.

When scripts are launched by nightly.sh the environment is already set. This
helper lets you also run scrape.py / send_email.py directly without sourcing
.env first. The file is parsed by real bash (so inline comments and quotes work
exactly as in a shell), and values already present in the environment always
win — explicit env vars and CLI flags are never overridden.

Location: $VANCOUVER_ENV_FILE, else ~/vancouver_scraper/.env.
"""

import os
import subprocess
from pathlib import Path


def env_file() -> Path:
    override = os.environ.get("VANCOUVER_ENV_FILE")
    return Path(override) if override else Path.home() / "vancouver_scraper" / ".env"


def load_env(path: "str | Path | None" = None) -> None:
    target = Path(path) if path else env_file()
    if not target.is_file():
        return
    try:
        result = subprocess.run(
            ["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; env', "_", str(target)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in result.stdout.splitlines():
        key, sep, val = line.partition("=")
        if sep and key and key not in os.environ:
            os.environ[key] = val

# vancouver_gov_meeting_minutes

AI-summarized reports of meeting minutes for Vancouver's government bodies:
**City Council**, the **Park Board**, and the **School Board (VSB)**.

## How it works

```
┌─ home server, nightly 02:30 (scripts/nightly.sh) ─────────────────┐
│ 1. scripts/scrape.py                                              │
│      · discovers + downloads agendas/minutes                      │
│      · downloads every cited bylaw/policy                         │
│      · extracts PDF text to .txt sidecars                         │
│      · updates manifest.json per body                            │
│ 2. scripts/generate_reports.sh                                    │
│      · for each body: claude -p <prompt> (headless synthesis)     │
│      · reads manifest + extracted text, writes reports/<key>.md   │
│        and renders <key>.pdf                                      │
│      · emails any newly created report PDFs (server-side SMTP)     │
└──────────────────────────────────────┬────────────────────────────┘
   writes everything to $VANCOUVER_DATA_DIR (not git)
                                        ▼
        /mnt/hyperion_share_fast/vancouver_meeting_reports/
            vancouver_city_council/{meetings,bylaws,reports}/  …+ 2 more bodies
```

The git repo holds **only the scripts**. All scraped data and reports are
written to `$VANCOUVER_DATA_DIR` on the server (default
`/mnt/hyperion_share_fast/vancouver_meeting_reports`). The scraper does **all**
information gathering so the report step does pure synthesis. Reports are
idempotent: one report per meeting, keyed `YYYY-MM-DD_<type>`; a meeting is
"unreported" iff its `minutes.txt` exists and `reports/<key>.pdf` does not. The
report step never rewrites existing reports. Everything runs unattended on the
server — nothing depends on a desktop app or on git.

## Layout

```
# In $VANCOUVER_DATA_DIR (on the server, not git):
vancouver_city_council/        # same shape for _park_board / _school_board
├── manifest.json              # single source of truth the report step reads
├── meetings/<key>/            # agenda.pdf/.txt, minutes.pdf/.txt, attachments/
├── bylaws/                    # cited bylaw/policy PDFs + .txt (+ _unresolved.json)
└── reports/<key>.md + .pdf    # synthesized reports

# In the git repo (scripts only):
scripts/scrape.py              # nightly scraper → $VANCOUVER_DATA_DIR
scripts/generate_reports.sh    # headless report synthesis + email
scripts/nightly.sh             # cron entrypoint (scrape, then reports)
scripts/md_to_pdf.py           # report.md -> report.pdf (markdown2 + xhtml2pdf)
scripts/send_email.py          # emails new report PDFs (stdlib SMTP)
scripts/report_prompts/*.txt   # per-body synthesis prompts (__DATA_DIR__ placeholder)
scripts/migrate_to_data_dir.sh # one-time: move existing data out of the repo
```

Meeting type codes — council: `regu`, `pspc`, `cfsc`, `phea`, `spec`
(in-camera `icre` is excluded); park board: `regular`, `committee`, `special`;
VSB: `board`, `special`, `delegation`.

## Data sources

| Body | Source | Notes |
|---|---|---|
| City Council | `council.vancouver.ca/YYYYMMDD/<type>YYYYMMDD{ag,min}` | date folders probed per type; minutes lag ~2 weeks |
| Park Board | `parkboardmeetings.vancouver.ca/YYYY/index.htm` | Cloudflare-protected → scraper uses curl_cffi Chrome impersonation |
| School Board | CMS media-library API behind `vsb.bc.ca/meeting-agendas-and-minutes` | media URLs contain hashes; never constructed, always discovered |
| Council bylaws | `bylaws.vancouver.ca` (`consolidated/{n}.pdf`, `{n}c.PDF`) | cited as "By-law No. N" in agendas/minutes |
| Park bylaws | `parkboardmeetings.vancouver.ca/files/BYLAW-*.pdf` | plus consolidated 2024 park bylaws |
| VSB policies | `vsb.bc.ca/board-policies-and-bylaws` | cited as "Policy N" in minutes |

## Server setup (fresh Ubuntu)

Tested on Ubuntu 22.04/24.04 LTS. Run as the non-root user that will own the
cron job. Replace `dhoeppne/...` if you forked.

### 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y git curl python3 python3-venv python3-pip
```

### 2. Node.js 20 LTS (required by the Claude Code CLI)

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version    # expect v20.x (Claude Code needs Node 18+)
```

### 3. Install the Claude Code CLI

Install globally without `sudo` by pointing npm at a user-owned prefix:

```bash
mkdir -p ~/.npm-global
npm config set prefix ~/.npm-global
echo 'export PATH=~/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
npm install -g @anthropic-ai/claude-code
claude --version    # verify the install
```

### 4. Authenticate Claude

Two options — pick one. **`nightly.sh` sources `~/vancouver_scraper/.env`, so
whichever credential you use goes there.**

**A) Subscription (Claude Pro/Max) — no per-token charges.** Generate a
long-lived OAuth token tied to your subscription with `claude setup-token`. It
needs a browser for the one-time authorization, so the easy path is to run it
on your laptop (or anywhere with a browser) and copy the token to the server:

```bash
claude setup-token        # authorize in the browser, copy the printed token
```

Then put it in the server's `.env` as `CLAUDE_CODE_OAUTH_TOKEN` (see below).
**Do not also set `ANTHROPIC_API_KEY`** — if present it takes precedence and
bills per-token instead of using your subscription. (For interactive manual
runs on the server you can instead `claude login`; cron then reuses the stored
credentials.)

**B) API key (pay-per-token)** from <https://console.anthropic.com/> — set
`ANTHROPIC_API_KEY` in `.env` instead of the OAuth token.

```bash
mkdir -p ~/vancouver_scraper
umask 077
cat > ~/vancouver_scraper/.env <<'EOF'
# --- Claude auth: use ONE of these ---
export CLAUDE_CODE_OAUTH_TOKEN=...                   # (A) subscription token
# export ANTHROPIC_API_KEY=sk-ant-...                # (B) API billing instead

# Where all scraped data + reports are written (must exist / be mounted):
export VANCOUVER_DATA_DIR=/mnt/hyperion_share_fast/vancouver_meeting_reports

# Email via Mailtrap (optional — omit to disable; reports still land on disk).
# Sandbox (captures mail in the Mailtrap inbox) — values from Mailtrap →
# Email Testing → your inbox → SMTP Settings → "Show Credentials":
export SMTP_SERVER=sandbox.smtp.mailtrap.io
export SMTP_PORT=587                                  # 587/2525 STARTTLS, or 465 SSL
export SMTP_USERNAME=your_mailtrap_inbox_user         # a hash, NOT an email
export SMTP_PASSWORD=your_mailtrap_inbox_pass
# From and To are independent — different addresses/domains are fine:
export SMTP_FROM=reports@sender-domain.com            # the From: address
export SMTP_TO=you@your-domain.com                    # the recipient
EOF
```

`SMTP_FROM` and `SMTP_TO` are both required to send and may be on entirely
different domains; neither is tied to `SMTP_USERNAME` (Mailtrap's username is
not an email address). For Mailtrap **live sending** (delivers to a real inbox)
instead of sandbox, use `SMTP_SERVER=live.smtp.mailtrap.io`,
`SMTP_USERNAME=api`, `SMTP_PASSWORD=<your API token>`, and an `SMTP_FROM` on a
verified sending domain.

A few nightly headless sessions sit comfortably within Pro/Max limits.

Every script auto-loads this file (`~/vancouver_scraper/.env`, or
`$VANCOUVER_ENV_FILE`), so you never have to `source` it by hand — running
`scripts/send_email.py`, `scripts/scrape.py`, etc. directly just works.

### 5. GitHub deploy key (read-only is enough)

The server only pulls script updates — it never pushes — so a **read-only**
deploy key suffices:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/vancouver_scraper -C "scraper@homeserver" -N ""
cat >> ~/.ssh/config <<'EOF'
Host github.com-vancouver
    HostName github.com
    IdentityFile ~/.ssh/vancouver_scraper
    IdentitiesOnly yes
EOF
cat ~/.ssh/vancouver_scraper.pub
# → add this at GitHub → repo → Settings → Deploy keys (no write access needed)
```

### 6. Clone + Python venv

```bash
cd ~/vancouver_scraper
git clone git@github.com-vancouver:dhoeppne/vancouver_gov_meeting_minutes.git repo
cd repo
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 7. (One-time) migrate existing data out of the repo

If the repo still tracks previously-scraped data (the `vancouver_*` body dirs),
move it into the data directory once so nothing has to be re-scraped. The
script copies, verifies every file by sha256, then `git rm`s + commits the
removal locally (you push):

```bash
bash scripts/migrate_to_data_dir.sh            # copy → verify → git rm → commit
# (add --dry-run first to copy + verify without touching git)
git push                                        # publish the removal
```

It aborts if `$VANCOUVER_DATA_DIR`'s mount isn't present, so it never deletes
the source after copying to an unmounted path. Skip this step on a fresh repo
that never stored data.

### 8. First run, then nightly cron

```bash
bash scripts/nightly.sh                       # full backfill + first reports
tail -f ~/vancouver_scraper/logs/cron.log     # watch progress

crontab -e
# add (use the absolute path; run `echo $HOME` to fill in USER):
# 30 2 * * * bash /home/USER/vancouver_scraper/repo/scripts/nightly.sh
```

`nightly.sh` fixes up `PATH` and sources `~/vancouver_scraper/.env` so cron's
minimal environment can find `node`/`claude`, your Claude credential,
`VANCOUVER_DATA_DIR`, and the SMTP settings.

Useful scraper flags: `--body council|parkboard|vsb`, `--dry-run` (discovery
only), `--data-dir DIR`, `--window-start YYYY-MM-DD`, `--log-dir DIR`.

## Report generation (headless) & email

After the scrape, [`scripts/generate_reports.sh`](scripts/generate_reports.sh)
runs `claude -p` once per body using the prompts in
[`scripts/report_prompts/`](scripts/report_prompts/) (the `__DATA_DIR__`
placeholder is substituted with `$VANCOUVER_DATA_DIR` at runtime). Each headless
session does pure synthesis — reads the manifest + extracted text under the data
dir, writes `reports/<key>.md`, and renders `<key>.pdf` with the repo's
`.venv` — and never touches git or the network. It runs with `--add-dir
$VANCOUVER_DATA_DIR` so it can read/write the data tree, while cwd stays the repo
so the committed [`.claude/settings.json`](.claude/settings.json) allowlist (file
tools + `.venv/bin/python`, no git/network) applies and cron never blocks on a
prompt. Reports are processed at most 5 per body per run, oldest first.

**Email:** the wrapper diffs the report PDFs before/after the run and emails any
new ones via [`scripts/send_email.py`](scripts/send_email.py) (stdlib SMTP,
using the `SMTP_*` vars from `.env`). If SMTP isn't configured the step is a
silent no-op — reports always land in the data dir regardless.

Cadence is simply "every night" — each body is a cheap no-op when nothing new
has published (council minutes lag ~2 weeks, park board 1–2 weeks, VSB minutes
are approved at the *next* monthly meeting; the board is dark Jul/Aug/Dec).

Each report has fixed sections: TL;DR · Key Decisions & Votes · Bylaws &
Policies Enacted or Discussed · Money & Budget Items · Contentious Items &
Public Delegations · What to Watch Next.

## Verifying & testing

Run these from the repo directory (wherever you cloned it). The scripts
**auto-load** `~/vancouver_scraper/.env` themselves — no manual `source` needed
— so the data dir, SMTP, and Claude credential are picked up automatically.
(Override the location with `VANCOUVER_ENV_FILE=/path/to/.env`.)

**Test email** without sending (checks config — From/To/attachments — and sends
nothing). From/To come from `$SMTP_FROM` / `$SMTP_TO`:

```bash
.venv/bin/python scripts/send_email.py --dry-run \
  "$VANCOUVER_DATA_DIR"/vancouver_city_council/reports/*.pdf
```

Then send a real one (any PDF works). With Mailtrap **sandbox** the message is
captured in your Mailtrap inbox; with **live** sending it arrives at `$SMTP_TO`:

```bash
.venv/bin/python scripts/send_email.py \
  "$(find "$VANCOUVER_DATA_DIR" -path '*/reports/*.pdf' | head -1)"
# prints "emailed 1 report(s) to ..." on success → check the Mailtrap inbox (or your inbox)
```

**Verify Claude actually ran.** The headless sessions' output (including each
body's final summary line) is captured in the logs:

```bash
tail -n 100 ~/vancouver_scraper/logs/reports.log    # "generating <body> reports", summaries, "new reports: ..."
tail -n 50  ~/vancouver_scraper/logs/cron.log       # overall nightly run
ls -lt "$VANCOUVER_DATA_DIR"/*/reports/*.pdf | head # newest report files = proof of synthesis
```

To watch one body run live with full detail (turn count, token cost, result),
run a single prompt in the foreground:

```bash
sed "s#__DATA_DIR__#$VANCOUVER_DATA_DIR#g" scripts/report_prompts/council.txt \
  | claude -p --permission-mode acceptEdits --add-dir "$VANCOUVER_DATA_DIR" \
           --verbose --output-format stream-json
```

A clean run ends with a `result` event; an auth failure surfaces immediately
here (so this also confirms your `CLAUDE_CODE_OAUTH_TOKEN`/API key works).

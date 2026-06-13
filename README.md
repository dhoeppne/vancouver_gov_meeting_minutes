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
│      · updates manifest.json per body, commits + pushes           │
│ 2. scripts/generate_reports.sh                                    │
│      · for each body: claude -p <prompt> (headless synthesis)     │
│      · reads manifest + extracted text, writes reports/<key>.md   │
│        and renders <key>.pdf                                      │
│      · commits + pushes all new reports in one push               │
└──────────────────────────────────────┬────────────────────────────┘
                                        │ push touching reports/*.pdf
                       ┌────────────────▼─────────────────┐
                       │ GitHub Action emails the new PDFs │
                       │ to tech@davidhoeppner.ca          │
                       └───────────────────────────────────┘
```

The scraper does **all** information gathering so the report step does pure
synthesis. Reports are idempotent: one report per meeting, keyed
`YYYY-MM-DD_<type>`; a meeting is "unreported" iff its `minutes.txt` exists
and `reports/<key>.pdf` does not. The report step never rewrites existing
reports. Both halves run unattended on the server — nothing depends on a
desktop app being open.

## Repo layout

```
vancouver_city_council/        # same shape for _park_board / _school_board
├── manifest.json              # single source of truth the routines read
├── meetings/<key>/            # agenda.pdf/.txt, minutes.pdf/.txt, attachments/
├── bylaws/                    # cited bylaw/policy PDFs + .txt (+ _unresolved.json)
└── reports/<key>.md + .pdf    # synthesized reports
scripts/scrape.py              # nightly scraper (standalone)
scripts/md_to_pdf.py           # report.md -> report.pdf (markdown2 + xhtml2pdf)
.github/workflows/email-reports.yml
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

### 4. Authenticate the CLI for unattended runs

Use an API key from <https://console.anthropic.com/> — simplest for cron,
which has no interactive session:

```bash
mkdir -p ~/vancouver_scraper
umask 077
cat > ~/vancouver_scraper/.env <<'EOF'
export ANTHROPIC_API_KEY=sk-ant-...      # paste your key
EOF
```

`nightly.sh` sources this file automatically. (Interactive `claude login`
also works for manual runs, but the API key is what makes cron headless.)

### 5. GitHub deploy key (write access)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/vancouver_scraper -C "scraper@homeserver" -N ""
cat >> ~/.ssh/config <<'EOF'
Host github.com-vancouver
    HostName github.com
    IdentityFile ~/.ssh/vancouver_scraper
    IdentitiesOnly yes
EOF
cat ~/.ssh/vancouver_scraper.pub
# → add this at GitHub → repo → Settings → Deploy keys → check "Allow write access"
```

### 6. Clone + Python venv

```bash
cd ~/vancouver_scraper
git clone git@github.com-vancouver:dhoeppne/vancouver_gov_meeting_minutes.git repo
cd repo
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
git config user.name "vancouver-scraper"
git config user.email "tech@davidhoeppner.ca"
```

### 7. First run, then nightly cron

```bash
bash scripts/nightly.sh                       # full backfill + first reports
tail -f ~/vancouver_scraper/logs/cron.log     # watch progress

crontab -e
# add (use the absolute path; run `echo $HOME` to fill in USER):
# 30 2 * * * bash /home/USER/vancouver_scraper/repo/scripts/nightly.sh
```

`nightly.sh` fixes up `PATH` and sources `~/vancouver_scraper/.env` so cron's
minimal environment can find `node`/`claude` and your API key.

Useful scraper flags: `--body council|parkboard|vsb`, `--dry-run` (discovery
only), `--no-git`, `--window-start YYYY-MM-DD`, `--log-dir DIR`.

## Email notifications

Add these **Actions secrets** (GitHub → Settings → Secrets and variables →
Actions): `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`.
Until they exist the email step skips silently; report PDFs are always
committed regardless.

## Report generation (headless)

After the scrape, [`scripts/generate_reports.sh`](scripts/generate_reports.sh)
runs `claude -p` once per body using the self-contained prompts in
[`scripts/report_prompts/`](scripts/report_prompts/). Each headless session
does pure synthesis — reads the manifest + extracted text, writes
`reports/<key>.md`, and renders `<key>.pdf` — and never touches git or the
network. The wrapper owns all git: it commits and pushes every new report in
one push, which triggers the email Action.

Permissions for the headless runs are scoped by the committed
[`.claude/settings.json`](.claude/settings.json) allowlist (file tools +
`.venv/bin/python` for rendering, no git or network), so cron runs never block
on a permission prompt. Reports are processed at most 5 per body per run,
oldest first, to bound cost.

Cadence is simply "every night" — each body is a cheap no-op when nothing new
has published (council minutes lag ~2 weeks, park board 1–2 weeks, VSB minutes
are approved at the *next* monthly meeting; the board is dark Jul/Aug/Dec).

Each report has fixed sections: TL;DR · Key Decisions & Votes · Bylaws &
Policies Enacted or Discussed · Money & Budget Items · Contentious Items &
Public Delegations · What to Watch Next.

# vancouver_gov_meeting_minutes

AI-summarized reports of meeting minutes for Vancouver's government bodies:
**City Council**, the **Park Board**, and the **School Board (VSB)**.

## How it works

```
┌─ home server, nightly 02:30 ──────────────┐   ┌─ Mac, Claude Code routines ─────────┐
│ scripts/scrape.py                         │   │ council-reports    (Mon 08:30)      │
│  · discovers + downloads agendas/minutes  │   │ parkboard-reports  (8th/22nd 08:45) │
│  · downloads every cited bylaw/policy     │ → │ vsb-reports        (3rd 09:00)      │
│  · extracts PDF text to .txt sidecars     │git│  · read manifest + extracted text   │
│  · updates manifest.json per body         │   │  · write reports/<key>.md + .pdf    │
│  · commits + pushes                       │   │  · commit + push                    │
└───────────────────────────────────────────┘   └──────────────────┬──────────────────┘
                                                                   │ push touching reports/*.pdf
                                                ┌──────────────────▼──────────────────┐
                                                │ GitHub Action emails the new PDFs   │
                                                │ to tech@davidhoeppner.ca            │
                                                └─────────────────────────────────────┘
```

The scraper does **all** information gathering so the report routines do pure
synthesis. Reports are idempotent: one report per meeting, keyed
`YYYY-MM-DD_<type>`; a meeting is "unreported" iff its `minutes.txt` exists
and `reports/<key>.pdf` does not. Routines never rewrite existing reports.

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

## Server bootstrap (scraper host)

```bash
# 1. Repo-scoped deploy key (add the .pub as a *write-access* deploy key
#    in GitHub → Settings → Deploy keys)
ssh-keygen -t ed25519 -f ~/.ssh/vancouver_scraper -C "scraper@homeserver"
cat >> ~/.ssh/config <<'EOF'
Host github.com-vancouver
    HostName github.com
    IdentityFile ~/.ssh/vancouver_scraper
    IdentitiesOnly yes
EOF

# 2. Clone + venv
mkdir -p ~/vancouver_scraper/logs && cd ~/vancouver_scraper
git clone git@github.com-vancouver:dhoeppne/vancouver_gov_meeting_minutes.git repo
python3 -m venv venv && venv/bin/pip install -r repo/requirements.txt
git -C repo config user.name "vancouver-scraper"
git -C repo config user.email "tech@davidhoeppner.ca"

# 3. Manual first run, then cron
venv/bin/python repo/scripts/scrape.py
crontab -e   # add:
# 30 2 * * * /home/USER/vancouver_scraper/venv/bin/python /home/USER/vancouver_scraper/repo/scripts/scrape.py >> /home/USER/vancouver_scraper/logs/cron.log 2>&1
```

Useful flags: `--body council|parkboard|vsb`, `--dry-run` (discovery only),
`--no-git`, `--window-start YYYY-MM-DD`, `--log-dir DIR`.

## Email notifications

Add these **Actions secrets** (GitHub → Settings → Secrets and variables →
Actions): `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`.
Until they exist the email step skips silently; report PDFs are always
committed regardless.

## Report routines

Three Claude Code scheduled tasks on the Mac (they run while the desktop app
is open; a missed run fires on next launch and is a cheap no-op when there is
nothing new):

| task | schedule | why |
|---|---|---|
| `council-reports` | Mon 08:30 weekly | ~5–6 meetings/month, minutes lag ~2 weeks |
| `parkboard-reports` | 8th + 22nd 08:45 | bi-weekly meetings, minutes lag 1–2 weeks |
| `vsb-reports` | 3rd of month 09:00 | ~monthly meetings, minutes approved at the *next* meeting |

Each report has fixed sections: TL;DR · Key Decisions & Votes · Bylaws &
Policies Enacted or Discussed · Money & Budget Items · Contentious Items &
Public Delegations · What to Watch Next.

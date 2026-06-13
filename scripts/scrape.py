#!/usr/bin/env python3
"""Nightly scraper for Vancouver civic meeting documents.

Downloads meeting agendas, minutes, and attachments for Vancouver City
Council, the Vancouver Park Board, and the Vancouver School Board, plus
every bylaw/policy those documents reference. Extracts text from each PDF
into a .txt sidecar and maintains a per-body manifest.json so the report
routines can do pure synthesis without touching the network.

Designed for an unattended cron run:
    30 2 * * * /path/venv/bin/python /path/repo/scripts/scrape.py

Each body is scraped independently; one body failing does not stop the
others. A single git commit is made at the end of the run.
"""

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup
# curl_cffi impersonates a real Chrome TLS fingerprint; vancouver.ca sits
# behind Cloudflare bot protection that 403s plain requests/curl clients.
from curl_cffi import requests
from curl_cffi.requests.exceptions import HTTPError, RequestException

REPO_ROOT = Path(__file__).resolve().parent.parent
BODY_DIRS = {
    "council": "vancouver_city_council",
    "parkboard": "vancouver_park_board",
    "vsb": "vancouver_school_board",
}

BACKFILL_START = dt.date(2026, 3, 12)
LOOKBACK_DAYS = 60        # steady-state discovery window into the past
LOOKAHEAD_DAYS = 14       # agendas are posted ahead of meetings
STALE_AFTER_DAYS = 45     # minutes still unpublished -> status "stale"
GIVE_UP_AFTER_DAYS = 90   # stop re-checking for minutes entirely
EMPTY_PROBE_FINAL_DAYS = 14  # past dates that probed empty stay empty
MAX_BYLAW_ATTEMPTS = 5
# GitHub hard-rejects files >100 MB; skip oversized PDFs (e.g. the Zoning and
# Development By-law is ~170 MB) and record them as references instead.
MAX_DOWNLOAD_BYTES = 90 * 1024 * 1024

HEADERS = {
    "Accept-Language": "en-CA,en;q=0.9",
}

COUNCIL_BASE = "https://council.vancouver.ca"
COUNCIL_TYPES = {
    "regu": "Regular Council",
    "pspc": "Standing Committee on Policy and Strategic Priorities",
    "cfsc": "Standing Committee on City Finance and Services",
    "phea": "Public Hearing",
    "spec": "Special Council",
}
COUNCIL_BYLAW_URLS = [
    "https://bylaws.vancouver.ca/consolidated/{n}.pdf",
    "https://bylaws.vancouver.ca/{n}c.PDF",
    "https://bylaws.vancouver.ca/{n}c.pdf",
    "https://bylaws.vancouver.ca/{n}.pdf",
]
BYLAW_NUM_RE = re.compile(r"[Bb]y-?law\s+No\.?\s*(\d{3,5})")

PARKBOARD_BASE = "https://parkboardmeetings.vancouver.ca"
PARK_CONSOLIDATED_BYLAWS = (
    f"{PARKBOARD_BASE}/files/BYLAW-ALLParkBylaws-2024.pdf"
)
PARK_ATTACHMENT_PREFIXES = (
    "REPORT", "DECISION", "MOTION", "MEMO", "HIGHLIGHTS", "PRESENTATION",
)

VSB_BASE = "https://www.vsb.bc.ca"
VSB_LISTING_PAGE = f"{VSB_BASE}/meeting-agendas-and-minutes"
VSB_POLICY_PAGE = f"{VSB_BASE}/board-policies-and-bylaws"
VSB_API_FALLBACK = "https://cicmsapi.azurewebsites.net/vsb"
# The meeting library is rendered client-side, but the page embeds the CMS
# media-library folder tree as JSON and the files are listed via a POST API.
VSB_TREE_RE = re.compile(r"<!--(\[\{.*?\}\])-->", re.S)
VSB_API_RE = re.compile(r"_ci\.api\s*=\s*'([^']+)'")
# Names vary: "Open Board Meeting Agenda 2026 Apr 29", "Special Open Board
# Meeting Minutes 2025 Dec 17", "Public Delegation Board Minutes 2025 Oct 27"
# ("Meeting" sometimes omitted), "Open Special Board Meeting Agenda ...".
VSB_NAME_RE = re.compile(
    r"^((?:Special\s+)?(?:Open\s+)?(?:Special\s+)?"
    r"(?:Public\s+Delegation\s+)?Board)\s+"
    r"(?:Meeting\s+)?(Agenda|Minutes)\s+(\d{4})\s+([A-Za-z]{3,4})\s+(\d{1,2})",
    re.IGNORECASE,
)
VSB_POLICY_FILE_RE = re.compile(
    r"/\d{2}-policy[-_]?(\d{1,2})[-_]([a-z0-9_-]+)\.[0-9a-f]+\.pdf",
    re.IGNORECASE,
)
VSB_POLICY_REF_RE = re.compile(r"\bPolicy\s+(\d{1,2})\b", re.IGNORECASE)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
    "nov": 11, "dec": 12,
}

log = logging.getLogger("scrape")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Fetcher:
    """Polite HTTP client: Chrome impersonation, jittered delays, retries."""

    RETRY_STATUSES = (429, 500, 502, 503, 504)

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update(HEADERS)
        self.council_dir_listing_ok = True

    def _pause(self, light: bool = False) -> None:
        if light:
            time.sleep(random.uniform(0.25, 0.6))
        else:
            time.sleep(random.uniform(0.8, 1.8))

    def _request(self, method: str, url: str, allow_404: bool = False,
                 light: bool = False, **kwargs):
        last_exc: Exception = RequestException(f"unreachable: {url}")
        for attempt in range(3):
            self._pause(light)
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except RequestException as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code in self.RETRY_STATUSES:
                last_exc = HTTPError(f"HTTP {resp.status_code} for {url}")
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        raise last_exc

    def get(self, url: str, allow_404: bool = False, light: bool = False):
        """GET a URL. Returns the response, or None on 404 when allowed."""
        return self._request("GET", url, allow_404=allow_404, light=light)

    def post(self, url: str, data: dict):
        return self._request("POST", url, data=data)

    def content_length(self, url: str):
        """Return the Content-Length via HEAD, or None if unavailable."""
        try:
            resp = self.session.head(url, timeout=30, allow_redirects=True)
        except RequestException:
            return None
        raw = resp.headers.get("Content-Length")
        return int(raw) if raw and raw.isdigit() else None


# Sentinel: download skipped because the file exceeds MAX_DOWNLOAD_BYTES.
OVERSIZE = "oversize"


def download_file(fetcher, url, dest: Path, expect_pdf: bool = True):
    """Download url to dest atomically.

    Returns True if the file is present, False on failure/404, or the
    OVERSIZE sentinel if the file exceeds the size cap (so callers can record
    it as a reference rather than re-attempting it forever).
    """
    if dest.exists():
        return True
    size = fetcher.content_length(url)
    if size is not None and size > MAX_DOWNLOAD_BYTES:
        log.warning("oversized (%d MB), skipping: %s", size // 1_048_576, url)
        return OVERSIZE
    try:
        resp = fetcher.get(url, allow_404=True)
    except RequestException as exc:
        log.warning("download failed %s: %s", url, exc)
        return False
    if resp is None:
        return False
    if len(resp.content) > MAX_DOWNLOAD_BYTES:
        log.warning("oversized body, skipping: %s", url)
        return OVERSIZE
    if expect_pdf and not resp.content.startswith(b"%PDF-"):
        log.warning("not a PDF (skipping): %s", url)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(resp.content)
    os.replace(tmp, dest)
    log.info("downloaded %s", dest.relative_to(REPO_ROOT))
    return True


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

def extract_pdf_text(pdf_path: Path, txt_path: Path) -> str:
    """Extract text into a sidecar .txt; returns 'ok' or 'poor_or_scanned'."""
    if txt_path.exists():
        return "ok"
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text(x_tolerance=1.5) or "")
    except Exception as exc:  # noqa: BLE001 - corrupt PDFs shouldn't kill a run
        log.warning("text extraction failed for %s: %s", pdf_path.name, exc)
        return "poor_or_scanned"
    text = "\f".join(pages)
    txt_path.write_text(text, encoding="utf-8")
    if len(pages) > 1 and len(text.strip()) < 200:
        return "poor_or_scanned"
    return "ok"


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out)


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(body_dir: Path, body_name: str) -> dict:
    path = body_dir / "manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "body": body_name,
        "schema_version": 1,
        "last_run": None,
        "backfill_complete": False,
        "probed_empty": {},
        "meetings": {},
    }


def save_manifest(body_dir: Path, manifest: dict) -> None:
    manifest["last_run"] = dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    path = body_dir / "manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def meeting_entry(manifest, key, date_iso, mtype, label, source_url, today,
                  counters) -> dict:
    m = manifest["meetings"].get(key)
    if m is None:
        m = {
            "date": date_iso,
            "type": mtype,
            "type_label": label,
            "status": "discovered",
            "source_url": source_url,
            "agenda": None,
            "minutes": None,
            "attachments": [],
            "bylaws": [],
            "first_seen": today.isoformat(),
            "last_checked": today.isoformat(),
        }
        manifest["meetings"][key] = m
        counters["new_meetings"] += 1
        log.info("discovered meeting %s", key)
    return m


def scrape_window(manifest, today, override_start=None):
    if override_start:
        start = override_start
    elif not manifest.get("backfill_complete"):
        start = BACKFILL_START
    else:
        start = today - dt.timedelta(days=LOOKBACK_DAYS)
    return start, today + dt.timedelta(days=LOOKAHEAD_DAYS)


def update_status(m, mdir: Path, today: dt.date) -> None:
    m["last_checked"] = today.isoformat()
    meeting_date = dt.date.fromisoformat(m["date"])
    has_minutes_txt = (mdir / "minutes.txt").exists()
    if has_minutes_txt:
        m["status"] = "complete"
    elif (today - meeting_date).days > STALE_AFTER_DAYS:
        m["status"] = "stale"
    elif m.get("agenda"):
        m["status"] = "agenda_only"


def should_check_minutes(m, today: dt.date) -> bool:
    meeting_date = dt.date.fromisoformat(m["date"])
    return (today - meeting_date).days <= GIVE_UP_AFTER_DAYS


# --------------------------------------------------------------------------
# Bylaw resolution (shared)
# --------------------------------------------------------------------------

def load_unresolved(body_dir: Path) -> dict:
    path = body_dir / "bylaws" / "_unresolved.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_unresolved(body_dir: Path, unresolved: dict) -> None:
    path = body_dir / "bylaws" / "_unresolved.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(unresolved, indent=2) + "\n", encoding="utf-8")


def resolve_council_bylaw(fetcher, body_dir: Path, number: str,
                          unresolved: dict, direct_url: str | None = None,
                          cited_by: str = "") -> bool:
    """Ensure bylaws/{number}.pdf and .txt exist. True when resolved."""
    pdf_path = body_dir / "bylaws" / f"{number}.pdf"
    txt_path = body_dir / "bylaws" / f"{number}.txt"
    if pdf_path.exists():
        if not txt_path.exists():
            extract_pdf_text(pdf_path, txt_path)
        return True
    entry = unresolved.get(number, {"attempts": 0, "cited_by": []})
    if cited_by and cited_by not in entry["cited_by"]:
        entry["cited_by"].append(cited_by)
    if entry["attempts"] >= MAX_BYLAW_ATTEMPTS or entry.get("oversize"):
        unresolved[number] = entry
        return False
    candidates = ([direct_url] if direct_url else []) + [
        u.format(n=number) for u in COUNCIL_BYLAW_URLS
    ]
    for url in candidates:
        result = download_file(fetcher, url, pdf_path)
        if result is True:
            extract_pdf_text(pdf_path, txt_path)
            unresolved.pop(number, None)
            return True
        if result == OVERSIZE:
            # Terminal: the bylaw exists but is too large to commit.
            entry["oversize"] = True
            entry["url"] = url
            entry["last_attempt"] = dt.date.today().isoformat()
            unresolved[number] = entry
            log.info("bylaw %s recorded as oversized reference", number)
            return False
    entry["attempts"] += 1
    entry["last_attempt"] = dt.date.today().isoformat()
    unresolved[number] = entry
    log.warning("unresolved bylaw %s (attempt %d)", number, entry["attempts"])
    return False


def record_bylaw(m, ref, number, pdf_rel, txt_rel, resolved) -> None:
    for b in m["bylaws"]:
        if b.get("number") == number or b.get("ref") == ref:
            b.update(pdf=pdf_rel, txt=txt_rel, resolved=resolved)
            return
    m["bylaws"].append(
        {"ref": ref, "number": number, "pdf": pdf_rel, "txt": txt_rel,
         "resolved": resolved}
    )


def meeting_texts(mdir: Path) -> str:
    chunks = []
    for txt in sorted(mdir.rglob("*.txt")):
        try:
            chunks.append(txt.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(chunks)


# --------------------------------------------------------------------------
# City Council
# --------------------------------------------------------------------------

def probe_council_date(fetcher, dstr: str) -> set[str]:
    """Return the set of meeting type codes that exist for a date folder."""
    pattern = re.compile(
        r"(regu|pspc|cfsc|phea|spec)(\d{8})ag\.(?:htm|pdf)", re.IGNORECASE
    )
    found: set[str] = set()
    # Directory listings are disabled (403) on the live server; remember the
    # first failure so later dates skip straight to the per-type probes.
    if fetcher.council_dir_listing_ok:
        try:
            resp = fetcher.get(f"{COUNCIL_BASE}/{dstr}/", allow_404=True,
                               light=True)
        except RequestException:
            fetcher.council_dir_listing_ok = False
            resp = None
        if resp is not None:
            for mm in pattern.finditer(resp.text):
                if mm.group(2) == dstr:
                    found.add(mm.group(1).lower())
            if found:
                return found
    # Fallback: probe each type's agenda page directly.
    for code in COUNCIL_TYPES:
        url = f"{COUNCIL_BASE}/{dstr}/{code}{dstr}ag.htm"
        try:
            resp = fetcher.get(url, allow_404=True, light=True)
        except RequestException:
            continue
        if resp is not None and b"<html" in resp.content[:2000].lower():
            found.add(code)
    return found


def council_download_docs(fetcher, body_dir, key, m, today, counters):
    dstr = m["date"].replace("-", "")
    code = m["type"]
    mdir = body_dir / "meetings" / key
    mdir.mkdir(parents=True, exist_ok=True)

    # Agenda: HTML (best source for bylaw links) + PDF + extracted text.
    htm_path = mdir / "agenda.htm"
    if not htm_path.exists():
        try:
            resp = fetcher.get(
                f"{COUNCIL_BASE}/{dstr}/{code}{dstr}ag.htm", allow_404=True
            )
        except RequestException as exc:
            log.warning("agenda fetch failed for %s: %s", key, exc)
            resp = None
        if resp is not None:
            htm_path.write_text(resp.text, encoding="utf-8")
    pdf_path = mdir / "agenda.pdf"
    for url in (
        f"{COUNCIL_BASE}/{dstr}/documents/{code}{dstr}ag.pdf",
        f"{COUNCIL_BASE}/{dstr}/{code}{dstr}ag.pdf",
    ):
        if download_file(fetcher, url, pdf_path) is True:
            break
    txt_path = mdir / "agenda.txt"
    if not txt_path.exists():
        if htm_path.exists():
            txt_path.write_text(
                html_to_text(htm_path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
        elif pdf_path.exists():
            extract_pdf_text(pdf_path, txt_path)
    if txt_path.exists():
        m["agenda"] = {
            "url": f"{COUNCIL_BASE}/{dstr}/{code}{dstr}ag.htm",
            "pdf": f"meetings/{key}/agenda.pdf" if pdf_path.exists() else None,
            "txt": f"meetings/{key}/agenda.txt",
        }

    # Minutes (published ~2 weeks after the meeting).
    min_path = mdir / "minutes.pdf"
    min_url = f"{COUNCIL_BASE}/{dstr}/documents/{code}{dstr}min.pdf"
    if not min_path.exists() and should_check_minutes(m, today):
        if download_file(fetcher, min_url, min_path) is True:
            counters["new_minutes"] += 1
    if min_path.exists():
        quality = extract_pdf_text(min_path, mdir / "minutes.txt")
        m["minutes"] = {
            "url": min_url,
            "published": True,
            "pdf": f"meetings/{key}/minutes.pdf",
            "txt": f"meetings/{key}/minutes.txt",
            "text_quality": quality,
        }
    elif not m.get("minutes"):
        m["minutes"] = {"url": min_url, "published": False}


def council_bylaws(fetcher, body_dir, key, m, unresolved):
    mdir = body_dir / "meetings" / key
    direct: dict[str, str] = {}
    htm_path = mdir / "agenda.htm"
    if htm_path.exists():
        soup = BeautifulSoup(htm_path.read_text(encoding="utf-8"),
                             "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "bylaws.vancouver.ca" not in href:
                continue
            mm = re.search(r"(\d{3,5})[a-zA-Z]*\.pdf", href, re.IGNORECASE)
            if mm:
                direct[mm.group(1)] = href
    numbers = set(BYLAW_NUM_RE.findall(meeting_texts(mdir))) | set(direct)
    for n in sorted(numbers):
        resolved = resolve_council_bylaw(
            fetcher, body_dir, n, unresolved, direct.get(n), cited_by=key
        )
        record_bylaw(
            m, f"By-law No. {n}", n,
            f"bylaws/{n}.pdf" if resolved else None,
            f"bylaws/{n}.txt" if resolved else None,
            resolved,
        )


def process_council(fetcher, body_dir, today, args):
    manifest = load_manifest(body_dir, "vancouver_city_council")
    counters = {"new_meetings": 0, "new_minutes": 0}
    start, end = scrape_window(manifest, today, args.window_start)
    log.info("council: window %s..%s", start, end)

    # Discovery: probe date folders inside the window.
    date = start
    while date <= end:
        iso = date.isoformat()
        dstr = date.strftime("%Y%m%d")
        known = [m for m in manifest["meetings"].values() if m["date"] == iso]
        probed_empty = iso in manifest["probed_empty"]
        final_empty = probed_empty and (
            (today - date).days > EMPTY_PROBE_FINAL_DAYS
        )
        if not known and not final_empty:
            types_found = probe_council_date(fetcher, dstr)
            if args.dry_run:
                if types_found:
                    log.info("dry-run: %s -> %s", iso, sorted(types_found))
            elif types_found:
                manifest["probed_empty"].pop(iso, None)
                for code in sorted(types_found):
                    meeting_entry(
                        manifest, f"{iso}_{code}", iso, code,
                        COUNCIL_TYPES[code],
                        f"{COUNCIL_BASE}/{dstr}/", today, counters,
                    )
            else:
                manifest["probed_empty"][iso] = today.isoformat()
        date += dt.timedelta(days=1)

    if args.dry_run:
        return counters

    # Prune ancient negative-cache entries (loop never revisits them).
    cutoff = (today - dt.timedelta(days=90)).isoformat()
    manifest["probed_empty"] = {
        d: seen for d, seen in manifest["probed_empty"].items() if d >= cutoff
    }

    # Document + bylaw phase for every incomplete meeting.
    unresolved = load_unresolved(body_dir)
    for key in sorted(manifest["meetings"]):
        m = manifest["meetings"][key]
        if m["status"] in ("complete", "stale"):
            continue
        council_download_docs(fetcher, body_dir, key, m, today, counters)
        council_bylaws(fetcher, body_dir, key, m, unresolved)
        update_status(m, body_dir / "meetings" / key, today)
    save_unresolved(body_dir, unresolved)

    manifest["backfill_complete"] = True
    save_manifest(body_dir, manifest)
    return counters


# --------------------------------------------------------------------------
# Park Board
# --------------------------------------------------------------------------

def parkboard_classify(name: str) -> str:
    upper = name.upper()
    if upper.startswith("AGENDA"):
        return "agenda"
    if upper.startswith("MINUTES"):
        return "minutes"
    if upper.startswith(PARK_ATTACHMENT_PREFIXES):
        return "attachment"
    if upper.startswith("BYLAW"):
        return "bylaw"
    return "other"


def parkboard_slug(link_text: str) -> str:
    text = link_text.lower()
    if "committee" in text:
        return "committee"
    if "special" in text:
        return "special"
    return "regular"


def process_parkboard(fetcher, body_dir, today, args):
    manifest = load_manifest(body_dir, "vancouver_park_board")
    counters = {"new_meetings": 0, "new_minutes": 0}
    start, end = scrape_window(manifest, today, args.window_start)
    log.info("parkboard: window %s..%s", start, end)

    for year in range(start.year, end.year + 1):
        index_url = f"{PARKBOARD_BASE}/{year}/index.htm"
        resp = fetcher.get(index_url, allow_404=True)
        if resp is None:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            mm = re.search(r"(\d{8})/index\.htm$", a["href"])
            if not mm:
                continue
            try:
                date = dt.datetime.strptime(mm.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if not start <= date <= end:
                continue
            slug = parkboard_slug(a.get_text(" ", strip=True))
            key = f"{date.isoformat()}_{slug}"
            if args.dry_run:
                log.info("dry-run: parkboard %s", key)
                continue
            meeting_entry(
                manifest, key, date.isoformat(), slug,
                f"Park Board ({slug.title()})",
                urljoin(index_url, a["href"]), today, counters,
            )

    if args.dry_run:
        return counters

    # Consolidated park bylaws: fetch once.
    consolidated = body_dir / "bylaws" / "all-park-bylaws-2024.pdf"
    if download_file(fetcher, PARK_CONSOLIDATED_BYLAWS, consolidated) is True:
        extract_pdf_text(consolidated, consolidated.with_suffix(".txt"))

    unresolved = load_unresolved(body_dir)
    for key in sorted(manifest["meetings"]):
        m = manifest["meetings"][key]
        if m["status"] in ("complete", "stale"):
            continue
        mdir = body_dir / "meetings" / key
        mdir.mkdir(parents=True, exist_ok=True)
        try:
            resp = fetcher.get(m["source_url"], allow_404=True)
        except RequestException as exc:
            log.warning("parkboard folder fetch failed %s: %s", key, exc)
            resp = None
        if resp is not None:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.lower().endswith(".pdf"):
                    continue
                url = urljoin(m["source_url"], href)
                name = url.rsplit("/", 1)[-1]
                kind = parkboard_classify(name)
                if kind == "agenda":
                    if download_file(fetcher, url, mdir / "agenda.pdf") is True:
                        extract_pdf_text(mdir / "agenda.pdf",
                                         mdir / "agenda.txt")
                        m["agenda"] = {
                            "url": url,
                            "pdf": f"meetings/{key}/agenda.pdf",
                            "txt": f"meetings/{key}/agenda.txt",
                        }
                elif kind == "minutes":
                    if not (mdir / "minutes.pdf").exists():
                        if download_file(fetcher, url, mdir / "minutes.pdf") is True:
                            counters["new_minutes"] += 1
                    if (mdir / "minutes.pdf").exists():
                        quality = extract_pdf_text(mdir / "minutes.pdf",
                                                   mdir / "minutes.txt")
                        m["minutes"] = {
                            "url": url,
                            "published": True,
                            "pdf": f"meetings/{key}/minutes.pdf",
                            "txt": f"meetings/{key}/minutes.txt",
                            "text_quality": quality,
                        }
                elif kind == "attachment":
                    dest = mdir / "attachments" / name
                    if download_file(fetcher, url, dest) is True:
                        extract_pdf_text(dest, dest.with_suffix(".txt"))
                        rel = f"meetings/{key}/attachments/{name}"
                        if not any(att["name"] == name
                                   for att in m["attachments"]):
                            m["attachments"].append({
                                "name": name,
                                "pdf": rel,
                                "txt": rel.rsplit(".", 1)[0] + ".txt",
                            })
                elif kind == "bylaw":
                    dest = body_dir / "bylaws" / name
                    if download_file(fetcher, url, dest) is True:
                        extract_pdf_text(dest, dest.with_suffix(".txt"))
                        record_bylaw(
                            m, name, None, f"bylaws/{name}",
                            f"bylaws/{name.rsplit('.', 1)[0]}.txt", True,
                        )
        if not m.get("minutes"):
            m["minutes"] = {"url": None, "published": False}

        # Council-numbered bylaws cited in park board documents.
        numbers = set(BYLAW_NUM_RE.findall(meeting_texts(mdir)))
        for n in sorted(numbers):
            resolved = resolve_council_bylaw(
                fetcher, body_dir, n, unresolved, cited_by=key
            )
            record_bylaw(
                m, f"By-law No. {n}", n,
                f"bylaws/{n}.pdf" if resolved else None,
                f"bylaws/{n}.txt" if resolved else None,
                resolved,
            )
        update_status(m, mdir, today)
    save_unresolved(body_dir, unresolved)

    manifest["backfill_complete"] = True
    save_manifest(body_dir, manifest)
    return counters


# --------------------------------------------------------------------------
# School Board
# --------------------------------------------------------------------------

def vsb_api_list(fetcher, api: str, folder_path: str) -> list:
    """List one media-library folder via the CMS BrandingSearch API."""
    resp = fetcher.post(
        f"{api}/_ci/ps",
        data={
            "Path": "MediaLib/BrandingSearch",
            "Args": json.dumps({"Path": folder_path, "Deep": 1}),
        },
    )
    payload = resp.json()
    if not payload.get("IsSuccess"):
        log.warning("vsb api listing failed for %s", folder_path)
        return []
    return payload.get("Result") or []


def vsb_meeting_type(prefix: str) -> str:
    prefix = prefix.lower()
    if "delegation" in prefix:
        return "delegation"
    if "special" in prefix:
        return "special"
    return "board"


def vsb_discover_documents(fetcher, start: dt.date, end: dt.date) -> dict:
    """Discover VSB board meeting documents via the CMS media-library API.

    The media URLs contain unpredictable hashes, so they can never be
    constructed — only discovered. The meetings page embeds the library
    folder tree as JSON; files are then listed per folder via a POST API.
    Returns {(date, mtype): {"agenda": url, "minutes": url}}.
    """
    resp = fetcher.get(VSB_LISTING_PAGE)
    html = resp.text
    api_match = VSB_API_RE.search(html)
    api = api_match.group(1) if api_match else VSB_API_FALLBACK
    tree_match = VSB_TREE_RE.search(html)
    if not tree_match:
        raise RuntimeError(
            "VSB meetings page no longer embeds the media-library tree — "
            "page structure may have changed"
        )
    tree = json.loads(tree_match.group(1))

    docs: dict = {}
    for item in tree:
        if not item.get("IsFolder"):
            continue
        year_match = re.fullmatch(r"(\d{4})-(\d{4})", item.get("Caption", ""))
        if not year_match:
            continue
        parts = [p for p in item["FolderPathDisplay"].split("/") if p.strip()]
        if len(parts) < 2 or parts[-2] not in ("Agendas", "Minutes"):
            continue
        school_year = (
            dt.date(int(year_match.group(1)), 9, 1),
            dt.date(int(year_match.group(2)), 8, 31),
        )
        if school_year[1] < start or school_year[0] > end:
            continue
        folder_path = f"{item['FolderPath']}{item['Id']}/"
        for sub in vsb_api_list(fetcher, api, folder_path):
            if not sub.get("IsFolder") or sub.get("Caption") != "Board":
                continue
            sub_path = f"{sub['FolderPath']}{sub['Id']}/"
            for f in vsb_api_list(fetcher, api, sub_path):
                if f.get("IsFolder"):
                    continue
                mm = VSB_NAME_RE.match(f.get("Name", "").strip())
                if not mm:
                    continue
                prefix, doctype, year, mon, day = mm.groups()
                month = MONTHS.get(mon.lower())
                if month is None:
                    continue
                try:
                    date = dt.date(int(year), month, int(day))
                except ValueError:
                    continue
                mtype = vsb_meeting_type(prefix)
                docs.setdefault((date, mtype), {})[doctype.lower()] = (
                    f["Url"]
                )
    if not docs:
        raise RuntimeError(
            "VSB media-library API yielded zero meeting documents — "
            "page structure may have changed"
        )
    return docs


def vsb_policy_index(fetcher, body_dir: Path) -> dict:
    index: dict = {}
    try:
        resp = fetcher.get(VSB_POLICY_PAGE, allow_404=True)
    except RequestException as exc:
        log.warning("vsb policy page fetch failed: %s", exc)
        resp = None
    if resp is not None:
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            mm = VSB_POLICY_FILE_RE.search(a["href"])
            if not mm:
                continue
            num, slug = mm.group(1), mm.group(2)
            index[num] = {
                "title": a.get_text(" ", strip=True)
                or slug.replace("-", " ").title(),
                "slug": slug,
                "url": urljoin(VSB_POLICY_PAGE, a["href"]),
            }
    if index:
        path = body_dir / "bylaws" / "policy_index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    else:
        log.error("VSB policy index scrape yielded nothing")
    return index


def process_vsb(fetcher, body_dir, today, args):
    manifest = load_manifest(body_dir, "vancouver_school_board")
    counters = {"new_meetings": 0, "new_minutes": 0}
    start, end = scrape_window(manifest, today, args.window_start)
    log.info("vsb: window %s..%s", start, end)

    docs = vsb_discover_documents(fetcher, start, end)
    if args.dry_run:
        for (date, mtype), urls in sorted(docs.items()):
            if start <= date <= end:
                log.info("dry-run: vsb %s_%s -> %s", date, mtype,
                         sorted(urls))
        return counters

    policy_index = vsb_policy_index(fetcher, body_dir)

    for (date, mtype), urls in sorted(docs.items()):
        if not start <= date <= end:
            continue
        key = f"{date.isoformat()}_{mtype}"
        label = {
            "special": "Special Board Meeting",
            "delegation": "Public Delegation Board Meeting",
        }.get(mtype, "Board Meeting")
        m = meeting_entry(
            manifest, key, date.isoformat(), mtype, label,
            VSB_LISTING_PAGE, today, counters,
        )
        if m["status"] in ("complete", "stale"):
            continue
        mdir = body_dir / "meetings" / key
        mdir.mkdir(parents=True, exist_ok=True)
        if "agenda" in urls:
            if download_file(fetcher, urls["agenda"], mdir / "agenda.pdf") is True:
                extract_pdf_text(mdir / "agenda.pdf", mdir / "agenda.txt")
                m["agenda"] = {
                    "url": urls["agenda"],
                    "pdf": f"meetings/{key}/agenda.pdf",
                    "txt": f"meetings/{key}/agenda.txt",
                }
        if "minutes" in urls:
            if not (mdir / "minutes.pdf").exists():
                if download_file(fetcher, urls["minutes"], mdir / "minutes.pdf") is True:
                    counters["new_minutes"] += 1
            if (mdir / "minutes.pdf").exists():
                quality = extract_pdf_text(mdir / "minutes.pdf",
                                           mdir / "minutes.txt")
                m["minutes"] = {
                    "url": urls["minutes"],
                    "published": True,
                    "pdf": f"meetings/{key}/minutes.pdf",
                    "txt": f"meetings/{key}/minutes.txt",
                    "text_quality": quality,
                }
        if not m.get("minutes"):
            m["minutes"] = {"url": None, "published": False}

        # Policies referenced in the meeting documents.
        refs = set(VSB_POLICY_REF_RE.findall(meeting_texts(mdir)))
        for num in sorted(refs, key=int):
            info = policy_index.get(num) or policy_index.get(str(int(num)))
            if not info:
                continue
            name = f"policy-{int(num):02d}-{info['slug']}.pdf"
            dest = body_dir / "bylaws" / name
            if download_file(fetcher, info["url"], dest) is True:
                extract_pdf_text(dest, dest.with_suffix(".txt"))
                record_bylaw(
                    m, f"Policy {num}", num, f"bylaws/{name}",
                    f"bylaws/{name[:-4]}.txt", True,
                )
        update_status(m, mdir, today)

    manifest["backfill_complete"] = True
    save_manifest(body_dir, manifest)
    return counters


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------

def run_git(args: list[str], check: bool = True):
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=check, capture_output=True, text=True,
    )


def git_start() -> None:
    run_git(["pull", "--rebase", "--autostash"])


def git_finish(summary: str) -> None:
    run_git(["add", *BODY_DIRS.values()])
    if run_git(["diff", "--cached", "--quiet"], check=False).returncode == 0:
        log.info("no changes to commit")
        return
    run_git(["commit", "-m", summary])
    for attempt in range(3):
        if run_git(["push"], check=False).returncode == 0:
            log.info("pushed: %s", summary)
            return
        log.warning("push rejected (attempt %d), rebasing", attempt + 1)
        run_git(["pull", "--rebase"], check=False)
    raise RuntimeError("git push failed after 3 attempts")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

BODY_FUNCS = {
    "council": process_council,
    "parkboard": process_parkboard,
    "vsb": process_vsb,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", choices=[*BODY_FUNCS, "all"], default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="discovery only: print what would be scraped")
    parser.add_argument("--no-git", action="store_true",
                        help="skip git pull/commit/push")
    parser.add_argument("--window-start", type=dt.date.fromisoformat,
                        default=None, metavar="YYYY-MM-DD",
                        help="override the discovery window start date")
    parser.add_argument("--log-dir", type=Path, default=None,
                        help="also write a rotating log file here")
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log_dir:
        args.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            args.log_dir / "scrape.log", maxBytes=2_000_000, backupCount=5,
        ))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    lock_path = Path(tempfile.gettempdir()) / "vancouver_scrape.lock"
    lock_file = open(lock_path, "w")  # noqa: SIM115 - held for process life
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another scrape run is in progress; exiting")
        return 0

    today = dt.date.today()
    use_git = not (args.no_git or args.dry_run)
    if use_git:
        git_start()

    fetcher = Fetcher(dry_run=args.dry_run)
    bodies = list(BODY_FUNCS) if args.body == "all" else [args.body]
    results: dict[str, dict | None] = {}
    for name in bodies:
        body_dir = REPO_ROOT / BODY_DIRS[name]
        try:
            results[name] = BODY_FUNCS[name](fetcher, body_dir, today, args)
        except Exception:
            log.exception("body %s failed", name)
            results[name] = None

    parts = []
    for name in bodies:
        c = results[name]
        parts.append(
            f"{name} failed" if c is None
            else f"{name} +{c['new_meetings']}/+{c['new_minutes']}"
        )
    summary = f"scrape: {today} — {', '.join(parts)}"
    log.info(summary)

    if use_git:
        git_finish(summary)

    return 1 if all(c is None for c in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

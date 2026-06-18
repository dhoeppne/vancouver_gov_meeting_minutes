#!/usr/bin/env python3
"""Email new meeting-report PDFs as attachments.

Usage: send_email.py [--from a@x.com] [--to b@y.com] report1.pdf [report2.pdf ...]

SMTP settings come from the environment (set them in
~/vancouver_scraper/.env, sourced by nightly.sh):
    SMTP_SERVER    e.g. sandbox.smtp.mailtrap.io, live.smtp.mailtrap.io
    SMTP_PORT      465 (SSL) or 587/2525 (STARTTLS)
    SMTP_USERNAME  SMTP login (Mailtrap: the inbox user, or "api" for sending)
    SMTP_PASSWORD  SMTP password / API token
    SMTP_FROM      the From: address (required to send; --from overrides)
    SMTP_TO        recipient(s): one address or a comma-separated list, e.g.
                   "a@x.com, b@y.com" (required to send; --to overrides)

From and To are independent — different addresses and domains are fine, and
neither is tied to the SMTP login (Mailtrap's username is not an email
address). If SMTP_SERVER is unset the script logs and exits 0 — email is
optional, so a missing config never fails the nightly run.
"""

import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from _env import load_env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", default=None,
                        help="recipient(s), comma-separated (default: $SMTP_TO)")
    parser.add_argument("--from", dest="from_addr", default=None,
                        help="sender address (default: $SMTP_FROM)")
    parser.add_argument("--subject", default=None, help="override subject")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the message and report it, but don't connect/send")
    parser.add_argument("pdfs", nargs="+", type=Path, help="report PDFs")
    args = parser.parse_args()

    load_env()  # pick up ~/vancouver_scraper/.env when run standalone
    server = os.environ.get("SMTP_SERVER")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    # From and To are independent and may be different addresses/domains. The
    # From: is separate from the SMTP login too — Mailtrap's username is a hash
    # (sandbox) or the literal "api" (live), never an email address.
    from_addr = args.from_addr or os.environ.get("SMTP_FROM", "")
    # SMTP_TO / --to may be a single address or a comma-separated list.
    to_raw = args.to or os.environ.get("SMTP_TO", "")
    recipients = [a.strip() for a in to_raw.split(",") if a.strip()]

    pdfs = [p for p in args.pdfs if p.is_file()]
    if not pdfs:
        print("no report PDFs to send; skipping email", file=sys.stderr)
        return 0

    names = ", ".join(p.name for p in pdfs)
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = args.subject or f"New Vancouver meeting reports: {names}"
    msg.set_content(
        "New meeting report PDFs are attached:\n\n"
        + "\n".join(f"  - {p.name}" for p in pdfs)
    )
    for p in pdfs:
        msg.add_attachment(
            p.read_bytes(), maintype="application", subtype="pdf",
            filename=p.name,
        )

    if args.dry_run:
        print(f"[dry-run] would send via {server or '(SMTP_SERVER unset)'}:{port}"
              f" as {username or '(SMTP_USERNAME unset)'}")
        print(f"[dry-run] From: {from_addr or '(set SMTP_FROM)'}")
        print(f"[dry-run] To:   {', '.join(recipients) or '(set SMTP_TO or --to)'}"
              f" [{len(recipients)} recipient(s)]")
        print(f"[dry-run] Subject: {msg['Subject']}")
        for p in pdfs:
            print(f"[dry-run] attach: {p.name} ({p.stat().st_size:,} bytes)")
        return 0

    if not server:
        print("SMTP_SERVER unset; skipping email", file=sys.stderr)
        return 0
    if not from_addr:
        print("SMTP_FROM (sender) is required; skipping email", file=sys.stderr)
        return 1
    if not recipients:
        print("SMTP_TO (recipient) is required; skipping email", file=sys.stderr)
        return 1

    try:
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(server, port, context=ctx) as s:
                s.login(username, password)
                s.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(server, port) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(username, password)
                s.send_message(msg, to_addrs=recipients)
    except Exception as exc:  # noqa: BLE001 - email must never crash the run
        print(f"email failed: {exc}", file=sys.stderr)
        return 1
    print(f"emailed {len(pdfs)} report(s) to {', '.join(recipients)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

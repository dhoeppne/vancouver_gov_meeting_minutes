#!/usr/bin/env python3
"""Email new meeting-report PDFs as attachments.

Usage: send_email.py --to addr@example.com report1.pdf [report2.pdf ...]

SMTP settings come from the environment (set them in
~/vancouver_scraper/.env, sourced by nightly.sh):
    SMTP_SERVER    e.g. sandbox.smtp.mailtrap.io, live.smtp.mailtrap.io
    SMTP_PORT      465 (SSL) or 587/2525 (STARTTLS)
    SMTP_USERNAME  SMTP login (Mailtrap: the inbox user, or "api" for sending)
    SMTP_PASSWORD  SMTP password / API token
    SMTP_FROM      the From: address — REQUIRED with Mailtrap, since the SMTP
                   username there is not an email address. Falls back to
                   SMTP_USERNAME only when that looks like an address.

If SMTP_SERVER is unset the script logs and exits 0 — email is optional, so a
missing config never fails the nightly run.
"""

import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="recipient address")
    parser.add_argument("--subject", default=None, help="override subject")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the message and report it, but don't connect/send")
    parser.add_argument("pdfs", nargs="+", type=Path, help="report PDFs")
    args = parser.parse_args()

    server = os.environ.get("SMTP_SERVER")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    # The From: address is separate from the SMTP login. Providers like
    # Mailtrap use a non-email username (an inbox hash, or the literal "api"),
    # so SMTP_FROM must carry a real address; only reuse the username as From
    # when it already looks like one.
    from_addr = os.environ.get("SMTP_FROM") or (
        username if "@" in username else ""
    )
    if not from_addr:
        print("warning: no valid From address (set SMTP_FROM)", file=sys.stderr)

    pdfs = [p for p in args.pdfs if p.is_file()]
    if not pdfs:
        print("no report PDFs to send; skipping email", file=sys.stderr)
        return 0

    names = ", ".join(p.name for p in pdfs)
    msg = EmailMessage()
    msg["From"] = from_addr or args.to
    msg["To"] = args.to
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
        print(f"[dry-run] From: {msg['From']}")
        print(f"[dry-run] To: {args.to}")
        print(f"[dry-run] Subject: {msg['Subject']}")
        for p in pdfs:
            print(f"[dry-run] attach: {p.name} ({p.stat().st_size:,} bytes)")
        return 0

    if not server:
        print("SMTP_SERVER unset; skipping email", file=sys.stderr)
        return 0

    try:
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(server, port, context=ctx) as s:
                s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(server, port) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(username, password)
                s.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - email must never crash the run
        print(f"email failed: {exc}", file=sys.stderr)
        return 1
    print(f"emailed {len(pdfs)} report(s) to {args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

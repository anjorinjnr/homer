#!/usr/bin/env python3
"""
gmail_search.py — Search Gmail and return matching emails via gogcli.

Supports multiple Google accounts (primary, homer, personal, etc.).
Uses the gogcli wrapper (Path B pattern) for performance and reliability.
"""

import argparse
import json
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from gmail_fetch import html_to_text
from google_auth import DEFAULT_ACCOUNT, load_google_credentials, require_scopes

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
BODY_MAX_CHARS = 2000


def get_access_token(account: str) -> str:
    creds = load_google_credentials(account)
    require_scopes(creds, account, GMAIL_READONLY_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


def normalize_date(date_str: str) -> str:
    """Normalize Gmail date header to '%Y-%m-%d %H:%M UTC'."""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return date_str


def fetch_message(token: str, msg_id: str) -> dict:
    """Fetch a single message and format it for Homer."""
    data = gogcli.run(token, "gmail", "get", msg_id)
    headers = data.get("headers", {})
    body = html_to_text(data.get("body") or "").strip()

    if len(body) > BODY_MAX_CHARS:
        body = body[:BODY_MAX_CHARS] + f"\n[truncated — {len(body)} chars total]"

    return {
        "id": msg_id,
        "thread_id": data.get("message", {}).get("threadId", ""),
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "date": normalize_date(headers.get("date", "")),
        "body": body,
    }


def main():
    parser = argparse.ArgumentParser(description="Search Gmail via gogcli.")
    parser.add_argument("--query", required=True, help="Gmail search query")
    parser.add_argument("--limit", type=int, default=5, help="Max emails to return (default: 5)")
    parser.add_argument("--account", required=True, help="Google account to use (e.g. primary, homer, personal)")
    args = parser.parse_args()

    try:
        token = get_access_token(args.account)
        search_results = gogcli.run(token, "gmail", "messages", "search", args.query, "--max", str(args.limit))
        
        messages = search_results.get("messages", [])
        emails = [fetch_message(token, m["id"]) for m in messages]
        print(json.dumps(emails, indent=2, ensure_ascii=False))

    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
contacts_search.py — Search Google Contacts via the gogcli wrapper.

Pilot for the gogcli integration pattern: Python owns OAuth (via google_auth.py)
and passes the access token to gogcli per call. gogcli does the API call.

Usage:
    python tools/contacts_search.py --query "smith"
    python tools/contacts_search.py --query "maya" --limit 3
    python tools/contacts_search.py --query "x" --account personal

Output (JSON array):
    [{"name": "...", "emails": [...], "phones": [...], "resource_name": "..."}]
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import DEFAULT_ACCOUNT, load_google_credentials, require_scopes

CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"


def get_access_token(account: str) -> str:
    creds = load_google_credentials(account)
    require_scopes(creds, account, CONTACTS_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


def normalize(payload: dict) -> list[dict]:
    """Flatten gogcli's `contacts search` response into Homer's contact shape.

    Observed shape: {"contacts": [{"resource", "name", "email", "phone"}, ...]}.
    Each item field is a string (or absent). Multi-valued contacts collapse to
    one value in gogcli's representation.
    """
    out = []
    for item in payload.get("contacts", []):
        email = item.get("email") or ""
        phone = item.get("phone") or ""
        out.append({
            "name": item.get("name") or "",
            "emails": [email] if email else [],
            "phones": [phone] if phone else [],
            "resource_name": item.get("resource") or "",
        })
    return out


def main():
    parser = argparse.ArgumentParser(description="Search Google Contacts via gogcli.")
    parser.add_argument("--query", required=True, help="Search query (name, email, or phone fragment)")
    parser.add_argument("--limit", type=int, default=10, help="Max contacts to return (default: 10)")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT,
                        help=f"Google account to search (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    try:
        token = get_access_token(args.account)
        payload = gogcli.run(token, "contacts", "search", args.query, "--max", str(args.limit))
        contacts = normalize(payload)
    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps(contacts, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

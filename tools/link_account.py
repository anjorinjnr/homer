#!/usr/bin/env python3
"""
link_account.py — Generate a portal URL for linking a Google account.

Homer sends this URL in WhatsApp/Telegram so the user can authorize
a Google account via the portal's OAuth flow.

Usage:
    python tools/link_account.py                     # primary (default)
    python tools/link_account.py --account maya      # named secondary

Output (JSON):
    {"url": "https://<portal>/dashboard/connections?link=primary", "account": "primary"}
"""

import json
import os
import sys
from urllib.parse import quote

PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "")

# A brand-new tenant always wants the household's primary account first
# (Gmail/Calendar/Drive). Defaulting here lets the agent emit the link
# in one tool call without prompting the user for an account name —
# secondary accounts are an opt-in, not the cold-start path.
DEFAULT_ACCOUNT = "primary"


def main():
    # Manual arg parsing to guarantee JSON output on all errors
    account = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--account" and i + 1 < len(args):
            account = args[i + 1]

    if not account:
        account = DEFAULT_ACCOUNT

    url = f"{PORTAL_BASE}/dashboard/connections?link={quote(account)}"
    print(json.dumps({"url": url, "account": account}))


if __name__ == "__main__":
    main()

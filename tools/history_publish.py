#!/usr/bin/env python3
"""history_publish.py — Manage share links for the public family history timeline.

Usage (via Homer exec tool):
    python tools/history_publish.py --generate [--expires-days 365]
    python tools/history_publish.py --status
    python tools/history_publish.py --rotate [--expires-days 365]
    python tools/history_publish.py --revoke

Environment:
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    HOMER_HOUSEHOLD_ID
    HOMER_HISTORY_BASE_URL  — base URL for the public timeline
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOOLS_DIR = str(REPO_ROOT / "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import history_store as hs


def _public_url(code: str) -> str:
    base = os.environ.get("HOMER_HISTORY_BASE_URL", "").rstrip("/")
    return f"{base}/h/{code}"


def _expires_at(days: int | None) -> str | None:
    if not days:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def do_generate(expires_days: int | None = None) -> None:
    hid = hs.household_id()
    existing = hs.get_share_link(hid)
    if existing:
        print(json.dumps({
            "status": "already_exists",
            "code": existing["code"],
            "url": _public_url(existing["code"]),
            "expires_at": existing.get("expires_at"),
            "note": "A share link already exists. Use --rotate to generate a new code.",
        }, indent=2))
        return

    row = hs.upsert_share_link(hid, _expires_at(expires_days))
    print(json.dumps({
        "status": "created",
        "code": row["code"],
        "url": _public_url(row["code"]),
        "expires_at": row.get("expires_at"),
        "note": (
            "Share this link with family members. Anyone with the code can view "
            "stories marked as 'shared' visibility."
        ),
    }, indent=2))


def do_status() -> None:
    hid = hs.household_id()
    row = hs.get_share_link(hid)
    if not row:
        print(json.dumps({"status": "no_share_link", "note": "Run --generate to create one."}))
        return

    expires_at = row.get("expires_at")
    expired = False
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            expired = datetime.now(timezone.utc) > exp
        except ValueError:
            pass

    viewer_log = row.get("viewer_log") or []
    print(json.dumps({
        "code": row["code"],
        "url": _public_url(row["code"]),
        "expires_at": expires_at,
        "expired": expired,
        "total_views": len(viewer_log),
        "created_at": row.get("created_at"),
    }, indent=2))


def do_rotate(expires_days: int | None = None) -> None:
    hid = hs.household_id()
    row = hs.upsert_share_link(hid, _expires_at(expires_days))
    print(json.dumps({
        "status": "rotated",
        "code": row["code"],
        "url": _public_url(row["code"]),
        "expires_at": row.get("expires_at"),
        "note": "Old code is now invalid. Anyone with the old link will need the new one.",
    }, indent=2))


def do_revoke() -> None:
    hid = hs.household_id()
    existing = hs.get_share_link(hid)
    if not existing:
        print(json.dumps({"status": "no_share_link"}))
        return
    hs.revoke_share_link(hid)
    print(json.dumps({
        "status": "revoked",
        "note": "Share link revoked. The public timeline is no longer accessible.",
    }))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage family history share links.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true", help="Create a share link")
    group.add_argument("--status", action="store_true", help="Show share link status")
    group.add_argument("--rotate", action="store_true", help="Generate a new share code")
    group.add_argument("--revoke", action="store_true", help="Revoke the share link")
    parser.add_argument("--expires-days", type=int, metavar="N",
                        help="Link expires after N days (default: never)")
    args = parser.parse_args()

    if args.generate:
        do_generate(args.expires_days)
    elif args.status:
        do_status()
    elif args.rotate:
        do_rotate(args.expires_days)
    elif args.revoke:
        do_revoke()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
rsvp_invite.py — Generate RSVP invite URLs for event guests.

Creates unique tokens and returns shareable links that guests can use
to RSVP via the portal web page. URLs are tenant-scoped; the portal
routes by `HOMER_HOUSEHOLD_ID`, which is injected into every container
at provision time (see analytics.identity.get_household_id).

Usage (via Homer exec tool):
    python tools/rsvp_invite.py --event-id mtb_colorado --guest "Jake"
    python tools/rsvp_invite.py --event-id mtb_colorado --all
    python tools/rsvp_invite.py --event-id mtb_colorado --public
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
HOMER_TOOLS = str(REPO_ROOT / "tools")

if HOMER_TOOLS not in sys.path:
    sys.path.insert(0, HOMER_TOOLS)
import event_store
import short_links
from analytics.identity import get_household_id

DEFAULT_BASE_URL = os.environ.get("PORTAL_BASE_URL", "")


def _require_household_id() -> str:
    """RSVP URLs are tenant-scoped; bail loudly if HOMER_HOUSEHOLD_ID is
    unset. A link without the hid segment would render a silent 404 on
    the portal, which is harder to diagnose than a refusal here."""
    hid = get_household_id()
    if not hid:
        print(json.dumps({
            "error": (
                "HOMER_HOUSEHOLD_ID is not set — cannot generate tenant-scoped "
                "RSVP URL. Run inside the provisioned container or export the env var."
            )
        }))
        sys.exit(1)
    return hid


def _invite_url(base_url: str, household_id: str, event_id: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/rsvp/{household_id}/{event_id}/{token}"


def _public_url(
    base_url: str, household_id: str, event_id: str, public_token: str
) -> str:
    return (
        f"{base_url.rstrip('/')}/rsvp/{household_id}/{event_id}/open/{public_token}"
    )


def _build_invite(
    event_id: str, guest: dict, base_url: str, household_id: str
) -> dict:
    """Generate token, mark invited, and return invite dict for a single guest."""
    token = event_store.generate_rsvp_token(event_id, guest["participant_id"])
    event_store.mark_invited(event_id, guest["participant_id"])
    url = _invite_url(base_url, household_id, event_id, token)
    result = {
        "guest": guest["name"],
        "token": token,
        "url": url,
        "rsvp_status": guest["rsvp_status"],
    }
    short = short_links.shorten_or_none(url, household_id=household_id, kind="rsvp")
    if short:
        result["short_url"] = short
    return result


def generate_invite(
    event_id: str, guest_name: str, base_url: str, household_id: str
) -> dict:
    """Generate an RSVP invite URL for a single guest."""
    match = event_store.find_guest_by_name(event_id, guest_name)
    if not match:
        return {"error": f"Guest '{guest_name}' not found in event '{event_id}'"}
    return _build_invite(event_id, match, base_url, household_id)


def generate_all_invites(
    event_id: str, base_url: str, household_id: str
) -> list[dict]:
    """Generate RSVP invite URLs for all guests in an event."""
    guests = event_store.list_guests(event_id)
    return [_build_invite(event_id, g, base_url, household_id) for g in guests]


def generate_public_link(
    event_id: str, base_url: str, household_id: str
) -> dict:
    """Generate a shareable public RSVP link for an event."""
    token = event_store.generate_public_token(event_id)
    url = _public_url(base_url, household_id, event_id, token)
    result = {
        "event_id": event_id,
        "public_token": token,
        "url": url,
    }
    short = short_links.shorten_or_none(url, household_id=household_id, kind="rsvp")
    if short:
        result["short_url"] = short
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate RSVP invite URLs for event guests.")
    parser.add_argument("--event-id", required=True, help="Event identifier")
    parser.add_argument("--guest", help="Guest name (for personal invite)")
    parser.add_argument("--all", action="store_true", help="Generate personal URLs for all guests")
    parser.add_argument("--public", action="store_true", help="Generate a shareable public RSVP link")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Portal base URL")

    args = parser.parse_args()

    if not args.guest and not args.all and not args.public:
        parser.error("One of --guest, --all, or --public is required")

    household_id = _require_household_id()

    if args.public:
        result = generate_public_link(args.event_id, args.base_url, household_id)
        print(json.dumps(result, indent=2))
    elif args.all:
        results = generate_all_invites(args.event_id, args.base_url, household_id)
        print(json.dumps({"event_id": args.event_id, "invites": results}, indent=2))
    else:
        result = generate_invite(args.event_id, args.guest, args.base_url, household_id)
        if "error" in result:
            print(json.dumps(result))
            sys.exit(1)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

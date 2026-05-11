#!/usr/bin/env python3
"""history_invite.py — Manage family history contributors.

Usage (via Homer exec tool):
    python tools/history_invite.py --invite --name "Mom" --phone 14125551234 [--relationship "Mom"] [--email mom@example.com]
    python tools/history_invite.py --verify --phone 14125551234
    python tools/history_invite.py --list
    python tools/history_invite.py --archive --contributor-id <uuid>
    python tools/history_invite.py --lookup --phone 14125551234

Environment:
    SUPABASE_URL, SUPABASE_SERVICE_KEY — Supabase project credentials
    HOMER_HOUSEHOLD_ID                 — current household (set by portal provisioning)
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOOLS_DIR = str(REPO_ROOT / "tools")
HOMER_VENV = str(REPO_ROOT / ".venv" / "bin" / "python")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import history_store as hs
import scope_store
from history_store import normalise_phone as _normalise_phone

_SCOPE_PREFIX = "family_history"
_HISTORY_FRONTEND_URL_DEFAULT = ""  # set HOMER_HISTORY_BASE_URL in env


def _scope_id(household_id: str) -> str:
    return f"{_SCOPE_PREFIX}_{household_id[:8]}"


def _invite_url(token: str) -> str:
    base = os.environ.get("HISTORY_FRONTEND_URL", _HISTORY_FRONTEND_URL_DEFAULT).strip() \
        or _HISTORY_FRONTEND_URL_DEFAULT
    return f"{base.rstrip('/')}/invite/{token}"


def _rebuild_guest_config() -> None:
    """Regenerate the guest workspace; log failures to stderr."""
    try:
        result = subprocess.run(
            [HOMER_VENV, f"{TOOLS_DIR}/build_context.py"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(
                f"WARNING: build_context.py exited {result.returncode}; "
                f"guest config may be stale.\nstderr: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as e:
        print(
            f"WARNING: failed to rebuild guest config: {e!r}; "
            "guest config may be stale.",
            file=sys.stderr,
        )


def do_invite(
    name: str,
    phone: str | None,
    email: str | None,
    relationship: str | None,
) -> None:
    if not phone and not email:
        print(json.dumps({"error": "Either --phone or --email is required"}))
        sys.exit(1)

    hid = hs.household_id()

    phone_warning = None
    if phone:
        phone, phone_warning = _normalise_phone(phone)

    if phone:
        existing = hs.get_contributor_by_phone(hid, phone)
        if existing and existing["status"] == "archived":
            print(json.dumps({
                "error": f"A contributor with this phone was previously archived (id: {existing['id']}). "
                         "Reactivation is not supported — contact the curator."
            }))
            sys.exit(1)
        if existing:
            out = {
                "status": "already_exists",
                "contributor_id": existing["id"],
                "display_name": existing["display_name"],
                "contributor_status": existing["status"],
            }
            if phone_warning:
                out["phone_warning"] = phone_warning
            print(json.dumps(out))
            return

    row = hs.create_contributor(
        household_id=hid,
        display_name=name,
        role="contributor",
        relationship=relationship,
        phone=phone,
        email=email,
    )
    out = {
        "status": "invited",
        "contributor_id": row["id"],
        "display_name": name,
        "phone": phone,
        "email": email,
        "contributor_status": "pending",
        "next_step": (
            "Contributor will be activated automatically when they send their first WhatsApp message. "
            "Share the household's WhatsApp number with them."
        ) if phone else (
            "Send the contributor a magic-link invitation via the curator portal."
        ),
    }
    invite_token = row.get("invite_token")
    if invite_token:
        out["invite_token"] = invite_token
        out["invite_url"] = _invite_url(invite_token)
    if phone_warning:
        out["phone_warning"] = phone_warning
    print(json.dumps(out, indent=2))


def do_verify(phone: str) -> None:
    hid = hs.household_id()
    phone = _normalise_phone(phone)[0]
    contributor = hs.get_contributor_by_phone(hid, phone)
    if not contributor:
        print(json.dumps({"error": f"No contributor found with phone {phone}"}))
        sys.exit(1)
    if contributor["status"] == "active":
        print(json.dumps({
            "status": "already_active",
            "contributor_id": contributor["id"],
            "display_name": contributor["display_name"],
        }))
        return
    if contributor["status"] == "archived":
        print(json.dumps({"error": "Contributor is archived and cannot be verified"}))
        sys.exit(1)
    updated = hs.activate_contributor(contributor["id"])
    print(json.dumps({
        "status": "activated",
        "contributor_id": updated.get("id", contributor["id"]),
        "display_name": updated.get("display_name", contributor["display_name"]),
    }, indent=2))


def do_list() -> None:
    hid = hs.household_id()
    rows = hs.list_contributors(hid)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "display_name": r["display_name"],
            "relationship": r.get("relationship"),
            "phone": r.get("phone"),
            "email": r.get("email"),
            "status": r["status"],
            "role": r["role"],
            "verified_at": r.get("verified_at"),
        })
    print(json.dumps(out, indent=2))


def do_archive(contributor_id: str) -> None:
    updated = hs.archive_contributor(contributor_id)
    if not updated:
        print(json.dumps({"error": f"Contributor {contributor_id} not found"}))
        sys.exit(1)

    # Also remove the contributor from the family_history scope envelope so
    # they no longer appear as a participant, and rebuild the guest config so
    # the change is reflected in allow_from / sender_map immediately.
    hid = hs.household_id()
    sid = _scope_id(hid)
    envelope = scope_store.get_scope(sid)
    scope_dirty = False
    if envelope:
        participants = envelope.get("participants", [])
        new_participants = [
            p for p in participants if p.get("contributor_id") != contributor_id
        ]
        if len(new_participants) != len(participants):
            envelope["participants"] = new_participants
            scope_store.update_scope(sid, envelope)
            scope_dirty = True

    if scope_dirty:
        _rebuild_guest_config()

    print(json.dumps({
        "status": "archived",
        "contributor_id": contributor_id,
        "scope_participant_removed": scope_dirty,
    }))


def do_lookup(phone: str) -> None:
    hid = hs.household_id()
    phone = _normalise_phone(phone)[0]
    contributor = hs.get_contributor_by_phone(hid, phone)
    if not contributor:
        print(json.dumps({"status": "not_found", "phone": phone}))
        return
    print(json.dumps({
        "contributor_id": contributor["id"],
        "display_name": contributor["display_name"],
        "relationship": contributor.get("relationship"),
        "status": contributor["status"],
        "role": contributor["role"],
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage family history contributors.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--invite", action="store_true",
                       help="Invite a new contributor")
    group.add_argument("--verify", action="store_true",
                       help="Activate a pending contributor by phone")
    group.add_argument("--list", action="store_true",
                       help="List all contributors")
    group.add_argument("--archive", action="store_true",
                       help="Archive a contributor")
    group.add_argument("--lookup", action="store_true",
                       help="Look up a contributor by phone")

    parser.add_argument("--name", help="Contributor display name (for --invite)")
    parser.add_argument("--phone", help="Phone number, digits only (for --invite, --verify, --lookup)")
    parser.add_argument("--email", help="Email address (for --invite web contributors)")
    parser.add_argument("--relationship", help="Relationship label, e.g. 'Mom', 'Grandpa' (for --invite)")
    parser.add_argument("--contributor-id", help="Contributor UUID (for --archive)")

    args = parser.parse_args()

    if args.invite:
        if not args.name:
            parser.error("--invite requires --name")
        do_invite(args.name, args.phone, args.email, args.relationship)
    elif args.verify:
        if not args.phone:
            parser.error("--verify requires --phone")
        do_verify(args.phone)
    elif args.list:
        do_list()
    elif args.archive:
        if not args.contributor_id:
            parser.error("--archive requires --contributor-id")
        do_archive(args.contributor_id)
    elif args.lookup:
        if not args.phone:
            parser.error("--lookup requires --phone")
        do_lookup(args.phone)


if __name__ == "__main__":
    main()

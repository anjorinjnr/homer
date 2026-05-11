#!/usr/bin/env python3
"""
manage_interaction.py — Ad-hoc interaction scope management for Homer.

Creates lightweight, channel-agnostic scopes for external contacts not tied
to an event.  When Homer contacts a painter, vendor, or contractor, an
interaction scope ensures their replies route to the guest agent.

Usage (via Homer exec tool):
    # WhatsApp (default):
    python tools/manage_interaction.py --create --name "Bob the Painter" --phone "+15551234567" --purpose "Quote for exterior painting"
    # Telegram:
    python tools/manage_interaction.py --create --name "Jake" --channel telegram --telegram-id 123456 --purpose "Fence repair estimate"
    # Email:
    python tools/manage_interaction.py --create --name "Acme Plumbing" --channel email --email "info@acmeplumbing.com" --purpose "Water heater replacement"
    # WhatsApp + email:
    python tools/manage_interaction.py --create --name "Bob" --phone "+15551234567" --email "bob@painters.co" --purpose "Quote"

    python tools/manage_interaction.py --list
    python tools/manage_interaction.py --close --scope-id int_bob_the_painter
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
_tools = str(REPO_ROOT / "tools")

if _tools not in sys.path:
    sys.path.insert(0, _tools)
import manage_guest as mg
import scope_store as ss

DEFAULT_EXPIRY_DAYS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_scope_id(name: str) -> str:
    """Turn a human name into a scope_id slug: int_<sanitized>."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return f"int_{slug}"


def _unique_scope_id(base: str) -> str:
    """Return *base* if unused, else append _2, _3, … until unique."""
    if ss.get_scope(base) is None:
        return base
    for i in range(2, 100):
        candidate = f"{base}_{i}"
        if ss.get_scope(candidate) is None:
            return candidate
    raise ValueError(f"Could not generate unique scope_id from {base}")


def _resolve_participant_id(
    channel: str,
    phone: str | None,
    telegram_id: str | None,
    email: str | None,
    whatsapp_group: str | None = None,
) -> str:
    """Resolve the primary participant_id from contact details."""
    if channel == "telegram":
        if not telegram_id:
            raise ValueError("--telegram-id is required for --channel telegram")
        return f"tg:{telegram_id}"
    if channel == "email":
        if not email:
            raise ValueError("--email is required for --channel email")
        return email.strip().lower()
    # whatsapp
    if whatsapp_group:
        # Group JIDs end in @g.us; strip any leading "+" or whitespace.
        jid = whatsapp_group.strip()
        if not jid.endswith("@g.us"):
            raise ValueError("--whatsapp-group must be a JID ending in @g.us")
        return jid
    if not phone:
        raise ValueError("--phone or --whatsapp-group is required for --channel whatsapp")
    return mg.phone_to_jid(phone)


def _default_expiry() -> str:
    """Return YYYY-MM-DD 30 days from now."""
    return (datetime.now(mg.LOCAL_TZ) + timedelta(days=DEFAULT_EXPIRY_DAYS)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def create_interaction(
    name: str,
    channel: str = "whatsapp",
    phone: str | None = None,
    telegram_id: str | None = None,
    email: str | None = None,
    purpose: str = "",
    expires: str | None = None,
    mode: str = "two_way",
    whatsapp_group: str | None = None,
) -> dict:
    """Create an interaction scope for an ad-hoc external contact.

    Idempotent: if an active interaction scope already exists for this
    participant, returns the existing scope instead of erroring.

    Returns dict with: status, scope_id, name, channel, contact,
                       expires, mode, scope_created
    """
    participant_id = _resolve_participant_id(
        channel, phone, telegram_id, email, whatsapp_group
    )

    def _existing_result(env: dict) -> dict:
        return {
            "status": "exists",
            "scope_id": env["scope_id"],
            "name": name,
            "channel": channel,
            "contact": participant_id,
            "expires": env.get("authorization", {}).get("expires_at"),
            "mode": env.get("mode", "two_way"),
            "scope_created": False,
        }

    # Idempotency: check by participant_id, then by email
    for env in ss.get_scopes_for_participant(participant_id):
        if env.get("scope_type") == ss.SCOPE_TYPE_INTERACTION:
            return _existing_result(env)
    if email:
        for env in ss.get_scopes_for_email(email):
            if env.get("scope_type") == ss.SCOPE_TYPE_INTERACTION:
                return _existing_result(env)

    # Generate scope_id and expiry
    scope_id = _unique_scope_id(_sanitize_scope_id(name))
    effective_expires = expires or _default_expiry()

    # Create scope envelope
    envelope = ss.make_interaction_envelope(
        scope_id=scope_id,
        name=name,
        participant_id=participant_id,
        channel=channel,
        email=email,
        purpose=purpose,
        expires=effective_expires,
        mode=mode,
    )
    ss.create_scope(envelope)

    # Add to ACL
    acl = mg.load_acl()
    acl_entry = {
        "name": name,
        "channel": channel,
        "added": mg.now_str(),
        "expires": effective_expires,
        "interaction_id": scope_id,
    }
    if channel == "whatsapp":
        if whatsapp_group:
            acl_entry["whatsapp_group"] = whatsapp_group.strip()
        else:
            acl_entry["phone"] = phone
    elif channel == "telegram":
        acl_entry["telegram_id"] = str(telegram_id)
    acl[participant_id] = acl_entry
    # Also add email entry for WA/TG contacts that have email
    if email and channel != "email":
        email_lower = email.strip().lower()
        if email_lower not in acl:
            acl[email_lower] = {
                "name": name,
                "channel": "email",
                "interaction_id": scope_id,
            }
    mg.save_acl(acl)

    # Update channel config allow_from
    if channel == "whatsapp":
        mg.add_to_allow_from(participant_id)
    elif channel == "telegram":
        mg.add_to_telegram_allowfrom(str(telegram_id))

    return {
        "status": "created",
        "scope_id": scope_id,
        "name": name,
        "channel": channel,
        "contact": participant_id,
        "expires": effective_expires,
        "mode": mode,
        "scope_created": True,
    }


def convert_to_two_way(scope_id: str) -> dict:
    """Upgrade a no-reply interaction scope to two-way.

    Used when a recipient we initially nudged in no-reply mode actually
    needs to come back with a question. Idempotent on already-two-way
    scopes.
    """
    env = ss.get_scope(scope_id)
    if env is None:
        raise ValueError(f"Scope '{scope_id}' not found")
    if env.get("scope_type") != ss.SCOPE_TYPE_INTERACTION:
        raise ValueError(f"Scope '{scope_id}' is not an interaction scope")
    previous = env.get("mode", "two_way")
    if previous != "two_way":
        ss.set_scope_mode(scope_id, "two_way")
    return {
        "status": "converted",
        "scope_id": scope_id,
        "previous_mode": previous,
        "mode": "two_way",
    }


def list_interactions() -> list[dict]:
    """List all active interaction scopes."""
    results = []
    for env in ss.list_active_scopes():
        if env.get("scope_type") != ss.SCOPE_TYPE_INTERACTION:
            continue
        participants = env.get("participants", [])
        expires = env.get("authorization", {}).get("expires_at")
        injected = env.get("context_layers", {}).get("injected", [])
        purpose = injected[0].get("content", "") if injected else ""
        results.append({
            "scope_id": env["scope_id"],
            "name": participants[0]["name"] if participants else "",
            "channel": participants[0].get("channel", "") if participants else "",
            "contact": participants[0].get("party_id", "") if participants else "",
            "purpose": purpose,
            "expires": expires,
        })
    return results


def close_interaction(scope_id: str) -> dict:
    """Terminate an interaction scope, remove from ACL, rebuild context."""
    env = ss.get_scope(scope_id)
    if env is None:
        raise ValueError(f"Scope '{scope_id}' not found")
    if env.get("scope_type") != "interaction":
        raise ValueError(f"Scope '{scope_id}' is not an interaction scope")

    ss.terminate_scope(scope_id)

    # Remove participants from ACL
    acl = mg.load_acl()
    removed_channels = set()
    for p in env.get("participants", []):
        pid = p.get("party_id", "")
        ch = p.get("channel", "")
        if pid in acl:
            del acl[pid]
            removed_channels.add((pid, ch))
        email = (p.get("email") or "").lower()
        if email and email in acl:
            del acl[email]
    mg.save_acl(acl)

    # Remove from channel config
    for pid, ch in removed_channels:
        mg.remove_guest_from_channel(pid, ch)

    return {
        "status": "closed",
        "scope_id": scope_id,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def do_create(args: argparse.Namespace) -> None:
    try:
        result = create_interaction(
            name=args.name,
            channel=args.channel,
            phone=args.phone,
            telegram_id=args.telegram_id,
            email=args.email,
            purpose=args.purpose or "",
            expires=args.expires,
            mode="no_reply" if args.no_reply else "two_way",
            whatsapp_group=args.whatsapp_group,
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    if result["scope_created"]:
        mg.rebuild_context()
        result["service_restarted"] = mg.restart_service("homer-guest")
    else:
        result["service_restarted"] = False

    print(json.dumps(result, indent=2))


def do_convert_to_two_way(scope_id: str) -> None:
    try:
        result = convert_to_two_way(scope_id)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    print(json.dumps(result, indent=2))


def do_list() -> None:
    interactions = list_interactions()
    print(json.dumps(interactions, indent=2))


def do_close(scope_id: str) -> None:
    try:
        result = close_interaction(scope_id)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    mg.rebuild_context()
    result["service_restarted"] = mg.restart_service("homer-guest")
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage ad-hoc interaction scopes for Homer."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Create an interaction scope")
    group.add_argument("--list", action="store_true", help="List active interaction scopes")
    group.add_argument("--close", action="store_true", help="Close an interaction scope")
    group.add_argument(
        "--convert-to-two-way", action="store_true",
        help="Upgrade an existing no-reply scope to two-way",
    )

    parser.add_argument("--name", help="Contact name")
    parser.add_argument("--phone", help="Phone number (e.g. +15551234567)")
    parser.add_argument("--channel", default="whatsapp",
                        choices=["whatsapp", "telegram", "email"],
                        help="Channel (default: whatsapp)")
    parser.add_argument("--telegram-id", help="Telegram user ID")
    parser.add_argument("--email", help="Email address")
    parser.add_argument("--whatsapp-group", help="WhatsApp group JID (ends in @g.us); use instead of --phone")
    parser.add_argument("--purpose", help="Brief description of the interaction")
    parser.add_argument("--expires", help="Expiry date YYYY-MM-DD (default: 30 days)")
    parser.add_argument(
        "--no-reply", action="store_true",
        help="Create as no-reply (outbound allowed; inbound from this participant suppressed)",
    )
    parser.add_argument("--scope-id", help="Scope ID (for --close / --convert-to-two-way)")

    args = parser.parse_args()

    if args.create:
        if not args.name:
            parser.error("--create requires --name")
        if args.whatsapp_group and args.channel != "whatsapp":
            parser.error("--whatsapp-group is only valid with --channel whatsapp")
        if args.channel == "whatsapp" and not (args.phone or args.whatsapp_group):
            parser.error("--create --channel whatsapp requires --phone or --whatsapp-group")
        if args.phone and args.whatsapp_group:
            parser.error("--phone and --whatsapp-group are mutually exclusive")
        if args.channel == "telegram" and not args.telegram_id:
            parser.error("--create --channel telegram requires --telegram-id")
        if args.channel == "email" and not args.email:
            parser.error("--create --channel email requires --email")
        do_create(args)
    elif args.list:
        do_list()
    elif args.close:
        if not args.scope_id:
            parser.error("--close requires --scope-id")
        do_close(args.scope_id)
    elif args.convert_to_two_way:
        if not args.scope_id:
            parser.error("--convert-to-two-way requires --scope-id")
        do_convert_to_two_way(args.scope_id)


if __name__ == "__main__":
    main()

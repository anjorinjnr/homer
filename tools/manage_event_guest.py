#!/usr/bin/env python3
"""
manage_event_guest.py — Event-specific guest management for Homer.

Wraps manage_guest.py with event validation and roster tracking.
For generic (non-event) guest operations, use manage_guest.py directly.

Usage (via Homer exec tool):
    # WhatsApp guest (default):
    python tools/manage_event_guest.py --add --event-id mtb_colorado --name "Jake" --phone "+15551234567"
    # Telegram guest:
    python tools/manage_event_guest.py --add --event-id mtb_colorado --name "Jake" --channel telegram --telegram-id 123456789

    python tools/manage_event_guest.py --remove --event-id mtb_colorado --name "Jake"
    python tools/manage_event_guest.py --remove --event-id mtb_colorado --phone "+15551234567"
    python tools/manage_event_guest.py --remove --event-id mtb_colorado --telegram-id 123456789
    python tools/manage_event_guest.py --list --event-id mtb_colorado
    python tools/manage_event_guest.py --expire-check
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
EVENTS_DIR = Path(os.environ["HOMER_EVENTS_DIR"]) if os.environ.get("HOMER_EVENTS_DIR") else REPO_ROOT / "context" / "events"
HOMER_TOOLS = str(REPO_ROOT / "tools")

if HOMER_TOOLS not in sys.path:
    sys.path.insert(0, HOMER_TOOLS)
# Repo root too, so `from tools.X import Y` resolves when run as a script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import manage_guest as mg
import event_store


def _get_db_path() -> Path:
    """Return scope DB path, honouring HOMER_SCOPE_DB env var (same as scope_store)."""
    env = os.environ.get("HOMER_SCOPE_DB")
    return Path(env) if env else REPO_ROOT / "context" / "scopes.db"


def _update_guest_summary(event_id: str) -> None:
    """Update the ## Guests summary line in status.md after guest mutations."""
    import re as _re
    status_path = EVENTS_DIR / event_id / "status.md"
    if not status_path.exists():
        return
    content = status_path.read_text(encoding="utf-8")
    summary = event_store.render_guest_summary(event_id)
    content = _re.sub(
        r"## Guests.*?(?=\n## |\Z)",
        summary + "\n",
        content,
        flags=_re.DOTALL,
    )
    status_path.write_text(content, encoding="utf-8")


# ── Event-aware operations ──────────────────────────────────────────────────


def add_event_guest(
    event_id: str,
    name: str,
    phone: str | None = None,
    expires: str | None = None,
    channel: str = "whatsapp",
    telegram_id: str | None = None,
    email: str | None = None,
) -> dict:
    """Add a guest to a shared event scope. Raises ValueError on invalid input.

    All guests for the same event share one scope (scope_id == event_id).
    First guest creates the scope; subsequent guests are added to it.
    """
    event_dir = EVENTS_DIR / event_id
    if not event_dir.exists():
        raise ValueError(f"Event '{event_id}' not found")

    acl_key = mg.resolve_participant_id(phone, channel, telegram_id)
    scope_id = event_id
    contact = str(telegram_id) if channel == "telegram" else phone

    # Register in ACL + channel config. mg.add_guest creates the scope on first guest;
    # on subsequent guests the UNIQUE constraint is silently caught (harmless).
    # If the guest is already in the ACL for a DIFFERENT event (multi-event participant),
    # skip the ACL/channel write and just enroll them in this event's scope below.
    # If they're already in THIS event, reject with duplicate error.
    existing_acl = mg.load_acl()
    already_in_acl = acl_key in existing_acl
    if already_in_acl:
        # Duplicate within the same event: check the scope's participant list.
        try:
            import scope_store as ss  # type: ignore[import-untyped]
            _db = _get_db_path()
            _env = ss.get_scope(scope_id, _db)
            if _env and any(p["party_id"] == acl_key for p in _env.get("participants", [])):
                raise ValueError(f"Guest {name} ({acl_key}) is already in event '{event_id}'")
        except ValueError:
            raise
        except Exception:
            pass
        result = {
            "status": "added",
            "name": name,
            "channel": channel,
            "contact": acl_key,
            "scope_created": False,
            "config_updated": False,
            "service_restarted": False,
            "welcome_message": f"Send a welcome message to {name} at chat_id {acl_key} on {channel}",
        }
    else:
        result = mg.add_guest(
            name=name,
            phone=phone,
            channel=channel,
            telegram_id=telegram_id,
            expires=expires,
            scope_id=scope_id,
            extra_acl={"event_id": event_id},
            context_source={"type": "event", "ref": event_id},
            rebuild=False,
        )

    # Ensure participant is in the shared scope envelope + scope_participants table.
    # update_scope syncs scope_participants from envelope.participants.
    # If the scope was previously terminated (all guests removed), reactivate it.
    # If no scope exists yet (e.g. first guest for this event is already in ACL from
    # a different event), create it now.
    try:
        import scope_store as ss  # type: ignore[import-untyped]
        db_path = _get_db_path()
        scope_env = ss.get_scope(scope_id, db_path)
        if scope_env is None:
            # First participant for this event; create the shared scope.
            # make_minimal_envelope already includes this participant, so no
            # further append or update_scope needed.
            envelope = ss.make_minimal_envelope(
                scope_id=scope_id,
                name=name,
                participant_id=acl_key,
                event_id=event_id,
                channel=channel,
                email=email,
                expires=expires or None,
                context_source={"type": "event", "ref": event_id},
            )
            ss.create_scope(envelope)
            result["scope_created"] = True
        elif scope_env:
            if scope_env.get("_status") == "terminated":
                ss.reactivate_scope(scope_id, db_path)
            new_p = {"party_id": acl_key, "name": name, "handle": contact or acl_key, "channel": channel}
            if email:
                new_p["email"] = email.strip().lower()
            scope_env.setdefault("participants", [])
            if not any(p["party_id"] == acl_key for p in scope_env["participants"]):
                scope_env["participants"].append(new_p)
            ss.update_scope(scope_id, scope_env, db_path)
    except Exception as e:
        raise ValueError(f"Shared scope update failed: {e}") from e

    # Add email address to ACL so nanobot's guest routing matches email senders.
    # Use raw lowercase (not normalized) because nanobot's email channel sends
    # the raw From: address as sender_id.
    if email:
        email_lower = email.strip().lower()
        acl = mg.load_acl()
        if email_lower not in acl:
            acl[email_lower] = {
                "name": name,
                "channel": "email",
                "event_id": event_id,
            }
            mg.save_acl(acl)

    try:
        event_store.add_guest(
            event_id=event_id,
            participant_id=acl_key,
            name=name,
            phone=phone,
            channel=channel,
        )
    except sqlite3.IntegrityError:
        pass  # Already exists in event_store (scope_store is source of truth for access)
    _update_guest_summary(event_id)
    _emit_guest_event("guest_added", acl_key, channel, event_id)
    return result


def do_add(
    event_id: str,
    name: str,
    phone: str | None = None,
    expires: str | None = None,
    channel: str = "whatsapp",
    telegram_id: str | None = None,
    email: str | None = None,
) -> None:
    try:
        result = add_event_guest(event_id, name, phone, expires, channel, telegram_id, email=email)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    mg.rebuild_context()
    result["service_restarted"] = mg.restart_service("homer-guest")
    result["event_id"] = event_id
    print(json.dumps(result, indent=2))


def remove_event_guest(
    event_id: str,
    name: str | None = None,
    phone: str | None = None,
    telegram_id: str | None = None,
    preserve_roster: bool = False,
) -> dict:
    """Remove a participant from the shared event scope. Raises ValueError if not found.

    Only terminates the scope if this was the last participant; otherwise just
    removes the participant from the envelope and scope_participants table.
    """
    acl = mg.load_acl()

    target_key = None
    if phone:
        target_key = mg.phone_to_jid(phone)
    elif telegram_id:
        target_key = f"tg:{telegram_id}"
    elif name:
        # Resolve by name via the event scope's participant list so multi-event guests
        # (whose ACL entry may carry a different event_id) are found correctly.
        try:
            import scope_store as ss  # type: ignore[import-untyped]
            _scope_env = ss.get_scope(event_id, _get_db_path())
            if _scope_env:
                for p in _scope_env.get("participants", []):
                    if p.get("name", "").lower() == name.lower():
                        target_key = p["party_id"]
                        break
        except Exception:
            pass
        # Fallback: ACL lookup by name + event_id (single-event guests)
        if not target_key:
            for key, info in acl.items():
                if info.get("name", "").lower() == name.lower() and info.get("event_id") == event_id:
                    target_key = key
                    break

    if not target_key or target_key not in acl:
        raise ValueError(f"Guest not found in ACL for event '{event_id}'")

    target_name = acl[target_key].get("name", "")
    target_channel = acl[target_key].get("channel", "whatsapp")

    # Handle shared scope: remove participant without terminating scope for others.
    # We bypass mg.remove_guest's scope termination and do it manually.
    try:
        import scope_store as ss  # type: ignore[import-untyped]
        db_path = _get_db_path()
        scope_env = ss.get_scope(event_id, db_path)
        if scope_env:
            remaining = [p for p in scope_env.get("participants", []) if p["party_id"] != target_key]
            scope_env["participants"] = remaining
            ss.update_scope(event_id, scope_env, db_path)
            if not remaining:
                ss.terminate_scope(event_id, db_path)
    except Exception as e:
        raise ValueError(f"Shared scope update failed: {e}") from e

    # Only remove from global ACL + channel config if the guest has no other active
    # scopes (e.g. they're also in a different event). If they are, leave them in
    # the ACL so they retain access via those other scopes.
    # Do NOT default to empty list on error — that would incorrectly evict the guest.
    import scope_store as ss  # type: ignore[import-untyped]
    db_path = _get_db_path()
    try:
        other_scopes = [
            s for s in ss.get_scopes_for_participant(target_key, db_path)
            if s.get("scope_id") != event_id
        ]
    except Exception as e:
        raise ValueError(f"Could not verify guest's other active scopes: {e}") from e

    config_updated = False
    if not other_scopes:
        del acl[target_key]
        mg.save_acl(acl)
        mg.remove_guest_from_channel(target_key, target_channel)
        config_updated = True

    if not preserve_roster:
        event_store.remove_guest(event_id, target_key)
        _update_guest_summary(event_id)
    _emit_guest_event("guest_removed", target_key, target_channel, event_id)
    return {
        "status": "removed",
        "name": target_name,
        "contact": target_key,
        "config_updated": config_updated,
        "service_restarted": False,
    }


def _emit_guest_event(event: str, acl_key: str, channel: str, scope_id: str) -> None:
    """Fire PostHog guest_added/removed. Fire-and-forget — never raises;
    analytics failures must not block the caller's success path."""
    try:
        from tools.analytics.events import track_guest_added, track_guest_removed
        from tools.analytics.identity import get_distinct_id
        distinct_id = get_distinct_id(acl_key, channel)
        if event == "guest_added":
            track_guest_added(distinct_id, scope_id=scope_id, channel=channel)
        elif event == "guest_removed":
            track_guest_removed(distinct_id, scope_id=scope_id, channel=channel)
    except Exception:
        pass


def do_remove(
    event_id: str,
    name: str | None = None,
    phone: str | None = None,
    telegram_id: str | None = None,
    preserve_roster: bool = False,
) -> None:
    try:
        result = remove_event_guest(event_id, name, phone, telegram_id, preserve_roster=preserve_roster)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    mg.rebuild_context()
    # Only restart homer-guest if the channel config changed (guest fully removed
    # from system). If they remain active in another event, config is unchanged.
    result["service_restarted"] = mg.restart_service("homer-guest") if result["config_updated"] else False
    result["event_id"] = event_id
    print(json.dumps(result, indent=2))


def do_list(event_id: str) -> None:
    guests = event_store.list_guests(event_id)
    print(json.dumps(guests, indent=2))


def do_expire_check() -> None:
    """Remove expired guests, update event rosters, then rebuild + restart."""
    expired = mg.expire_guests()

    # Remove expired guests from event_store and update summaries
    for entry in expired:
        eid = entry.get("event_id")
        if eid:
            event_store.remove_guest(eid, entry["contact"])
            _update_guest_summary(eid)

    if expired:
        mg.rebuild_context()
        mg.restart_service("homer-guest")

    print(json.dumps({
        "expired_count": len(expired),
        "expired": expired,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage guest access for Homer events.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true", help="Add a guest")
    group.add_argument("--remove", action="store_true", help="Remove a guest")
    group.add_argument("--list", action="store_true", help="List guests for an event")
    group.add_argument("--expire-check", action="store_true", help="Remove expired guests")

    parser.add_argument("--event-id", help="Event identifier")
    parser.add_argument("--name", help="Guest name")
    parser.add_argument("--phone", help="Guest phone number (e.g. +15551234567)")
    parser.add_argument("--channel", default="whatsapp", choices=["whatsapp", "telegram"],
                        help="Messaging channel (default: whatsapp)")
    parser.add_argument("--telegram-id", help="Guest Telegram user ID (for --channel telegram)")
    parser.add_argument("--email", help="Guest email address (enables inbound email routing)")
    parser.add_argument("--expires", help="Access expiry date (YYYY-MM-DD)")
    parser.add_argument("--preserve-roster", action="store_true",
                        help="Keep guest in events.db (used by --close to preserve RSVP history)")

    args = parser.parse_args()

    if args.add:
        if not args.event_id or not args.name:
            parser.error("--add requires --event-id and --name")
        if args.channel == "telegram" and not args.telegram_id:
            parser.error("--add --channel telegram requires --telegram-id")
        if args.channel == "whatsapp" and not args.phone:
            parser.error("--add --channel whatsapp requires --phone")
        do_add(args.event_id, args.name, args.phone, args.expires,
               channel=args.channel, telegram_id=args.telegram_id, email=args.email)
    elif args.remove:
        if not args.event_id:
            parser.error("--remove requires --event-id")
        if not args.name and not args.phone and not args.telegram_id:
            parser.error("--remove requires --name, --phone, or --telegram-id")
        do_remove(args.event_id, name=args.name, phone=args.phone, telegram_id=args.telegram_id,
                  preserve_roster=args.preserve_roster)
    elif args.list:
        if not args.event_id:
            parser.error("--list requires --event-id")
        do_list(args.event_id)
    elif args.expire_check:
        do_expire_check()


if __name__ == "__main__":
    main()

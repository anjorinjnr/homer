#!/usr/bin/env python3
"""
manage_guest.py — Generic guest management for Homer.

Shared infrastructure and core operations for adding/removing guests
from the ACL, scope store, and channel config. No event knowledge.

For event-specific guest operations (event validation, roster tracking),
use manage_event_guest.py which wraps this module.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE_DIR = REPO_ROOT / "context" / ".nanobot_workspace"
_events_dir = Path(os.environ["HOMER_EVENTS_DIR"]) if os.environ.get("HOMER_EVENTS_DIR") else REPO_ROOT / "context" / "events"
ACL_FILE = _events_dir / "guest_agent_acl.json"
NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
GUEST_NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "guest_config.json"
LOCAL_TZ = ZoneInfo("America/New_York")
HOMER_VENV = str(REPO_ROOT / ".venv" / "bin" / "python")
HOMER_TOOLS = str(REPO_ROOT / "tools")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def phone_to_jid(phone: str) -> str:
    """Convert a phone number to a WhatsApp JID.

    Strips +, spaces, dashes, parens. Appends @s.whatsapp.net.
    """
    digits = re.sub(r"[^\d]", "", phone)
    if not digits:
        raise ValueError(f"Invalid phone number: {phone}")
    return f"{digits}@s.whatsapp.net"


def resolve_participant_id(
    phone: str | None = None,
    channel: str = "whatsapp",
    telegram_id: str | None = None,
) -> str:
    """Resolve a participant ID from contact details."""
    if channel == "telegram":
        if not telegram_id:
            raise ValueError("telegram_id is required for channel=telegram")
        return f"tg:{telegram_id}"
    if not phone:
        raise ValueError("phone is required for channel=whatsapp")
    return phone_to_jid(phone)


def load_acl() -> dict:
    if ACL_FILE.exists():
        try:
            return json.loads(ACL_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_acl(acl: dict) -> None:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    ACL_FILE.write_text(json.dumps(acl, indent=2, ensure_ascii=False), encoding="utf-8")


def load_nanobot_config() -> dict:
    return _load_config(NANOBOT_CONFIG_PATH)


def save_nanobot_config(config: dict) -> None:
    _save_config(config, NANOBOT_CONFIG_PATH)


def _load_config(path: Path) -> dict:
    """Load a nanobot config JSON file."""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _save_config(config: dict, path: Path) -> None:
    """Write a nanobot config JSON file."""
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def add_to_allow_from(jid: str) -> bool:
    """Add a JID to the WhatsApp allow_from list in the GUEST nanobot config.

    WhatsApp guests are handled by the guest nanobot instance (port 18791),
    not the main instance. Returns True if added.
    """
    if not GUEST_NANOBOT_CONFIG_PATH.exists():
        return False
    config = _load_config(GUEST_NANOBOT_CONFIG_PATH)
    if not config:
        return False

    channels = config.get("channels", {})
    wa = channels.get("whatsapp", {})
    allow_from = wa.get("allow_from", [])

    added = False
    if jid not in allow_from:
        allow_from.append(jid)
        added = True

    if added:
        wa["allow_from"] = allow_from
        channels["whatsapp"] = wa
        config["channels"] = channels
        _save_config(config, GUEST_NANOBOT_CONFIG_PATH)

    return added


def remove_from_allow_from(jid: str) -> bool:
    """Remove a JID from the WhatsApp allow_from list in the GUEST nanobot config.

    Returns True if removed.
    """
    if not GUEST_NANOBOT_CONFIG_PATH.exists():
        return False
    config = _load_config(GUEST_NANOBOT_CONFIG_PATH)
    if not config:
        return False

    channels = config.get("channels", {})
    wa = channels.get("whatsapp", {})
    allow_from = wa.get("allow_from", [])

    original_len = len(allow_from)
    allow_from = [x for x in allow_from if x != jid]

    if len(allow_from) < original_len:
        wa["allow_from"] = allow_from
        channels["whatsapp"] = wa
        config["channels"] = channels
        _save_config(config, GUEST_NANOBOT_CONFIG_PATH)
        return True
    return False


def add_to_telegram_allowfrom(telegram_id: str) -> bool:
    """Add a Telegram user ID to the guest agent's telegram allowFrom list. Returns True if added."""
    if not GUEST_NANOBOT_CONFIG_PATH.exists():
        return False
    config = _load_config(GUEST_NANOBOT_CONFIG_PATH)
    if not config:
        return False

    channels = config.get("channels", {})
    tg = channels.get("telegram", {})
    allow = tg.get("allowFrom", [])
    tid = str(telegram_id)

    if tid not in allow:
        allow.append(tid)
        tg["allowFrom"] = allow
        channels["telegram"] = tg
        config["channels"] = channels
        _save_config(config, GUEST_NANOBOT_CONFIG_PATH)
        return True
    return False


def remove_from_telegram_allowfrom(telegram_id: str) -> bool:
    """Remove a Telegram user ID from the guest agent's telegram allowFrom list. Returns True if removed."""
    if not GUEST_NANOBOT_CONFIG_PATH.exists():
        return False
    config = _load_config(GUEST_NANOBOT_CONFIG_PATH)
    if not config:
        return False

    channels = config.get("channels", {})
    tg = channels.get("telegram", {})
    allow = tg.get("allowFrom", [])
    tid = str(telegram_id)

    if tid in allow:
        allow.remove(tid)
        tg["allowFrom"] = allow
        channels["telegram"] = tg
        config["channels"] = channels
        _save_config(config, GUEST_NANOBOT_CONFIG_PATH)
        return True
    return False


def remove_guest_from_channel(acl_key: str, channel: str) -> bool:
    """Remove a guest from the appropriate channel's allow_from list."""
    if channel == "telegram":
        # ACL key is "tg:{id}" — strip prefix
        tid = acl_key.removeprefix("tg:")
        return remove_from_telegram_allowfrom(tid)
    else:
        return remove_from_allow_from(acl_key)


def rebuild_context() -> None:
    """Run build_context.py to regenerate workspace files including guest workspace."""
    subprocess.run(
        [HOMER_VENV, f"{HOMER_TOOLS}/build_context.py"],
        capture_output=True, text=True, timeout=30,
    )


def restart_service(service: str = "homer-guest") -> bool:
    """Restart a Homer systemd service. Requires sudoers rule for the homer user.

    service: always 'homer-guest' — all guests (WhatsApp and Telegram) run on the guest agent.
    """
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", service],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def add_guest(
    name: str,
    phone: str | None = None,
    channel: str = "whatsapp",
    telegram_id: str | None = None,
    expires: str | None = None,
    scope_id: str | None = None,
    extra_acl: dict | None = None,
    context: str | None = None,
    context_source: dict | None = None,
    rebuild: bool = True,
) -> dict:
    """Add guest to ACL + scope store + channel config + rebuild + restart.

    scope_id: override default (default: rel_{participant_id_clean})
    extra_acl: extra fields merged into ACL entry (e.g. {"event_id": "mtb_colorado"})
    context: static context string injected into the scope at creation time
    context_source: opts scope into dynamic refresh (e.g. {"type": "event", "ref": "denver_mtb"})
    rebuild: if False, skip rebuild_context + restart (caller's responsibility)
    Returns dict with: status, name, channel, contact, scope_created,
                       config_updated, service_restarted, welcome_message
    """
    if channel == "telegram":
        if not telegram_id:
            print(json.dumps({"error": "--telegram-id is required for --channel telegram"}))
            sys.exit(1)
        participant_id = f"tg:{telegram_id}"
    else:
        if not phone:
            print(json.dumps({"error": "--phone is required for --channel whatsapp"}))
            sys.exit(1)
        participant_id = phone_to_jid(phone)

    # Check ACL for duplicate
    acl = load_acl()
    if participant_id in acl:
        print(json.dumps({"error": f"Guest {name} ({participant_id}) is already registered"}))
        sys.exit(1)

    # Build ACL entry
    entry: dict = {
        "name": name,
        "channel": channel,
        "added": now_str(),
        "expires": expires or "",
    }
    if channel == "whatsapp":
        entry["phone"] = phone
    else:
        entry["telegram_id"] = str(telegram_id)
    if extra_acl:
        entry.update(extra_acl)
    acl[participant_id] = entry
    save_acl(acl)

    # Create scope in scope store
    scope_created = False
    try:
        sys.path.insert(0, HOMER_TOOLS)
        import scope_store
        effective_scope_id = scope_id or f"rel_{participant_id.split('@')[0]}"
        # Build a minimal envelope — use extra_acl event_id if available, else a generic tag
        event_id = (extra_acl or {}).get("event_id", "guest")
        envelope = scope_store.make_minimal_envelope(
            scope_id=effective_scope_id,
            name=name,
            participant_id=participant_id,
            event_id=event_id,
            channel=channel,
            expires=expires or None,
            context_source=context_source,
        )
        # Static context: inject at creation time
        if context:
            envelope["context_layers"]["injected"] = [
                {"fragment_id": f"init_{effective_scope_id}", "content": context}
            ]
        scope_store.create_scope(envelope)
        scope_created = True
    except Exception as e:
        print(f"[warn] scope_store.create_scope failed: {e}", file=sys.stderr)

    # Rebuild context + restart (unless caller will do it after additional work)
    restarted = False
    if rebuild:
        rebuild_context()
        restarted = restart_service("homer-guest")

    return {
        "status": "added",
        "name": name,
        "channel": channel,
        "contact": participant_id,
        "scope_created": scope_created,
        "config_updated": True,
        "service_restarted": restarted,
        "welcome_message": f"Send a welcome message to {name} at chat_id {participant_id} on {channel}",
    }


def remove_guest(participant_id: str, channel: str, rebuild: bool = True) -> dict:
    """Remove guest from ACL + terminate all scopes + channel config + rebuild + restart.

    rebuild: if False, skip rebuild_context + restart (caller's responsibility)
    Returns dict with: status, name, contact, config_updated, service_restarted
    """
    acl = load_acl()

    if participant_id not in acl:
        print(json.dumps({"error": f"Guest not found in ACL: {participant_id}"}))
        sys.exit(1)

    target_name = acl[participant_id].get("name", "")

    # Remove from ACL
    del acl[participant_id]
    save_acl(acl)

    # Terminate all scopes for this participant
    try:
        sys.path.insert(0, HOMER_TOOLS)
        import scope_store
        scopes = scope_store.get_scopes_for_participant(participant_id)
        for env in scopes:
            scope_store.terminate_scope(env["scope_id"])
    except Exception as e:
        print(f"[warn] scope_store.terminate_scope failed: {e}", file=sys.stderr)

    # Rebuild context + restart (unless caller will do it after additional work)
    restarted = False
    if rebuild:
        rebuild_context()
        restarted = restart_service("homer-guest")

    return {
        "status": "removed",
        "name": target_name,
        "contact": participant_id,
        "config_updated": True,
        "service_restarted": restarted,
    }


def expire_guests() -> list[dict]:
    """Find and remove expired guests. Handles ACL + scope + channel cleanup.

    Does NOT call rebuild/restart — caller's responsibility.
    Returns list of expired dicts: {contact, name, channel, event_id (if present), ...}
    """
    acl = load_acl()
    today = now_str()
    expired = []

    for key, info in list(acl.items()):
        exp = info.get("expires", "")
        if exp and exp <= today:
            channel = info.get("channel", "whatsapp")
            entry = {
                "contact": key,
                "name": info.get("name", ""),
                "channel": channel,
            }
            # Include all extra ACL fields (e.g. event_id)
            for field in ("event_id",):
                if field in info:
                    entry[field] = info[field]
            expired.append(entry)
            del acl[key]
            # Terminate all scopes for this participant
            try:
                sys.path.insert(0, HOMER_TOOLS)
                import scope_store
                scopes = scope_store.get_scopes_for_participant(key)
                for env in scopes:
                    scope_store.terminate_scope(env["scope_id"])
            except Exception:
                pass

    # Expire interaction scopes by authorization.expires_at
    try:
        import scope_store  # already on sys.path from above
        for env in scope_store.list_active_scopes():
            if env.get("scope_type") != scope_store.SCOPE_TYPE_INTERACTION:
                continue
            exp = env.get("authorization", {}).get("expires_at")
            if not exp or exp > today:
                continue
            scope_store.terminate_scope(env["scope_id"])
            for p in env.get("participants", []):
                pid = p.get("party_id", "")
                if pid and pid in acl:
                    del acl[pid]
                email = (p.get("email") or "").lower()
                if email and email in acl:
                    del acl[email]
                expired.append({
                    "contact": pid or email,
                    "name": p.get("name", ""),
                    "channel": p.get("channel", ""),
                    "interaction_id": env["scope_id"],
                })
    except Exception:
        pass

    if expired:
        save_acl(acl)

    return expired


# ---------------------------------------------------------------------------
# CLI wrappers
# ---------------------------------------------------------------------------

def do_add(
    name: str,
    phone: str | None = None,
    channel: str = "whatsapp",
    telegram_id: str | None = None,
    expires: str | None = None,
) -> None:
    result = add_guest(
        name=name,
        phone=phone,
        channel=channel,
        telegram_id=telegram_id,
        expires=expires,
    )
    print(json.dumps(result, indent=2))


def do_remove(
    name: str | None = None,
    phone: str | None = None,
    telegram_id: str | None = None,
) -> None:
    acl = load_acl()

    # Resolve participant_id
    target_key = None
    if phone:
        target_key = phone_to_jid(phone)
    elif telegram_id:
        target_key = f"tg:{telegram_id}"
    elif name:
        for key, info in acl.items():
            if info.get("name", "").lower() == name.lower():
                target_key = key
                break

    if not target_key or target_key not in acl:
        print(json.dumps({"error": "Guest not found in ACL"}))
        sys.exit(1)

    target_channel = acl[target_key].get("channel", "whatsapp")
    result = remove_guest(target_key, target_channel)
    print(json.dumps(result, indent=2))


def do_list() -> None:
    """List all guests in ACL (no event filter)."""
    acl = load_acl()
    guests = []
    for key, info in acl.items():
        guests.append({
            "name": info.get("name", ""),
            "channel": info.get("channel", "whatsapp"),
            "contact": key,
            "phone": info.get("phone", ""),
            "telegram_id": info.get("telegram_id", ""),
            "status": info.get("status", ""),
            "added": info.get("added", ""),
            "expires": info.get("expires", ""),
        })
    print(json.dumps(guests, indent=2))


def do_expire_check() -> None:
    """Remove expired guests, then rebuild context and restart service."""
    expired = expire_guests()
    if expired:
        rebuild_context()
        restart_service("homer-guest")
    print(json.dumps({
        "expired_count": len(expired),
        "expired": expired,
    }, indent=2))


def update_lid(phone: str, lid: str) -> dict:
    """Update an existing guest's ACL entry with their WhatsApp LID.

    When Homer sends a first message to a guest, the bridge learns the
    guest's opaque LID. This function stores it in the ACL so that
    build_context can include it in sender_map.json and allow_from.

    Returns dict with: status, name, phone, lid, allow_from_updated
    """
    acl = load_acl()
    target_jid = phone_to_jid(phone)

    if target_jid not in acl:
        print(json.dumps({"error": f"Guest not found in ACL: {target_jid}"}))
        sys.exit(1)

    lid_digits = re.sub(r"[^\d]", "", lid)
    if not lid_digits:
        print(json.dumps({"error": f"Invalid LID: {lid}"}))
        sys.exit(1)

    entry = acl[target_jid]
    entry["lid"] = lid_digits
    save_acl(acl)

    # Add LID to guest allow_from so inbound messages from this LID are accepted
    allow_updated = add_to_allow_from(lid_digits)

    return {
        "status": "lid_updated",
        "name": entry.get("name", ""),
        "phone": phone,
        "lid": lid_digits,
        "allow_from_updated": allow_updated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic guest management for Homer.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true", help="Add a guest")
    group.add_argument("--remove", action="store_true", help="Remove a guest")
    group.add_argument("--list", action="store_true", help="List all guests")
    group.add_argument("--expire-check", action="store_true", help="Remove expired guests")
    group.add_argument("--update-lid", action="store_true", help="Update guest with WhatsApp LID")

    parser.add_argument("--name", help="Guest name")
    parser.add_argument("--phone", help="Guest phone number (e.g. +15551234567)")
    parser.add_argument("--channel", default="whatsapp", choices=["whatsapp", "telegram"],
                        help="Messaging channel (default: whatsapp)")
    parser.add_argument("--telegram-id", help="Guest Telegram user ID (for --channel telegram)")
    parser.add_argument("--expires", help="Access expiry date (YYYY-MM-DD)")
    parser.add_argument("--lid", help="WhatsApp LID prefix (for --update-lid)")

    args = parser.parse_args()

    if args.add:
        if not args.name:
            parser.error("--add requires --name")
        if args.channel == "telegram" and not args.telegram_id:
            parser.error("--add --channel telegram requires --telegram-id")
        if args.channel == "whatsapp" and not args.phone:
            parser.error("--add --channel whatsapp requires --phone")
        do_add(args.name, args.phone, args.channel, args.telegram_id, args.expires)
    elif args.remove:
        if not args.name and not args.phone and not args.telegram_id:
            parser.error("--remove requires --name, --phone, or --telegram-id")
        do_remove(name=args.name, phone=args.phone, telegram_id=args.telegram_id)
    elif args.list:
        do_list()
    elif args.expire_check:
        do_expire_check()
    elif args.update_lid:
        if not args.phone:
            parser.error("--update-lid requires --phone")
        if not args.lid:
            parser.error("--update-lid requires --lid")
        result = update_lid(args.phone, args.lid)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

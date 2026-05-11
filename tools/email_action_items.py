#!/usr/bin/env python3
"""
email_action_items.py — Track actionable emails that need follow-up.

When Homer's Gmail scan finds an actionable email, it persists the item here.
The morning briefing reads open items so unresolved actions surface daily
until the user addresses them.

Usage (via Homer exec tool):
    # Record an actionable email:
    python tools/email_action_items.py --add --subject "Dentist appointment" \
        --sender "office@dentist.com" --action "Confirm appointment for Friday" \
        --urgency today

    # List all open action items:
    python tools/email_action_items.py --list

    # Mark an item as done by ID:
    python tools/email_action_items.py --complete --id "abc123-..."

    # Mark an item as done by subject keyword:
    python tools/email_action_items.py --complete --subject "dentist"

    # Remove all action items:
    python tools/email_action_items.py --clear

Output is always JSON.
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
STATE_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "state"
ACTIONS_FILE = STATE_DIR / "email_actions.json"


def _load() -> list[dict]:
    if not ACTIONS_FILE.exists():
        return []
    try:
        data = json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ACTIONS_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _find_duplicate(entries: list[dict], email_id: str, subject: str,
                    sender: str) -> dict | None:
    for e in entries:
        existing_email_id = e.get("email_id", "")
        if email_id and existing_email_id == email_id:
            return e
        if not email_id and not existing_email_id \
                and e.get("subject", "") == subject \
                and e.get("sender", "") == sender:
            return e
    return None


def cmd_add(subject: str, sender: str, action: str, urgency: str,
            email_id: str | None = None) -> None:
    entries = _load()
    existing = _find_duplicate(entries, email_id or "", subject, sender)
    if existing:
        print(json.dumps({
            "status": "duplicate",
            "id": existing["id"],
            "subject": subject,
        }))
        return
    entry = {
        "id": str(uuid.uuid4()),
        "email_id": email_id or "",
        "subject": subject,
        "sender": sender,
        "action": action,
        "urgency": urgency,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    entries.append(entry)
    _save(entries)
    print(json.dumps({"status": "added", "id": entry["id"], "subject": subject}))


def cmd_list() -> None:
    entries = _load()
    print(json.dumps(entries, indent=2))


def cmd_complete(entry_id: str | None, subject_keyword: str | None) -> None:
    entries = _load()
    if entry_id:
        remaining = [e for e in entries if e.get("id") != entry_id]
        removed = len(entries) - len(remaining)
        if removed == 0:
            print(json.dumps({"status": "not_found", "id": entry_id}))
            sys.exit(1)
        _save(remaining)
        print(json.dumps({"status": "completed", "id": entry_id, "removed": removed}))
    elif subject_keyword:
        keyword = subject_keyword.lower()
        remaining = [e for e in entries if keyword not in e.get("subject", "").lower()]
        removed = len(entries) - len(remaining)
        if removed == 0:
            print(json.dumps({"status": "not_found", "subject": subject_keyword}))
            sys.exit(1)
        _save(remaining)
        print(json.dumps({"status": "completed", "subject": subject_keyword, "removed": removed}))


class _JsonParser(argparse.ArgumentParser):
    """ArgumentParser that outputs errors as JSON to stdout (tool contract)."""

    def error(self, message: str) -> None:
        print(json.dumps({"error": message}))
        sys.exit(1)


def main() -> None:
    parser = _JsonParser(description="Track actionable emails needing follow-up.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--complete", action="store_true")
    group.add_argument("--clear", action="store_true", help="Remove all action items")

    parser.add_argument("--id", dest="entry_id")
    parser.add_argument("--email-id")
    parser.add_argument("--subject")
    parser.add_argument("--sender")
    parser.add_argument("--action")
    parser.add_argument("--urgency", default="today",
                        help="Urgency level: today, this_week, low (default: today)")

    args = parser.parse_args()

    if args.add:
        missing = [f for f, v in [
            ("--subject", args.subject),
            ("--sender", args.sender),
            ("--action", args.action),
        ] if not v]
        if missing:
            print(json.dumps({"error": f"--add requires: {', '.join(missing)}"}))
            sys.exit(1)
        cmd_add(args.subject, args.sender, args.action, args.urgency,
                email_id=args.email_id)

    elif args.list:
        cmd_list()

    elif args.complete:
        if not args.entry_id and not args.subject:
            print(json.dumps({"error": "--complete requires --id or --subject"}))
            sys.exit(1)
        if args.entry_id and args.subject:
            print(json.dumps({"error": "--complete takes --id or --subject, not both"}))
            sys.exit(1)
        cmd_complete(args.entry_id, args.subject)

    elif args.clear:
        removed = len(_load())
        _save([])
        print(json.dumps({"status": "cleared", "removed": removed}))


if __name__ == "__main__":
    main()

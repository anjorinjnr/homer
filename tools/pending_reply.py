#!/usr/bin/env python3
"""
pending_reply.py — Track outbound messages waiting for a reply.

When Homer messages someone on behalf of a user, it records a pending follow-up here.
When that person's next message arrives, Homer checks this store, forwards the reply
to the waiting user, and clears the entry.

After --add and --complete, rebuilds USER.md via build_context.py so Homer sees
the updated state on the very next turn (same pattern as context_updater.py).

Usage (via Homer exec tool):
    # Record that we're waiting for Alex to reply about weekend plans:
    python tools/pending_reply.py --add --from alex --topic "weekend plans" \
        --notify-channel whatsapp --notify-recipient "1234567890@s.whatsapp.net"

    # List all pending replies:
    python tools/pending_reply.py --list

    # Check if Homer is waiting for anything from Alex:
    python tools/pending_reply.py --list --from alex

    # Mark a specific entry as received (by ID — preferred to avoid wiping sibling entries):
    python tools/pending_reply.py --complete --id "abc123-..."

    # Mark all pending entries for a person as received:
    python tools/pending_reply.py --complete --from alex

Output is always JSON.
"""

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PENDING_FILE = REPO_ROOT / "context" / "pending_replies.json"


def _load() -> list[dict]:
    if not PENDING_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: list[dict]) -> None:
    PENDING_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _rebuild_context() -> None:
    """Rebuild USER.md so Homer sees updated follow-ups on the next turn."""
    build_script = REPO_ROOT / "tools" / "build_context.py"
    subprocess.run(
        [sys.executable, str(build_script)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )


def cmd_add(
    from_name: str,
    topic: str,
    notify_channel: str,
    notify_recipient: str,
    party_id: str | None = None,
) -> None:
    entries = _load()
    entry = {
        "id": str(uuid.uuid4()),
        "from": from_name.lower().strip(),
        "topic": topic,
        "notify_channel": notify_channel,
        "notify_recipient": notify_recipient,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if party_id:
        entry["party_id"] = party_id.strip()
    entries.append(entry)
    _save(entries)
    _rebuild_context()
    print(json.dumps({
        "status": "added", "id": entry["id"],
        "from": entry["from"], "topic": topic,
        "party_id": entry.get("party_id", ""),
    }))


def cmd_list(from_name: str | None) -> None:
    entries = _load()
    if from_name:
        entries = [e for e in entries if e.get("from", "").lower() == from_name.lower().strip()]
    print(json.dumps(entries, indent=2))


def cmd_complete(from_name: str | None, entry_id: str | None) -> None:
    if not from_name and not entry_id:
        print(json.dumps({"error": "--complete requires --from or --id"}))
        sys.exit(1)
    entries = _load()
    if entry_id:
        remaining = [e for e in entries if e.get("id") != entry_id]
        removed = len(entries) - len(remaining)
        if removed == 0:
            print(json.dumps({"status": "not_found", "id": entry_id}))
            sys.exit(1)
        _save(remaining)
        _rebuild_context()
        print(json.dumps({"status": "completed", "id": entry_id, "removed": removed}))
    else:
        key = from_name.lower().strip()  # type: ignore[union-attr]
        remaining = [e for e in entries if e.get("from", "").lower() != key]
        removed = len(entries) - len(remaining)
        if removed == 0:
            print(json.dumps({"status": "not_found", "from": from_name}))
            sys.exit(1)
        _save(remaining)
        _rebuild_context()
        print(json.dumps({"status": "completed", "from": from_name, "removed": removed}))


class _JsonParser(argparse.ArgumentParser):
    """ArgumentParser that outputs errors as JSON to stdout (tool contract)."""

    def error(self, message: str) -> None:
        print(json.dumps({"error": message}))
        sys.exit(1)


def main() -> None:
    parser = _JsonParser(description="Track pending follow-ups.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--complete", action="store_true")

    parser.add_argument("--from", dest="from_name")
    parser.add_argument("--id", dest="entry_id")
    parser.add_argument("--topic")
    parser.add_argument("--notify-channel")
    parser.add_argument("--notify-recipient")
    parser.add_argument(
        "--party-id",
        dest="party_id",
        help="Recipient's exact identity (WhatsApp JID, tg:<id>, or email). "
             "When set, scope_store renders the pending follow-up only into "
             "the scope whose participant matches this party_id — avoiding "
             "name-collision across scopes.",
    )

    args = parser.parse_args()

    if args.add:
        missing = [f for f, v in [
            ("--from", args.from_name),
            ("--topic", args.topic),
            ("--notify-channel", args.notify_channel),
            ("--notify-recipient", args.notify_recipient),
        ] if not v]
        if missing:
            print(json.dumps({"error": f"--add requires: {', '.join(missing)}"}))
            sys.exit(1)
        cmd_add(
            args.from_name, args.topic, args.notify_channel,
            args.notify_recipient, party_id=args.party_id,
        )

    elif args.list:
        cmd_list(args.from_name)

    elif args.complete:
        if not args.from_name and not args.entry_id:
            print(json.dumps({"error": "--complete requires --from or --id"}))
            sys.exit(1)
        cmd_complete(args.from_name, args.entry_id)


if __name__ == "__main__":
    main()

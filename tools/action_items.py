#!/usr/bin/env python3
"""
action_items.py — Track open user action items across sources.

Replaces the older email-only `email_action_items.py`. An action item is
anything Homer surfaces to the user for follow-up: an actionable email, a
calendar prep task, a scope handoff, a chat ask, a manual capture, or
something Homer inferred. The morning brief reads open items so unresolved
actions surface until the user addresses them.

Each item carries:
  - description       user-facing sentence ("Pay water bill")
  - source            email | calendar | scope | chat | manual | inference
  - source_ref        source-specific metadata (subject/sender for email,
                      event_id/calendar_id for calendar, etc.)
  - urgency           today | this_week | low | none (fallback ordering)
  - due_at            optional ISO date or datetime (precise deadline)
  - status            open | done | snoozed
  - snoozed_until     ISO datetime when status=snoozed

Usage:
    python tools/action_items.py --list
    python tools/action_items.py --list --source email
    python tools/action_items.py --list --due-today
    python tools/action_items.py --list --status snoozed
    python tools/action_items.py --add --source email \\
        --description "Pay water bill" \\
        --source-ref '{"subject":"...","sender":"...","account":"primary"}' \\
        --urgency this_week --due 2026-05-15
    python tools/action_items.py --complete --id ai_a3b2c1de
    python tools/action_items.py --snooze --id ai_a3b2c1de --until 2026-05-20
    python tools/action_items.py --remove --id ai_a3b2c1de

Output is always JSON.
"""

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent.resolve()
STATE_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "state"
ITEMS_FILE = STATE_DIR / "action_items.json"

LOCAL_TZ = ZoneInfo("America/New_York")

VALID_SOURCES = {"email", "calendar", "scope", "chat", "manual", "inference"}
VALID_URGENCIES = {"today", "this_week", "low", "none"}
VALID_STATUSES = {"open", "done", "snoozed"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return f"ai_{uuid.uuid4().hex[:8]}"


def _load() -> list[dict]:
    if not ITEMS_FILE.exists():
        return []
    try:
        data = json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ITEMS_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _parse_due(raw: str) -> str:
    """Accept a date (YYYY-MM-DD) or full ISO datetime; return ISO string.

    Date-only inputs are stored as the date string itself — comparisons
    against `--due-today` treat the date portion only.
    """
    if not raw:
        return ""
    # Date-only first: Python 3.11+ datetime.fromisoformat accepts bare dates
    # and promotes them to midnight, which would mask date-precision input as
    # a datetime. Try date first to preserve the original precision.
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError(f"--due expects YYYY-MM-DD or ISO datetime, got '{raw}'") from exc


def _due_date(entry: dict) -> date | None:
    """Extract the date portion of due_at, or None."""
    raw = entry.get("due_at") or ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _norm(s: str) -> str:
    """Normalize a dedup-key component: strip whitespace, lowercase, collapse
    common reply/forward prefixes that Gmail adds on the same thread."""
    out = (s or "").strip().lower()
    while out.startswith(("re:", "fwd:", "fw:")):
        out = out.split(":", 1)[1].strip()
    return out


def _email_dedupe_key(source_ref: dict) -> tuple | None:
    """Email items dedupe on message_id when present, else (subject, sender,
    account). Normalized — `"Receipt"` and `"Re: Receipt"` dedupe, case
    variants dedupe — matching the cross-account event-dedup convention.
    Other sources have no automatic dedup — multiple manual or inference
    items with the same description are allowed."""
    mid = (source_ref.get("message_id") or "").strip()
    if mid:
        return ("mid", mid)
    subj = _norm(source_ref.get("subject") or "")
    sender = _norm(source_ref.get("sender") or "")
    account = _norm(source_ref.get("account") or "")
    if subj or sender:
        return ("ssa", subj, sender, account)
    return None


def _find_duplicate(entries: list[dict], source: str, source_ref: dict) -> dict | None:
    if source != "email":
        return None
    key = _email_dedupe_key(source_ref)
    if key is None:
        return None
    for e in entries:
        if e.get("source") != "email":
            continue
        if e.get("status") != "open":
            continue
        existing = _email_dedupe_key(e.get("source_ref") or {})
        if existing == key:
            return e
    return None


def cmd_add(source: str, description: str, source_ref: dict, urgency: str,
            due_at: str) -> None:
    entries = _load()
    existing = _find_duplicate(entries, source, source_ref)
    if existing:
        print(json.dumps({
            "status": "duplicate",
            "id": existing["id"],
            "description": description,
        }))
        return
    entry = {
        "id": _new_id(),
        "description": description,
        "source": source,
        "source_ref": source_ref,
        "urgency": urgency,
        "due_at": due_at,
        "status": "open",
        "snoozed_until": None,
        "created_at": _now_iso(),
        "completed_at": None,
    }
    entries.append(entry)
    _save(entries)
    print(json.dumps({"status": "added", "id": entry["id"], "description": description}))


def cmd_list(source: str | None, status: str | None, due_today: bool) -> None:
    entries = _load()
    if status == "all":
        filtered = list(entries)
    elif status:
        filtered = [e for e in entries if e.get("status") == status]
    else:
        filtered = [e for e in entries if e.get("status") == "open"]
    if source:
        filtered = [e for e in filtered if e.get("source") == source]
    if due_today:
        today = datetime.now(LOCAL_TZ).date()
        filtered = [e for e in filtered if _due_date(e) == today]
    print(json.dumps(filtered, indent=2))


def cmd_complete(entry_id: str) -> None:
    entries = _load()
    for e in entries:
        if e.get("id") == entry_id:
            if e.get("status") == "done":
                # Idempotent: keep the original completed_at — a re-run
                # from gmail-scan resolving the same item twice shouldn't
                # rewrite history.
                print(json.dumps({"status": "already_done", "id": entry_id}))
                return
            e["status"] = "done"
            e["completed_at"] = _now_iso()
            _save(entries)
            print(json.dumps({"status": "completed", "id": entry_id}))
            return
    print(json.dumps({"status": "not_found", "id": entry_id}))
    sys.exit(1)


def cmd_snooze(entry_id: str, until: str) -> None:
    entries = _load()
    for e in entries:
        if e.get("id") == entry_id:
            e["status"] = "snoozed"
            e["snoozed_until"] = until
            _save(entries)
            print(json.dumps({"status": "snoozed", "id": entry_id, "until": until}))
            return
    print(json.dumps({"status": "not_found", "id": entry_id}))
    sys.exit(1)


def cmd_remove(entry_id: str) -> None:
    entries = _load()
    remaining = [e for e in entries if e.get("id") != entry_id]
    if len(remaining) == len(entries):
        print(json.dumps({"status": "not_found", "id": entry_id}))
        sys.exit(1)
    _save(remaining)
    print(json.dumps({"status": "removed", "id": entry_id}))


class _JsonParser(argparse.ArgumentParser):
    """ArgumentParser that emits errors as JSON (tool contract)."""

    def error(self, message: str) -> None:
        print(json.dumps({"error": message}))
        sys.exit(1)


def main() -> None:
    parser = _JsonParser(description="Track open user action items.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--complete", action="store_true")
    group.add_argument("--snooze", action="store_true")
    group.add_argument("--remove", action="store_true")

    parser.add_argument("--id", dest="entry_id")
    parser.add_argument("--source")
    parser.add_argument("--description")
    parser.add_argument("--source-ref", dest="source_ref",
                        help="JSON object with source-specific fields")
    parser.add_argument("--urgency", default="this_week",
                        help=f"One of: {sorted(VALID_URGENCIES)} (default: this_week)")
    parser.add_argument("--due", dest="due",
                        help="Due date (YYYY-MM-DD) or full ISO datetime")
    parser.add_argument("--status",
                        help="Filter by status: open | done | snoozed | all")
    parser.add_argument("--due-today", dest="due_today", action="store_true",
                        help="Filter to items whose due_at date is today (local TZ)")
    parser.add_argument("--until",
                        help="Snooze target (YYYY-MM-DD or ISO datetime)")

    args = parser.parse_args()

    if args.add:
        missing = [f for f, v in [
            ("--source", args.source),
            ("--description", args.description),
        ] if not v]
        if missing:
            print(json.dumps({"error": f"--add requires: {', '.join(missing)}"}))
            sys.exit(1)
        if args.source not in VALID_SOURCES:
            print(json.dumps({
                "error": f"--source must be one of: {sorted(VALID_SOURCES)}",
            }))
            sys.exit(1)
        if args.urgency not in VALID_URGENCIES:
            print(json.dumps({
                "error": f"--urgency must be one of: {sorted(VALID_URGENCIES)}",
            }))
            sys.exit(1)
        try:
            ref = json.loads(args.source_ref) if args.source_ref else {}
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"--source-ref is not valid JSON: {exc}"}))
            sys.exit(1)
        if not isinstance(ref, dict):
            print(json.dumps({"error": "--source-ref must be a JSON object"}))
            sys.exit(1)
        try:
            due_at = _parse_due(args.due) if args.due else ""
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            sys.exit(1)
        cmd_add(args.source, args.description, ref, args.urgency, due_at)

    elif args.list:
        if args.status and args.status not in VALID_STATUSES and args.status != "all":
            print(json.dumps({
                "error": f"--status must be one of: {sorted(VALID_STATUSES)} or 'all'",
            }))
            sys.exit(1)
        if args.source and args.source not in VALID_SOURCES:
            print(json.dumps({
                "error": f"--source must be one of: {sorted(VALID_SOURCES)}",
            }))
            sys.exit(1)
        cmd_list(args.source, args.status, args.due_today)

    elif args.complete:
        if not args.entry_id:
            print(json.dumps({"error": "--complete requires --id"}))
            sys.exit(1)
        cmd_complete(args.entry_id)

    elif args.snooze:
        if not args.entry_id or not args.until:
            print(json.dumps({"error": "--snooze requires --id and --until"}))
            sys.exit(1)
        try:
            until = _parse_due(args.until)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            sys.exit(1)
        cmd_snooze(args.entry_id, until)

    elif args.remove:
        if not args.entry_id:
            print(json.dumps({"error": "--remove requires --id"}))
            sys.exit(1)
        cmd_remove(args.entry_id)


if __name__ == "__main__":
    main()

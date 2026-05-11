#!/usr/bin/env python3
"""
log_learning.py — Log bugs, feature requests, and prompt improvements Homer encounters.

Homer calls this whenever it makes a mistake, can't fulfill a request, or notices its
instructions handled a situation poorly. The log lives at context/learnings.md and is
reviewed by the developer to drive fixes and improvements.

This is a developer feedback channel — NOT for household facts (use context_updater.py)
and NOT for session memory (nanobot handles that). Only log things that require the
developer to change code, skills, or agent instructions.

Usage:
    python tools/log_learning.py --type bug     --desc "Used hardcoded totals instead of =SUMIF()"
    python tools/log_learning.py --type feature --desc "User wants to create recurring calendar events from natural language"
    python tools/log_learning.py --type prompt  --desc "Sheets skill missing SUMIF guidance for category summaries" --context "User asked for expense report"
    python tools/log_learning.py --list
    python tools/log_learning.py --list --filter-type bug
    python tools/log_learning.py --list --limit 5
    python tools/log_learning.py --clear

Types:
    bug     — Homer produced wrong output or behaved incorrectly
    feature — User asked for something Homer can't do yet
    prompt  — Homer's instructions or a SKILL.md need adjusting (missing guidance, edge case, ambiguous rule)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent.resolve()
LEARNINGS_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "learnings.md"

VALID_TYPES = ("bug", "feature", "prompt")
TYPE_EMOJI  = {"bug": "🐛", "feature": "💡", "prompt": "✏️"}


def append_entry(entry_type: str, desc: str, context: str = None) -> None:
    date = datetime.now().strftime("%Y-%m-%d")
    time = datetime.now().strftime("%H:%M")
    emoji = TYPE_EMOJI[entry_type]

    lines = [f"### {date} {time} · {emoji} {entry_type} · {desc}"]
    if context:
        lines.append(f"Context: {context}")
    lines.append("")

    if not LEARNINGS_FILE.exists():
        LEARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEARNINGS_FILE.write_text(
            "# Homer Learnings Log\n"
            "<!-- Appended by log_learning.py. Reviewed by developer to drive fixes. -->\n\n",
            encoding="utf-8",
        )

    with open(LEARNINGS_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(json.dumps({
        "logged": True,
        "type": entry_type,
        "desc": desc,
        "date": date,
    }))


def clear_entries() -> None:
    if not LEARNINGS_FILE.exists():
        print(json.dumps({"cleared": True, "entries_removed": 0}))
        return

    text = LEARNINGS_FILE.read_text(encoding="utf-8")
    count = sum(1 for line in text.splitlines() if line.startswith("### "))
    LEARNINGS_FILE.write_text(
        "# Homer Learnings Log\n"
        "<!-- Appended by log_learning.py. Reviewed by developer to drive fixes. -->\n\n",
        encoding="utf-8",
    )
    print(json.dumps({"cleared": True, "entries_removed": count}))


def list_entries(filter_type: str = None, limit: int = 20) -> None:
    if not LEARNINGS_FILE.exists():
        print(json.dumps({"entries": [], "total": 0}))
        return

    text = LEARNINGS_FILE.read_text(encoding="utf-8")
    blocks = []
    current = []

    for line in text.splitlines():
        if line.startswith("### "):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)

    if current:
        blocks.append("\n".join(current))

    # Parse and optionally filter
    entries = []
    for block in blocks:
        first = block.splitlines()[0]
        # Format: ### DATE TIME · EMOJI TYPE · DESC
        parts = first.lstrip("# ").split(" · ", 2)
        if len(parts) < 3:
            continue
        datetime_part = parts[0].strip()
        type_part = parts[1].strip().split()[-1]  # strip emoji
        desc_part = parts[2].strip()
        context_line = next((l for l in block.splitlines()[1:] if l.startswith("Context:")), None)

        entry = {
            "datetime": datetime_part,
            "type": type_part,
            "desc": desc_part,
        }
        if context_line:
            entry["context"] = context_line.replace("Context: ", "", 1)

        if filter_type and type_part != filter_type:
            continue
        entries.append(entry)

    # Most recent first
    entries = list(reversed(entries))[:limit]

    print(json.dumps({"entries": entries, "total": len(entries)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Log Homer learnings and feedback.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--type", choices=VALID_TYPES, help="Type of entry to log")
    group.add_argument("--list", action="store_true", help="List recent entries")
    group.add_argument("--clear", action="store_true", help="Clear all entries (after dev review)")

    parser.add_argument("--desc", help="Description of the bug, feature, correction, or learning")
    parser.add_argument("--context", help="Additional context about what triggered this")
    parser.add_argument("--limit", type=int, default=20, help="Max entries to show with --list (default: 20)")
    parser.add_argument("--filter-type", choices=VALID_TYPES, dest="filter_type",
                        help="Filter --list by type")

    args = parser.parse_args()

    if args.list:
        list_entries(filter_type=args.filter_type, limit=args.limit)
    elif args.clear:
        clear_entries()
    else:
        if not args.desc:
            print(json.dumps({"error": "--desc is required when logging an entry"}))
            sys.exit(1)
        append_entry(args.type, args.desc, context=args.context)


if __name__ == "__main__":
    main()

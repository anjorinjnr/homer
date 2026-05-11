#!/usr/bin/env python3
"""
announce_update.py — Remove a processed announcement from HEARTBEAT.md.

Usage (via Homer exec tool):
    python tools/announce_update.py --done "smart reminder"  # remove matching announcement
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
HEARTBEAT_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "HEARTBEAT.md"

ANNOUNCEMENTS_MARKER = "## Announcements"
USER_TASKS_MARKER = "## User Tasks"


def read_heartbeat() -> str:
    if not HEARTBEAT_FILE.exists():
        print(json.dumps({"error": "HEARTBEAT.md not found"}))
        sys.exit(1)
    return HEARTBEAT_FILE.read_text(encoding="utf-8")


def write_heartbeat(content: str) -> None:
    HEARTBEAT_FILE.write_text(content, encoding="utf-8")


def get_announcements_bounds(content: str) -> tuple[int, int]:
    """Return (start, end) indices of the Announcements section body."""
    ann_pos = content.find(ANNOUNCEMENTS_MARKER)
    user_tasks_pos = content.find(USER_TASKS_MARKER)
    if ann_pos == -1 or user_tasks_pos == -1 or ann_pos >= user_tasks_pos:
        return -1, -1
    # Start after the section header line
    start = content.index("\n", ann_pos) + 1
    return start, user_tasks_pos


def done(keyword: str) -> None:
    content = read_heartbeat()
    start, end = get_announcements_bounds(content)
    if start == -1:
        print(json.dumps({"error": "Announcements section not found"}))
        sys.exit(1)

    section = content[start:end]
    keyword_lower = keyword.lower()

    for m in re.finditer(r"(###\s+.+?)(?=\n###\s|\Z)", section, re.DOTALL):
        if keyword_lower in m.group(0).lower():
            block_start = start + m.start()
            block_end = start + m.end()
            title = m.group(0).split("\n")[0].lstrip("#").strip()
            updated = content[:block_start] + content[block_end:]
            write_heartbeat(updated)
            print(json.dumps({"status": "removed", "announcement": title}))
            return

    print(json.dumps({"error": f"No announcement matching '{keyword}' found"}))
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove a processed announcement from HEARTBEAT.md.")
    parser.add_argument("--done", metavar="KEYWORD", required=True,
                        help="Remove announcement matching keyword (case-insensitive)")
    args = parser.parse_args()
    done(args.done)


if __name__ == "__main__":
    main()

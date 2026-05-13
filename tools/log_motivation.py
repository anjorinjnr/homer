#!/usr/bin/env python3
"""
log_motivation.py — Append a motivation line to the rolling-last-7 log.

The morning-brief skill reads this file before composing today's
motivation line to avoid reuse, and writes back through this tool after
sending. Kept narrow on purpose — the brief is no longer one tool, and
this is the one piece of brief state that has to persist across runs.

Usage:
    python tools/log_motivation.py --line "Today, choose one small good thing."
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
STATE_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "state"
MOTIVATIONS_FILE = STATE_DIR / "recent_motivations.txt"
KEEP = 7


def _load() -> list[str]:
    """Return the last KEEP non-blank lines (oldest → newest).

    Defensive trim on read: if the file ever ends up with more than KEEP
    rows (manual edit, partial-write recovery, concurrent fan-out where
    two dispatches both appended before either trimmed), only the most
    recent KEEP are returned so the morning-brief skill never compares
    today's candidate motivation against an unbounded history."""
    if not MOTIVATIONS_FILE.exists():
        return []
    try:
        lines = MOTIVATIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-KEEP:]


def append(line: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    history = _load()
    history.append(line.strip())
    history = history[-KEEP:]
    MOTIVATIONS_FILE.write_text("\n".join(history) + "\n", encoding="utf-8")
    return len(history)


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a motivation line to the rolling log.")
    parser.add_argument("--line", required=True, help="The motivation line to log")
    args = parser.parse_args()

    if not args.line.strip():
        print(json.dumps({"error": "--line requires a non-empty value"}))
        sys.exit(1)

    kept = append(args.line)
    print(json.dumps({"status": "logged", "kept": kept}))


if __name__ == "__main__":
    main()

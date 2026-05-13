#!/usr/bin/env python3
"""
bootstrap_user_briefs.py — Ensure every brief recipient has a prompt file.

When PR-C ships the morning-brief skill, the heartbeat fans out per
recipient and reads `users/<recipient>.brief.md` (workspace-relative)
as that recipient's prompt. The recipient names come from the Morning
briefing block's `Recipients:` field — chat-routing names like
`primary` and lowercase first names, NOT the registry user names from
`manage_users.py --list`.

This tool parses the Morning briefing block's `Recipients:` from
`context/.nanobot_workspace/HEARTBEAT.md`, strips the `:channel`
suffix per recipient, dedups, and copies
`skills/morning-brief/default.brief.md` →
`context/.nanobot_workspace/users/<recipient>.brief.md` for any
recipient whose file is missing. **Idempotent**: existing files are
left alone (a user may have edited theirs).

Usage:
    python tools/bootstrap_user_briefs.py                   # all recipients
    python tools/bootstrap_user_briefs.py --recipient primary  # one recipient
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TEMPLATE = REPO_ROOT / "skills" / "morning-brief" / "default.brief.md"
# Workspace-relative so nanobot's `Prompt-file: users/{recipient}.brief.md`
# resolves correctly (nanobot anchors at `<homer>/context/.nanobot_workspace`).
WORKSPACE_DIR = REPO_ROOT / "context" / ".nanobot_workspace"
USERS_DIR = WORKSPACE_DIR / "users"
HEARTBEAT_FILE = WORKSPACE_DIR / "HEARTBEAT.md"


def parse_brief_recipients(heartbeat_text: str) -> list[str]:
    """Extract the Morning briefing block's recipient names from a
    HEARTBEAT.md string. Strips `:channel` suffixes (`primary:whatsapp`
    → `primary`), trims whitespace, and dedups while preserving the
    written order — matches what nanobot's `recipient_names()` does so
    the substitution paths line up.

    Returns an empty list when the Morning briefing block is missing or
    has no Recipients field.
    """
    block_match = re.search(
        r"^### Morning briefing\b.*?(?=^### |\Z)",
        heartbeat_text,
        re.MULTILINE | re.DOTALL,
    )
    if not block_match:
        return []
    block = block_match.group(0)
    rec_match = re.search(r"^Recipients:\s*(.+)$", block, re.MULTILINE)
    if not rec_match:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in rec_match.group(1).split(","):
        name = entry.rsplit(":", 1)[0].strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def bootstrap_recipient(recipient: str) -> dict:
    """Create users/<recipient>.brief.md from default.brief.md if missing.

    Returns one of:
      {"recipient": name, "status": "created", "path": "..."}
      {"recipient": name, "status": "exists",  "path": "..."}  # left alone
      {"recipient": name, "status": "error",   "error": "..."}

    Recipient names with `/` or `..` are rejected — they would let an
    `--recipient` value (or a typo in HEARTBEAT.md's Recipients field)
    write outside USERS_DIR. Nanobot's prompt-file resolver catches the
    same shapes on the read side; rejecting here keeps the writer
    consistent.
    """
    if "/" in recipient or ".." in recipient:
        return {
            "recipient": recipient,
            "status": "error",
            "error": "recipient name must not contain '/' or '..'",
        }
    target = USERS_DIR / f"{recipient}.brief.md"
    if target.exists():
        return {"recipient": recipient, "status": "exists", "path": str(target)}
    if not TEMPLATE.exists():
        return {"recipient": recipient, "status": "error",
                "error": f"template missing at {TEMPLATE}"}
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATE, target)
    return {"recipient": recipient, "status": "created", "path": str(target)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap per-recipient morning brief prompts from the default template.")
    parser.add_argument("--recipient",
                        help="Bootstrap a single recipient by name (default: all from HEARTBEAT.md)")
    args = parser.parse_args()

    if args.recipient:
        recipients = [args.recipient]
    else:
        if not HEARTBEAT_FILE.exists():
            print(json.dumps({"error": f"HEARTBEAT.md not found at {HEARTBEAT_FILE}"}))
            sys.exit(1)
        recipients = parse_brief_recipients(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        if not recipients:
            print(json.dumps({
                "error": "No Morning briefing recipients found in HEARTBEAT.md "
                         "(check that the Morning briefing block has a Recipients: field).",
            }))
            sys.exit(1)

    results = [bootstrap_recipient(r) for r in recipients]
    summary = {
        "total": len(results),
        "created": sum(1 for r in results if r["status"] == "created"),
        "exists": sum(1 for r in results if r["status"] == "exists"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }
    print(json.dumps(summary, indent=2))

    if summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

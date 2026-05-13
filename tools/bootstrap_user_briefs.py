#!/usr/bin/env python3
"""
bootstrap_user_briefs.py — Ensure every household member has a brief.md.

When PR-C ships the morning-brief skill, the heartbeat fans out per
recipient and reads `users/<recipient>.brief.md` (workspace-relative)
as that recipient's prompt. From the homer repo root that resolves to
`context/.nanobot_workspace/users/<name>.brief.md`. New users get their
file at add-time (the skill covers that), but existing tenants need a
one-shot backfill — otherwise day-1 after deploy, nanobot's safe-degrade
fires and existing users get a generic brief until someone runs the cp.

This tool walks `manage_users.list_users()` and copies
`skills/morning-brief/default.brief.md` →
`context/.nanobot_workspace/users/<name>.brief.md` for any user whose
file is missing. **Idempotent**: existing files are left alone (a user
may have edited theirs).

Usage:
    python tools/bootstrap_user_briefs.py             # all users
    python tools/bootstrap_user_briefs.py --user ebby # one user
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manage_users import list_users

REPO_ROOT = Path(__file__).parent.parent.resolve()
TEMPLATE = REPO_ROOT / "skills" / "morning-brief" / "default.brief.md"
# Workspace-relative so nanobot's `Prompt-file: users/{recipient}.brief.md`
# resolves correctly (nanobot anchors at `<homer>/context/.nanobot_workspace`).
USERS_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "users"


def bootstrap_user(name: str) -> dict:
    """Create context/users/<name>.brief.md from default.brief.md if missing.

    Returns one of:
      {"user": name, "status": "created", "path": "..."}
      {"user": name, "status": "exists",  "path": "..."}  # left alone
      {"user": name, "status": "error",   "error": "..."}
    """
    target = USERS_DIR / f"{name}.brief.md"
    if target.exists():
        return {"user": name, "status": "exists", "path": str(target)}
    if not TEMPLATE.exists():
        return {"user": name, "status": "error",
                "error": f"template missing at {TEMPLATE}"}
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATE, target)
    return {"user": name, "status": "created", "path": str(target)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap per-user morning brief prompts from the default template.")
    parser.add_argument("--user", help="Bootstrap a single user by name (default: all)")
    args = parser.parse_args()

    if args.user:
        results = [bootstrap_user(args.user)]
    else:
        results = [bootstrap_user(u["name"])
                   for u in list_users() if u.get("name")]

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

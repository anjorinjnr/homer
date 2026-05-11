#!/usr/bin/env python3
"""scope_leakage_check.py — guard against scope data appearing in guest USER.md.

Since the per-sender injection hook landed, the guest workspace's USER.md is a
stub: scope envelopes are rendered per-turn by nanobot at message arrival and
never persisted to the workspace. If scope sections reappear in USER.md, the
stub path has regressed — every guest inbound would see every scope again.

Usage:
  python tools/scope_leakage_check.py [--workspace PATH] [--json]

Exit codes:
  0 — stub intact (no scope data found)
  1 — leakage detected (scope sections found in USER.md)
  2 — USER.md missing

Designed to be cron-able on the VPS and cheap to call from a status endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_GUEST_WORKSPACE = REPO_ROOT / "context" / ".guest_workspace"

# Markers that should NEVER appear in guest USER.md once the stub is in effect.
# Ordered most-specific first to give informative error messages.
LEAKAGE_MARKERS = (
    "## Scope:",
    "### Context",
    "### Conversation History",
    "### Pending Follow-ups",
    "Disclosure rules",
    "## Active Scopes",
)


def check(user_md_path: Path) -> tuple[int, dict]:
    """Return (exit_code, details)."""
    if not user_md_path.exists():
        return 2, {
            "status": "missing",
            "path": str(user_md_path),
        }

    content = user_md_path.read_text(encoding="utf-8")
    found = [m for m in LEAKAGE_MARKERS if m in content]
    if found:
        return 1, {
            "status": "leakage_detected",
            "path": str(user_md_path),
            "markers_found": found,
            "size_chars": len(content),
        }
    return 0, {
        "status": "ok",
        "path": str(user_md_path),
        "size_chars": len(content),
    }


def _resolve_workspace(override: str | None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("HOMER_GUEST_WORKSPACE")
    return Path(env) if env else DEFAULT_GUEST_WORKSPACE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Guest workspace path override")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    user_md = _resolve_workspace(args.workspace) / "USER.md"
    code, details = check(user_md)

    if args.json:
        print(json.dumps(details))
    else:
        if code == 0:
            print(f"✓ {user_md}: {details['size_chars']} chars, no scope leakage")
        elif code == 1:
            print(f"✗ {user_md}: SCOPE LEAKAGE DETECTED")
            print(f"  markers found: {', '.join(details['markers_found'])}")
        else:
            print(f"? {user_md}: not found")
    return code


if __name__ == "__main__":
    sys.exit(main())

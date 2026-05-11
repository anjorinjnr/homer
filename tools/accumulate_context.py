#!/usr/bin/env python3
"""
accumulate_context.py — Persist key facts learned during guest interactions.

Called by the guest agent after meaningful interactions to save context
fragments that should survive across sessions (e.g., a guest confirming
availability, providing preferences, or making decisions).

Usage (exec tool):
  python tools/accumulate_context.py --scope-id denver_mtb \
    --guest "Ugo" --content "Prefers drivable trips over flights"

Each call appends an attributed fragment to context_layers.accumulated
in the scope envelope, then rebuilds the guest USER.md so subsequent
interactions see the accumulated context.
"""

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
HOMER_TOOLS = str(REPO_ROOT / "tools")
HOMER_VENV = str(REPO_ROOT / ".venv" / "bin" / "python")

if HOMER_TOOLS not in sys.path:
    sys.path.insert(0, HOMER_TOOLS)
import guest_scope_guard


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def accumulate(
    scope_id: str,
    content: str,
    guest: str = "",
    source_interaction: str = "",
    db_path: Path | None = None,
    rebuild: bool = True,
) -> dict:
    """Append an accumulated context fragment to a scope envelope.

    Returns a result dict with fragment_id and status.
    """
    sys.path.insert(0, HOMER_TOOLS)
    import scope_store

    envelope = scope_store.get_scope(scope_id, db_path)
    if envelope is None:
        return {"ok": False, "error": f"scope {scope_id} not found"}

    status = envelope.get("_status", "active")
    if status != "active":
        return {"ok": False, "error": f"scope {scope_id} is {status}, not active"}

    # Guard against unbounded growth
    MAX_FRAGMENT_LENGTH = 2000
    MAX_FRAGMENTS_PER_SCOPE = 50

    if len(content) > MAX_FRAGMENT_LENGTH:
        content = content[:MAX_FRAGMENT_LENGTH]

    existing = envelope.get("context_layers", {}).get("accumulated", [])
    if len(existing) >= MAX_FRAGMENTS_PER_SCOPE:
        return {"ok": False, "error": f"scope {scope_id} has reached the {MAX_FRAGMENTS_PER_SCOPE} fragment limit"}

    fragment_id = f"acc_{uuid.uuid4().hex[:12]}"
    fragment = {
        "fragment_id": fragment_id,
        "guest": guest,
        "content": content,
        "source_interaction_id": source_interaction,
        "timestamp": _now_utc(),
        "prunable": True,
    }

    envelope.setdefault("context_layers", {}).setdefault("accumulated", [])
    envelope["context_layers"]["accumulated"].append(fragment)

    scope_store.update_scope(scope_id, envelope, db_path)

    # Rebuild guest USER.md so the new context is visible in future interactions.
    # Skip rebuild in simulation (HOMER_SIM=1) — the harness manages rebuilds.
    if rebuild and not os.environ.get("HOMER_SIM"):
        try:
            subprocess.run(
                [HOMER_VENV, str(REPO_ROOT / "tools" / "build_context.py")],
                cwd=str(REPO_ROOT),
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass  # best-effort rebuild; context is already persisted in DB

    return {
        "ok": True,
        "fragment_id": fragment_id,
        "scope_id": scope_id,
        "accumulated_count": len(envelope["context_layers"]["accumulated"]),
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Persist accumulated context for a guest scope."
    )
    parser.add_argument("--scope-id", required=True, help="Scope ID to accumulate context for")
    parser.add_argument("--content", required=True, help="Context fragment to persist")
    parser.add_argument("--guest", default="", help="Guest name for attribution")
    parser.add_argument("--source-interaction", default="", help="Source interaction/message ID")
    parser.add_argument("--db", metavar="DB_PATH", help="Override DB path")
    args = parser.parse_args()

    guest_scope_guard.assert_scope_allowed(args.scope_id)
    db_path = Path(args.db) if args.db else None
    result = accumulate(
        scope_id=args.scope_id,
        content=args.content,
        guest=args.guest,
        source_interaction=args.source_interaction,
        db_path=db_path,
    )
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    _cli()

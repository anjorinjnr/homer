#!/usr/bin/env python3
"""resolve_escalation.py — Main agent tool for resolving pending escalations.

Called by the main agent to resolve an escalation created by the guest agent.
Supports multiple resolution actions: injecting context, granting capabilities,
drafting a response, modifying scope, or terminating scope.

Usage:
  python tools/resolve_escalation.py --escalation-id <id> \
    --action response_drafted --drafted-response "The Airbnb costs $450/night..."

  python tools/resolve_escalation.py --escalation-id <id> \
    --action context_injected --context "Budget: $2000 total, Airbnb $450/night"
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "tools"))

import scope_store

VALID_ACTIONS = {
    "context_injected",
    "response_drafted",
    "scope_terminated",
}


def resolve_escalation(
    *,
    escalation_id: str,
    action: str,
    drafted_response: str = "",
    context: str = "",
    db_path: Path | None = None,
    skip_rebuild: bool = False,
) -> dict:
    """Resolve a pending escalation.

    Returns dict with resolution details.
    Raises ValueError for invalid inputs or states.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"Invalid action '{action}'. "
            f"Must be one of: {', '.join(sorted(VALID_ACTIONS))}"
        )

    if action == "response_drafted" and not drafted_response.strip():
        raise ValueError(
            "Action 'response_drafted' requires a non-empty --drafted-response."
        )
    if action == "context_injected" and not context.strip():
        raise ValueError(
            "Action 'context_injected' requires a non-empty --context."
        )
    if action == "scope_terminated" and not drafted_response.strip():
        raise ValueError(
            "Action 'scope_terminated' requires a non-empty --drafted-response "
            "(farewell message for the guest before scope is closed)."
        )

    now = scope_store._now_utc()

    # Fetch the escalation
    with scope_store.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM escalations WHERE escalation_id = ?",
            (escalation_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"Escalation '{escalation_id}' not found.")
    if row["status"] != "pending":
        raise ValueError(
            f"Escalation '{escalation_id}' is already '{row['status']}', cannot resolve."
        )

    scope_id = row["scope_id"]

    # Build resolution JSON
    resolution = {
        "action_taken": action,
        "resolved_at": now,
    }
    if drafted_response:
        resolution["drafted_response"] = drafted_response
    if context:
        resolution["context_fragments_added"] = [context]

    # Update escalation in DB
    with scope_store.get_conn(db_path) as conn:
        conn.execute(
            """UPDATE escalations
               SET status = 'resolved', resolution = ?, resolved_at = ?
               WHERE escalation_id = ?""",
            (json.dumps(resolution), now, escalation_id),
        )
        conn.commit()

    # Update scope envelope based on action
    envelope = scope_store.get_scope(scope_id, db_path)
    if envelope:
        envelope.pop("_status", None)

        if action == "context_injected" and context:
            fragment = {
                "fragment_id": f"frag_{uuid.uuid4().hex[:8]}",
                "content": context,
                "sensitivity": "medium",
                "ttl": None,
                "injected_by_escalation": escalation_id,
            }
            envelope.setdefault("context_layers", {}).setdefault("injected", []).append(fragment)

        # NOTE: scope_terminated does NOT terminate immediately. The scope stays
        # active so the guest agent can deliver the farewell message. Actual
        # termination happens in deliver_escalation.py after outbound_sent=1.

        # Update escalation_log entry
        for entry in envelope.get("escalation_log", []):
            if entry.get("escalation_id") == escalation_id:
                entry["status"] = "resolved"
                entry["resolution"] = resolution
                break

        scope_store.update_scope(scope_id, envelope, db_path)

    # Rebuild guest workspace so it picks up context changes
    if not skip_rebuild:
        _rebuild_guest_workspace()

    return {
        "ok": True,
        "escalation_id": escalation_id,
        "scope_id": scope_id,
        "action": action,
        "status": "resolved",
        "resolved_at": now,
        "resolution": resolution,
    }


def _rebuild_guest_workspace() -> None:
    """Call build_context.py to rebuild the guest workspace."""
    homer_home = os.environ.get("HOMER_HOME") or str(REPO_ROOT)
    venv_python = f"{homer_home}/.venv/bin/python"
    build_script = f"{homer_home}/tools/build_context.py"

    # Use system python if venv doesn't exist (e.g., in tests)
    python_cmd = venv_python if Path(venv_python).exists() else sys.executable
    try:
        subprocess.run(
            [python_cmd, build_script],
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        print(f"[warn] guest workspace rebuild failed: {e}", file=sys.stderr)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Resolve a pending escalation.")
    parser.add_argument("--escalation-id", required=True, help="Escalation ID to resolve")
    parser.add_argument(
        "--action", required=True,
        choices=sorted(VALID_ACTIONS),
        help="Resolution action",
    )
    parser.add_argument("--drafted-response", default="",
                        help="Response text for response_drafted action")
    parser.add_argument("--context", default="",
                        help="Context text for context_injected action")
    parser.add_argument("--db", metavar="DB_PATH", help="Override DB path")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    try:
        result = resolve_escalation(
            escalation_id=args.escalation_id,
            action=args.action,
            drafted_response=args.drafted_response,
            context=args.context,
            db_path=db_path,
        )
        print(json.dumps(result, indent=2))
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()

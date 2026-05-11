#!/usr/bin/env python3
"""deliver_escalation.py — Guest agent tool for delivering resolved escalation responses.

Called by the guest agent to retrieve and deliver a resolved escalation's response
to the guest. Marks the escalation as delivered (outbound_sent=1).

Usage:
  python tools/deliver_escalation.py --escalation-id <id>
  python tools/deliver_escalation.py --list-pending --scope-id <scope_id>
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "tools"))

import scope_store
import guest_scope_guard


def _read_active_scope_ids() -> list[str]:
    return guest_scope_guard.sender_scope_ids()


def deliver_escalation(
    *,
    escalation_id: str,
    db_path: Path | None = None,
    active_scope_ids: list[str] | None = None,
) -> dict:
    """Deliver a resolved escalation's response.

    Args:
        escalation_id: The escalation to deliver.
        db_path: Override DB path.
        active_scope_ids: If provided, verify the escalation belongs to one
            of these scopes before delivering (prevents cross-scope leaks).

    Returns dict with the drafted_response text (or context info).
    Raises ValueError for invalid states.
    """
    with scope_store.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM escalations WHERE escalation_id = ?",
            (escalation_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"Escalation '{escalation_id}' not found.")

    # Cross-scope validation: ensure escalation belongs to an active scope
    if active_scope_ids is not None and row["scope_id"] not in active_scope_ids:
        raise ValueError(
            f"Escalation '{escalation_id}' does not belong to any active scope."
        )

    if row["status"] != "resolved":
        raise ValueError(
            f"Escalation '{escalation_id}' is '{row['status']}', not resolved. "
            "Cannot deliver an unresolved escalation."
        )
    if row["outbound_sent"]:
        raise ValueError(
            f"Escalation '{escalation_id}' has already been delivered."
        )

    resolution = json.loads(row["resolution"]) if row["resolution"] else {}
    now = scope_store._now_utc()

    # Mark as delivered
    with scope_store.get_conn(db_path) as conn:
        conn.execute(
            """UPDATE escalations
               SET outbound_sent = 1, outbound_sent_at = ?
               WHERE escalation_id = ?""",
            (now, escalation_id),
        )
        conn.commit()

    result = {
        "ok": True,
        "escalation_id": escalation_id,
        "scope_id": row["scope_id"],
        "action_taken": resolution.get("action_taken", ""),
        "delivered_at": now,
    }

    # Always include the original question and participant for routing
    if row["triggering_message"]:
        result["original_message"] = row["triggering_message"]
    # Include participant info so heartbeat knows who to message
    envelope = scope_store.get_scope(row["scope_id"], db_path)
    if envelope:
        participants = envelope.get("participants", [])
        if participants:
            party_id = participants[0].get("party_id", "")
            # Strip @s.whatsapp.net — nanobot message tool uses short form
            result["participant"] = party_id.split("@")[0] if "@" in party_id else party_id

    # Include drafted_response if present
    if resolution.get("drafted_response"):
        result["drafted_response"] = resolution["drafted_response"]

    # Include injected context info if present
    if resolution.get("context_fragments_added"):
        result["context_injected"] = True

    # Deferred termination: if the resolution was scope_terminated, terminate
    # now that the farewell message has been delivered, then rebuild so
    # USER.md and active_scopes.json no longer reference the dead scope
    if resolution.get("action_taken") == "scope_terminated":
        scope_store.terminate_scope(row["scope_id"], db_path)
        result["scope_terminated"] = True
        try:
            homer_home = os.environ.get("HOMER_HOME") or str(REPO_ROOT)
            venv_py = f"{homer_home}/.venv/bin/python"
            python_cmd = venv_py if Path(venv_py).exists() else sys.executable
            subprocess.run([python_cmd, f"{homer_home}/tools/build_context.py"],
                           capture_output=True, timeout=30)
        except Exception as e:
            print(f"[warn] guest workspace rebuild failed: {e}", file=sys.stderr)

    return result


def list_pending_for_scope(
    *,
    scope_id: str,
    db_path: Path | None = None,
) -> list[dict]:
    """List resolved but undelivered escalations for a specific scope.

    This is the safe entry point for the guest agent — it only returns
    escalations belonging to the given scope, preventing cross-scope leaks.
    """
    return scope_store.get_resolved_undelivered_escalations(
        db_path=db_path,
        scope_id=scope_id,
    )


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Deliver a resolved escalation response.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--escalation-id", help="Escalation ID to deliver")
    group.add_argument("--list-pending", action="store_true",
                       help="List undelivered escalations for active scopes")
    parser.add_argument("--db", metavar="DB_PATH", help="Override DB path")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    active_ids = _read_active_scope_ids()

    if args.list_pending:
        # No active scopes = no pending escalations (not an error)
        if not active_ids:
            print(json.dumps([]))
            return
        all_results: list[dict] = []
        for sid in active_ids:
            all_results.extend(
                list_pending_for_scope(scope_id=sid, db_path=db_path)
            )
        print(json.dumps(all_results, indent=2))
        return

    try:
        result = deliver_escalation(
            escalation_id=args.escalation_id,
            db_path=db_path,
            active_scope_ids=active_ids,
        )
        print(json.dumps(result, indent=2))
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()

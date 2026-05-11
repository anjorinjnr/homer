#!/usr/bin/env python3
"""escalate.py — Guest agent tool for creating escalations.

Called by the guest agent via exec when it encounters something outside its scope.
Inserts a pending escalation into the SQLite escalations table and appends
to the scope envelope's escalation_log.

Usage:
  python tools/escalate.py --scope-id rel_123_denver_mtb --trigger-type context_missing \
    --message "Guest asked about lodging cost but I don't have budget details" \
    --assessment "Guest needs specific dollar amount from budget spreadsheet"
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "tools"))

import scope_store
import guest_scope_guard


def _read_active_scope_ids() -> list[str]:
    return guest_scope_guard.sender_scope_ids()


def _resolve_scope_id(cli_scope_id: str | None) -> str:
    """Resolve the scope ID from active_scopes.json, validating any CLI override.

    - If exactly one active scope, use it (ignore cli_scope_id).
    - If multiple active scopes and cli_scope_id is provided and in the list, use it.
    - Otherwise, error.
    """
    active = _read_active_scope_ids()
    if not active:
        print(
            json.dumps({"ok": False, "error": "No active scopes found in active_scopes.json."}),
            file=sys.stderr,
        )
        sys.exit(1)

    if len(active) == 1:
        return active[0]

    # Multiple scopes — require explicit --scope-id that's in the active list
    if not cli_scope_id:
        print(
            json.dumps({
                "ok": False,
                "error": f"Multiple active scopes ({', '.join(active)}). Provide --scope-id.",
            }),
            file=sys.stderr,
        )
        sys.exit(1)

    if cli_scope_id not in active:
        print(
            json.dumps({
                "ok": False,
                "error": f"Scope '{cli_scope_id}' is not in the active scope list.",
            }),
            file=sys.stderr,
        )
        sys.exit(1)

    return cli_scope_id

VALID_TRIGGER_TYPES = {
    "capability_exceeded",
    "context_missing",
    "disclosure_risk",
    "authorization_expired",
    "uncertainty",
    "domain_drift",
    "guest_update",
}

VALID_URGENCY = {"async", "real_time"}


def create_escalation(
    *,
    scope_id: str,
    trigger_type: str,
    message: str,
    assessment: str = "",
    urgency: str = "async",
    db_path: Path | None = None,
) -> dict:
    """Create a new escalation for a scope.

    Returns dict with escalation_id and status.
    Raises ValueError for invalid inputs.
    """
    if trigger_type not in VALID_TRIGGER_TYPES:
        raise ValueError(
            f"Invalid trigger_type '{trigger_type}'. "
            f"Must be one of: {', '.join(sorted(VALID_TRIGGER_TYPES))}"
        )
    if urgency not in VALID_URGENCY:
        raise ValueError(f"Invalid urgency '{urgency}'. Must be 'async' or 'real_time'.")

    # Verify scope exists
    envelope = scope_store.get_scope(scope_id, db_path)
    if envelope is None:
        raise ValueError(f"Scope '{scope_id}' not found.")

    escalation_id = str(uuid.uuid4())
    now = scope_store._now_utc()

    # Insert into escalations table
    with scope_store.get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO escalations
               (escalation_id, scope_id, trigger_type, triggering_message,
                guest_assessment, urgency, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (escalation_id, scope_id, trigger_type, message, assessment, urgency, now),
        )
        conn.commit()

    # Append to scope envelope's escalation_log
    envelope.pop("_status", None)
    log_entry = {
        "escalation_id": escalation_id,
        "trigger_type": trigger_type,
        "message": message,
        "assessment": assessment,
        "urgency": urgency,
        "created_at": now,
        "status": "pending",
    }
    envelope.setdefault("escalation_log", []).append(log_entry)
    scope_store.update_scope(scope_id, envelope, db_path)

    return {
        "ok": True,
        "escalation_id": escalation_id,
        "scope_id": scope_id,
        "trigger_type": trigger_type,
        "urgency": urgency,
        "status": "pending",
        "created_at": now,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Create an escalation from the guest agent.")
    parser.add_argument("--scope-id", default=None,
                        help="Scope ID (auto-detected from active_scopes.json; "
                             "only needed when multiple scopes are active)")
    parser.add_argument(
        "--trigger-type", required=True,
        choices=sorted(VALID_TRIGGER_TYPES),
        help="Escalation trigger type",
    )
    parser.add_argument("--message", required=True, help="Triggering message / question")
    parser.add_argument("--assessment", default="", help="Guest agent's assessment of what's needed")
    parser.add_argument("--urgency", default="async", choices=sorted(VALID_URGENCY),
                        help="Urgency level (default: async)")
    parser.add_argument("--db", metavar="DB_PATH", help="Override DB path")
    args = parser.parse_args()

    scope_id = _resolve_scope_id(args.scope_id)
    db_path = Path(args.db) if args.db else None
    try:
        result = create_escalation(
            scope_id=scope_id,
            trigger_type=args.trigger_type,
            message=args.message,
            assessment=args.assessment,
            urgency=args.urgency,
            db_path=db_path,
        )
        print(json.dumps(result, indent=2))
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()

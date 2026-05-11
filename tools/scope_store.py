#!/usr/bin/env python3
"""scope_store.py — SQLite-backed scope store for Homer guest agent management.

Backend module and human-facing CLI. Not intended as a Homer exec tool —
Homer interacts with scopes indirectly via manage_event_guest.py.

Replaces/augments guest_agent_acl.json with a structured database that
supports the full ScopeEnvelope design (Phase 1: minimal envelopes).

Schema:
  scopes(scope_id, envelope JSON, status, last_active, created_at)
  scope_participants(participant_id, scope_id)
  escalations(...)  — stub table for Phase 2

CLI usage (human / ops):
  python tools/scope_store.py --list
  python tools/scope_store.py --get-participants
  python tools/scope_store.py --migrate-acl [acl_path]
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = REPO_ROOT / "context" / "scopes.db"
DEFAULT_PENDING_REPLIES_PATH = REPO_ROOT / "context" / "pending_replies.json"

# Scope type constants
SCOPE_TYPE_INTERACTION = "interaction"
SCOPE_TYPE_RELATIONSHIP = "relationship"

# Disclosure rules by authorization tier (rendered into each scope section)
DISCLOSURE_RULES = {
    "identity_only": (
        "**Disclosure rules**: Only confirm that you are Homer and that a task exists. "
        "Do not share any task details, plans, or context. "
        "Do not speculate about household finances, health, or personal matters."
    ),
    "task_context": (
        "**Disclosure rules**: You may share details about this task from your injected "
        "and accumulated context, but nothing beyond it. "
        "Do not speculate about household finances, health, or personal matters."
    ),
    "broad_context": (
        "**Disclosure rules**: You may share broader context relevant to this relationship. "
        "Use good judgment — avoid sharing sensitive financial or health details "
        "unless they are explicitly in your context."
    ),
}


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return the scope DB path. Override with HOMER_SCOPE_DB env var."""
    env = os.environ.get("HOMER_SCOPE_DB")
    return Path(env) if env else DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

# Set of DB paths (resolved) that have already had _create_tables() run in this
# process. Scope DBs are long-lived and tables never dropped, so running schema
# creation per-connection wasted SQLite parse/lock cycles on every turn of the
# per-sender injection hot path. Concurrent add() from two async tasks is safe
# under CPython's GIL; the worst case is two concurrent CREATE IF NOT EXISTS
# executions on first use — idempotent.
_SCHEMA_INITIALISED: set[str] = set()


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the scope DB and ensure tables exist."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    key = str(path.resolve())
    if key not in _SCHEMA_INITIALISED:
        _create_tables(conn)
        _SCHEMA_INITIALISED.add(key)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scopes (
            scope_id    TEXT PRIMARY KEY,
            envelope    TEXT NOT NULL,          -- JSON ScopeEnvelope
            status      TEXT NOT NULL DEFAULT 'active',  -- active | dormant | terminated
            last_active TEXT,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS scope_participants (
            participant_id  TEXT NOT NULL,      -- WhatsApp JID or tg:<id>
            scope_id        TEXT NOT NULL,
            PRIMARY KEY (participant_id, scope_id),
            FOREIGN KEY (scope_id) REFERENCES scopes(scope_id)
        );

        -- Email-to-scope index for inbound email routing
        CREATE TABLE IF NOT EXISTS scope_email_index (
            email       TEXT NOT NULL,
            scope_id    TEXT NOT NULL,
            PRIMARY KEY (email, scope_id),
            FOREIGN KEY (scope_id) REFERENCES scopes(scope_id)
        );

        -- Escalation events (Phase 2 IPC bus — table created now, used in Phase 2)
        CREATE TABLE IF NOT EXISTS escalations (
            escalation_id       TEXT PRIMARY KEY,
            scope_id            TEXT NOT NULL,
            trigger_type        TEXT NOT NULL,
            triggering_message  TEXT,
            guest_assessment    TEXT,
            urgency             TEXT NOT NULL DEFAULT 'async',  -- real_time | async
            status              TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved
            resolution          TEXT,           -- JSON
            outbound_sent       INTEGER NOT NULL DEFAULT 0,
            outbound_sent_at    TEXT,
            surfaced_at         TEXT,           -- when the main agent notified the user
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            resolved_at         TEXT,
            FOREIGN KEY (scope_id) REFERENCES scopes(scope_id)
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Scope envelope helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_email(email: str) -> str:
    """Normalize an email address for consistent lookup.

    Lowercases, and for Gmail addresses strips dots and plus-suffixes
    from the local part (j.doe+tag@gmail.com → jdoe@gmail.com).
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    gmail_domains = {"gmail.com", "googlemail.com"}
    if domain in gmail_domains:
        local = local.split("+")[0].replace(".", "")
    return f"{local}@{domain}"


def make_minimal_envelope(
    *,
    scope_id: str,
    name: str,
    participant_id: str,
    event_id: str,
    relationship_type: str = "personal",
    channel: str = "whatsapp",
    email: Optional[str] = None,
    expires: Optional[str] = None,
    principal: str = "",
    context_source: Optional[dict] = None,
) -> dict:
    """Build a minimal Phase 1 scope envelope from basic guest data.

    context_source: optional dict like {"type": "event", "ref": "denver_mtb"}
                    that opts the scope into dynamic context refresh via context_inject.py.
    email: optional email address for the participant (enables inbound email routing).
    """
    participant = {
        "party_id": participant_id,
        "name": name,
        "handle": participant_id,
        "relationship_type": relationship_type,
        "channel": channel,
    }
    if email:
        participant["email"] = email.strip().lower()
    envelope = {
        "scope_id": scope_id,
        "scope_type": "relationship",
        "principal": principal,
        "guest_identity": "I am Homer",
        "creation": {
            "trigger": "user_initiated",
            "parent_scope_id": None,
            "parent_task_id": None,
            "created_at": _now_utc(),
        },
        "participants": [participant],
        "authorization": {
            "granted_capabilities": ["message"],
            "max_disclosure_tier": "task_context",
            "escalation_triggers": [],
            "expires_at": expires or None,
        },
        "context_layers": {
            "injected": [],
            "accumulated": [],
        },
        "task_tags": [
            {
                "task_id": f"task_{event_id}",
                "description": event_id.replace("_", " ").title(),
                "status": "active",
                "context_fragment_ids": [],
            }
        ],
        "lifecycle": {
            "last_active": None,
            "pruning_policy": "retain_all",
            "review_trigger": "30d",
        },
        "escalation_log": [],
    }
    if context_source:
        envelope["context_source"] = context_source
    return envelope


SCOPE_MODE_TWO_WAY = "two_way"
SCOPE_MODE_NO_REPLY = "no_reply"
_VALID_SCOPE_MODES = (SCOPE_MODE_TWO_WAY, SCOPE_MODE_NO_REPLY)


def make_interaction_envelope(
    *,
    scope_id: str,
    name: str,
    participant_id: str,
    channel: str = "whatsapp",
    email: Optional[str] = None,
    purpose: str = "",
    expires: Optional[str] = None,
    principal: str = "",
    mode: str = SCOPE_MODE_TWO_WAY,
) -> dict:
    """Build a scope envelope for an ad-hoc external interaction.

    Unlike make_minimal_envelope (event-centric), this creates a lightweight
    scope for any channel — WhatsApp, Telegram, or email — with no event
    dependency.  Interaction scopes default to 30-day expiry.

    participant_id: JID (whatsapp), tg:<id> (telegram), or email address (email).
    email: optional email for the participant (even for WA/TG contacts).
    purpose: brief description of why the interaction exists (injected as context).
    mode: 'two_way' (default — replies route to guest agent) or 'no_reply'
          (outbound allowed but inbound from this participant is suppressed).
    """
    if mode not in _VALID_SCOPE_MODES:
        raise ValueError(
            f"mode must be one of {_VALID_SCOPE_MODES}, got {mode!r}"
        )
    participant = {
        "party_id": participant_id,
        "name": name,
        "handle": participant_id,
        "relationship_type": "service",
        "channel": channel,
    }
    if email:
        participant["email"] = email.strip().lower()
    # For email-channel contacts, always store email even if not passed separately
    if channel == "email" and "email" not in participant:
        participant["email"] = participant_id.strip().lower()

    injected = []
    if purpose:
        injected.append({
            "fragment_id": f"init_{scope_id}",
            "content": purpose,
        })

    description = purpose or scope_id.replace("_", " ").removeprefix("int ").title()

    return {
        "scope_id": scope_id,
        "scope_type": SCOPE_TYPE_INTERACTION,
        "principal": principal,
        "guest_identity": "I am Homer",
        "mode": mode,
        "creation": {
            "trigger": "user_initiated",
            "parent_scope_id": None,
            "parent_task_id": None,
            "created_at": _now_utc(),
        },
        "participants": [participant],
        "authorization": {
            "granted_capabilities": ["message"],
            "max_disclosure_tier": "task_context",
            "escalation_triggers": [],
            "expires_at": expires or None,
        },
        "context_layers": {
            "injected": injected,
            "accumulated": [],
        },
        "task_tags": [
            {
                "task_id": f"task_{scope_id}",
                "description": description,
                "status": "active",
                "context_fragment_ids": [],
            }
        ],
        "lifecycle": {
            "last_active": None,
            "pruning_policy": "retain_all",
            "review_trigger": "30d",
        },
        "escalation_log": [],
    }


def set_scope_mode(scope_id: str, mode: str, db_path: Optional[Path] = None) -> None:
    """Update an existing scope's mode (two_way <-> no_reply).

    Used by manage_interaction --convert-to-two-way to upgrade a no-reply
    scope after the fact (e.g., a vendor we initially nudged actually
    needs to come back with a question).
    """
    if mode not in _VALID_SCOPE_MODES:
        raise ValueError(
            f"mode must be one of {_VALID_SCOPE_MODES}, got {mode!r}"
        )
    env = get_scope(scope_id, db_path=db_path)
    if env is None:
        raise ValueError(f"Scope '{scope_id}' not found")
    env["mode"] = mode
    update_scope(scope_id, env, db_path=db_path)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _sync_email_index(conn: sqlite3.Connection, scope_id: str, participants: list[dict]) -> None:
    """Sync scope_email_index for a scope's participants."""
    new_emails = set()
    for p in participants:
        email = p.get("email", "")
        if email:
            new_emails.add(normalize_email(email))

    # Get existing emails in the index
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT email FROM scope_email_index WHERE scope_id = ?", (scope_id,)
        )
    }

    for email in new_emails - existing:
        conn.execute(
            "INSERT OR IGNORE INTO scope_email_index (email, scope_id) VALUES (?, ?)",
            (email, scope_id),
        )
    for email in existing - new_emails:
        conn.execute(
            "DELETE FROM scope_email_index WHERE email = ? AND scope_id = ?",
            (email, scope_id),
        )


def create_scope(envelope: dict, db_path: Optional[Path] = None) -> str:
    """Insert a new scope. Returns the scope_id. Raises if scope_id already exists."""
    scope_id = envelope["scope_id"]
    participants = envelope.get("participants", [])
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO scopes (scope_id, envelope, status) VALUES (?, ?, 'active')",
            (scope_id, json.dumps(envelope)),
        )
        for p in participants:
            conn.execute(
                "INSERT OR IGNORE INTO scope_participants (participant_id, scope_id) VALUES (?, ?)",
                (p["party_id"], scope_id),
            )
        _sync_email_index(conn, scope_id, participants)
        conn.commit()
    return scope_id


def get_scope(scope_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the scope envelope dict, or None if not found."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT envelope, status FROM scopes WHERE scope_id = ?", (scope_id,)
        ).fetchone()
    if not row:
        return None
    env = json.loads(row["envelope"])
    env["_status"] = row["status"]
    return env


def update_scope(scope_id: str, envelope: dict, db_path: Optional[Path] = None) -> None:
    """Overwrite the envelope for an existing scope and update last_active."""
    participants = envelope.get("participants", [])
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE scopes SET envelope = ?, last_active = ? WHERE scope_id = ?",
            (json.dumps(envelope), _now_utc(), scope_id),
        )
        # Sync scope_participants
        existing = {
            row["participant_id"]
            for row in conn.execute(
                "SELECT participant_id FROM scope_participants WHERE scope_id = ?",
                (scope_id,),
            )
        }
        new_ids = {p["party_id"] for p in participants}
        for pid in new_ids - existing:
            conn.execute(
                "INSERT OR IGNORE INTO scope_participants (participant_id, scope_id) VALUES (?, ?)",
                (pid, scope_id),
            )
        for pid in existing - new_ids:
            conn.execute(
                "DELETE FROM scope_participants WHERE participant_id = ? AND scope_id = ?",
                (pid, scope_id),
            )
        # Sync email index
        _sync_email_index(conn, scope_id, participants)
        conn.commit()


def terminate_scope(scope_id: str, db_path: Optional[Path] = None) -> None:
    """Mark a scope as terminated (soft delete)."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE scopes SET status = 'terminated', last_active = ? WHERE scope_id = ?",
            (_now_utc(), scope_id),
        )
        conn.commit()


def reactivate_scope(scope_id: str, db_path: Optional[Path] = None) -> None:
    """Reactivate a previously terminated scope (e.g. new guest added to an empty event)."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE scopes SET status = 'active', last_active = ? WHERE scope_id = ?",
            (_now_utc(), scope_id),
        )
        conn.commit()


def list_active_scopes(db_path: Optional[Path] = None) -> list[dict]:
    """Return all active scope envelopes."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT envelope FROM scopes WHERE status = 'active' ORDER BY created_at",
        ).fetchall()
    return [json.loads(r["envelope"]) for r in rows]


def get_scopes_for_participant(
    participant_id: str, db_path: Optional[Path] = None
) -> list[dict]:
    """Return all active scope envelopes for a given participant (routing lookup)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT s.envelope FROM scopes s
               JOIN scope_participants sp ON s.scope_id = sp.scope_id
               WHERE sp.participant_id = ? AND s.status = 'active'
               ORDER BY s.created_at""",
            (participant_id,),
        ).fetchall()
    return [json.loads(r["envelope"]) for r in rows]


def get_scopes_for_email(
    email: str, db_path: Optional[Path] = None
) -> list[dict]:
    """Return all active scope envelopes for a given email address (inbound email routing)."""
    normalized = normalize_email(email)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT s.envelope FROM scopes s
               JOIN scope_email_index sei ON s.scope_id = sei.scope_id
               WHERE sei.email = ? AND s.status = 'active'
               ORDER BY s.created_at""",
            (normalized,),
        ).fetchall()
    return [json.loads(r["envelope"]) for r in rows]


def get_all_active_email_addresses(db_path: Optional[Path] = None) -> list[str]:
    """Return all email addresses across active scopes (for email channel allow_from).

    Returns the raw (lowercase, un-normalized) emails from participant dicts,
    since nanobot's email channel sends the raw From: address as sender_id.
    """
    scopes = list_active_scopes(db_path)
    emails = set()
    for env in scopes:
        for p in env.get("participants", []):
            email = p.get("email", "")
            if email:
                emails.add(email)
    return sorted(emails)


def get_all_active_participant_ids(db_path: Optional[Path] = None) -> list[str]:
    """Return all participant IDs across active scopes (for allow_from list)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT DISTINCT sp.participant_id
               FROM scope_participants sp
               JOIN scopes s ON sp.scope_id = s.scope_id
               WHERE s.status = 'active'
               ORDER BY sp.participant_id""",
        ).fetchall()
    return [r["participant_id"] for r in rows]


def add_participant(
    scope_id: str, participant_id: str, db_path: Optional[Path] = None
) -> None:
    """Add a participant to an existing scope."""
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scope_participants (participant_id, scope_id) VALUES (?, ?)",
            (participant_id, scope_id),
        )
        conn.commit()


def remove_participant(
    scope_id: str, participant_id: str, db_path: Optional[Path] = None
) -> None:
    """Remove a participant from a scope."""
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM scope_participants WHERE participant_id = ? AND scope_id = ?",
            (participant_id, scope_id),
        )
        conn.commit()


def find_scope_for_participant_and_event(
    participant_id: str, event_id: str, db_path: Optional[Path] = None
) -> Optional[str]:
    """Find the scope_id for a specific participant + event combination."""
    scopes = get_scopes_for_participant(participant_id, db_path)
    for env in scopes:
        for tag in env.get("task_tags", []):
            if tag.get("task_id") == f"task_{event_id}":
                return env["scope_id"]
    return None


# ---------------------------------------------------------------------------
# Migration from guest_agent_acl.json
# ---------------------------------------------------------------------------

def migrate_from_acl(
    acl_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> int:
    """One-time migration from guest_agent_acl.json to SQLite scope store.

    Creates one scope per ACL entry. Skips entries already in the DB.
    Returns the count of scopes created.
    """
    if acl_path is None:
        acl_path = REPO_ROOT / "context" / "events" / "guest_agent_acl.json"

    if not acl_path.exists():
        print(f"ACL file not found: {acl_path}")
        return 0

    acl: dict = json.loads(acl_path.read_text(encoding="utf-8"))
    if not acl:
        print("ACL file is empty — nothing to migrate.")
        return 0

    created = 0
    for participant_id, info in acl.items():
        event_id = info.get("event_id", "unknown")
        name = info.get("name", "Unknown")
        channel = info.get("channel", "whatsapp")
        expires = info.get("expires") or None
        if expires == "":
            expires = None

        scope_id = f"rel_{participant_id.split('@')[0]}_{event_id}"
        # Skip if already in DB
        if get_scope(scope_id, db_path) is not None:
            print(f"  Skipping {scope_id} (already exists)")
            continue

        relationship_type = "personal"

        envelope = make_minimal_envelope(
            scope_id=scope_id,
            name=name,
            participant_id=participant_id,
            event_id=event_id,
            relationship_type=relationship_type,
            channel=channel,
            expires=expires,
        )
        create_scope(envelope, db_path)
        print(f"  Migrated: {scope_id} ({name}, {participant_id})")
        created += 1

    return created


# ---------------------------------------------------------------------------
# Scope summary (for main agent context injection)
# ---------------------------------------------------------------------------

def get_pending_escalations(db_path: Optional[Path] = None) -> list[dict]:
    """Return all pending escalations with scope context for heartbeat polling."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT e.escalation_id, e.scope_id, e.trigger_type,
                      e.triggering_message, e.guest_assessment, e.urgency,
                      e.created_at
               FROM escalations e
               JOIN scopes s ON e.scope_id = s.scope_id
               WHERE e.status = 'pending' AND s.status = 'active'
                     AND e.surfaced_at IS NULL
               ORDER BY e.created_at""",
        ).fetchall()

    results = []
    for row in rows:
        entry = {
            "escalation_id": row["escalation_id"],
            "scope_id": row["scope_id"],
            "trigger_type": row["trigger_type"],
            "triggering_message": row["triggering_message"],
            "guest_assessment": row["guest_assessment"],
            "urgency": row["urgency"],
            "created_at": row["created_at"],
        }
        # Include scope participant names for context
        envelope = get_scope(row["scope_id"], db_path)
        if envelope:
            entry["participants"] = [
                p["name"] for p in envelope.get("participants", [])
            ]
            tasks = [
                t["description"]
                for t in envelope.get("task_tags", [])
                if t.get("status") == "active"
            ]
            if tasks:
                entry["active_tasks"] = tasks
        results.append(entry)

    return results


def mark_escalation_surfaced(
    escalation_id: str, db_path: Optional[Path] = None
) -> None:
    """Mark an escalation as surfaced to the principal (prevents re-notification)."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE escalations SET surfaced_at = ? WHERE escalation_id = ?",
            (_now_utc(), escalation_id),
        )
        conn.commit()


def get_resolved_undelivered_escalations(
    db_path: Optional[Path] = None,
    scope_id: Optional[str] = None,
) -> list[dict]:
    """Return resolved but undelivered escalations (for guest heartbeat).

    Args:
        db_path: Override DB path.
        scope_id: If provided, filter to only this scope's escalations.
                  IMPORTANT: Always pass scope_id from guest agent context
                  to avoid cross-scope data leaks.
    """
    with get_conn(db_path) as conn:
        if scope_id:
            rows = conn.execute(
                """SELECT e.escalation_id, e.scope_id, e.trigger_type, e.resolution,
                          e.resolved_at
                   FROM escalations e
                   WHERE e.status = 'resolved' AND e.outbound_sent = 0
                         AND e.scope_id = ?
                   ORDER BY e.resolved_at""",
                (scope_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT e.escalation_id, e.scope_id, e.trigger_type, e.resolution,
                          e.resolved_at
                   FROM escalations e
                   WHERE e.status = 'resolved' AND e.outbound_sent = 0
                   ORDER BY e.resolved_at""",
            ).fetchall()

    results = []
    for row in rows:
        resolution = json.loads(row["resolution"]) if row["resolution"] else {}
        entry = {
            "escalation_id": row["escalation_id"],
            "scope_id": row["scope_id"],
            "trigger_type": row["trigger_type"],
            "action_taken": resolution.get("action_taken", ""),
            "resolved_at": row["resolved_at"],
        }
        if resolution.get("drafted_response"):
            entry["has_drafted_response"] = True
        if resolution.get("context_fragments_added"):
            entry["has_context_injected"] = True
        results.append(entry)

    return results


def get_scope_summary(db_path: Optional[Path] = None) -> str:
    """Return a compact text summary of all active scopes for main agent context."""
    scopes = list_active_scopes(db_path)
    if not scopes:
        return "active_scopes: (none)"

    lines = ["active_scopes:"]
    for env in scopes:
        participants = ", ".join(p["name"] for p in env.get("participants", []))
        tasks = ", ".join(
            f'"{t["description"]}"'
            for t in env.get("task_tags", [])
            if t.get("status") == "active"
        )
        tier = env.get("authorization", {}).get("max_disclosure_tier", "task_context")
        lines.append(f"  - {env['scope_id']}: [{participants}] tasks={tasks} tier={tier}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scope rendering (section text injected into guest agent context)
# ---------------------------------------------------------------------------

def _render_disclosure_rules(tier: str) -> str:
    """Render authorization disclosure rules for a scope section."""
    rule = DISCLOSURE_RULES.get(tier, DISCLOSURE_RULES["task_context"])
    return f"{rule}\n"


def _render_accumulated_context(accumulated: list[dict]) -> str:
    """Render accumulated context fragments as a Conversation History section."""
    lines = ["\n### Conversation History"]
    for frag in accumulated:
        ts = frag.get("timestamp", "")
        date_str = ts[:10] if len(ts) >= 10 else "unknown"
        guest = frag.get("guest", "")
        safe_content = frag["content"].replace("\n", " ")
        prefix = f"{guest}: " if guest else ""
        lines.append(f"- [{date_str}] {prefix}{safe_content}")
    return "\n".join(lines) + "\n"


def _render_pending_follow_ups(entries: list[dict]) -> str:
    """Render pending_replies entries scoped to a single scope's participants."""
    lines = ["\n### Pending Follow-ups"]
    for e in entries:
        lines.append(
            f"- **{e.get('from', '?')}** re: {e.get('topic', '?')} "
            f"(id: {e.get('id', '?')}, "
            f"notify via {e.get('notify_channel', '?')} → {e.get('notify_recipient', '?')})"
        )
    lines.append(
        "When the listed person's next message arrives, forward it to the notify "
        "recipient via the **message** tool, then call pending_reply.py --complete "
        "--id <id>."
    )
    return "\n".join(lines) + "\n"


def render_scope_section(
    envelope: dict,
    pending_follow_ups: Optional[list[dict]] = None,
) -> str:
    """Render one scope envelope as a ``## Scope:`` markdown section.

    Used both by build_context.py (global assembly) and
    render_scope_context_for_sender (per-sender injection). Output format is
    stable — callers join sections with "\\n".

    pending_follow_ups: optional list of pending_replies entries already
    filtered to this scope's participants. Appended as a trailing
    "### Pending Follow-ups" subsection when non-empty.
    """
    participants = ", ".join(
        f"{p['name']} ({p['handle']})"
        for p in envelope.get("participants", [])
    )
    tier = envelope.get("authorization", {}).get("max_disclosure_tier", "task_context")
    caps = ", ".join(envelope.get("authorization", {}).get("granted_capabilities", []))
    tasks = "\n".join(
        f"  - {t['description']} [{t['status']}]"
        for t in envelope.get("task_tags", [])
    )
    injected = envelope.get("context_layers", {}).get("injected", [])
    injected_text = "\n".join(f["content"] for f in injected) if injected else ""
    accumulated = envelope.get("context_layers", {}).get("accumulated", [])

    scope_type = envelope.get("scope_type", "relationship")
    section = f"## Scope: {envelope['scope_id']}\n"
    section += f"Type: {scope_type}\n"
    section += f"Participants: {participants}\n"
    section += f"Authorization: {tier} | capabilities: {caps}\n"
    section += _render_disclosure_rules(tier)
    type_directive = _render_scope_type_directive(scope_type, envelope)
    if type_directive:
        section += type_directive
    if tasks:
        section += f"Tasks:\n{tasks}\n"
    if injected_text:
        section += f"\n### Context\n{injected_text}\n"
    if accumulated:
        section += _render_accumulated_context(accumulated)
    if pending_follow_ups:
        section += _render_pending_follow_ups(pending_follow_ups)
    return section


_FAMILY_HISTORY_DIRECTIVE = """
### Identity Override (this turn only)

For the duration of this turn, you are the **family historian** — not the
household's generic assistant. Your single overriding goal is to PRESERVE
every contribution this contributor sends and to gently elicit more. Do
NOT introduce yourself as the household's assistant; do not say "the
organizer" or "the host." You are the historian.

### Required tool sequence (every inbound, in order)

1. **`message`** — send a one-sentence ack to the inbound channel + chat_id
   *as your first tool call*. Examples: "Got it — give me a moment.",
   "Reading. One sec." This goes out before any heavier work so the
   contributor sees you heard them.

2. **`history_manage.py --context --contributor-id <inbound sender id>`** —
   pass through whatever the channel handed you (JID / LID / phone). The
   resolver returns the contributor UUID + recent fragments + open threads.

3. **`history_manage.py --write-artifact`** — capture the raw message
   verbatim, UNLESS the message is pure conversational filler (≤ 4 words
   with no proper nouns: "thanks", "good morning", "ok", "👍"). When in
   doubt, capture. A redundant artifact row is cheap; a lost memory is
   not. Suspicious / weird / off-topic content still gets captured —
   capture is preservation, not endorsement.

4. **`history_extract.py --artifact-id <id> --contributor-id <uuid>`** —
   only when step 3 wrote something.

5. **Send the substantive reply via `message` (a SECOND call).** Once
   capture and extraction are done, call `message` again with the same
   channel + chat_id and a substantive content body. Use one of the
   elicitation techniques (sensory anchor, concrete-instance probe,
   witness expansion, era anchor). Do NOT repeat the step-1 ack.

   The framework treats inline assistant text as silence when
   `message` has already been called in the turn, so the substantive
   reply MUST go through a second `message` call — inline text will
   not be delivered.

### Tool argument cheatsheet (don't invent flags)

- `history_manage.py --write-artifact --contributor-id <uuid> --kind text|image|audio|video --body "<text>" --channel whatsapp`
  — the text flag is `--body`, NOT `--content`.
- `history_extract.py --artifact-id <uuid> --contributor-id <uuid>`

### Scope-bound behavior

- You see only this contributor's data. Never reveal data from other
  contributors or other scopes.
- Escalate to the primary household member ONLY if the contributor expresses distress. Capture
  is *not* a substitute for escalation, and escalation is *not* a
  substitute for capture — when in doubt, do both.
- Skip the file-system spelunking the generic assistant resorts to.
  Your tools are listed above. If a tool returns an error, surface it
  in your reply and end the turn — do not grep the codebase.
"""


def _render_scope_type_directive(scope_type: str, envelope: dict) -> str:
    """Per-scope-type behavioral preamble injected with the scope context.

    For ``family_history``, this overrides the generic guest SOUL/AGENTS
    framing for the duration of the turn — the agent acts as the
    historian. Other scope types currently fall back to no directive
    (the generic guest SOUL/AGENTS apply).
    """
    if scope_type == "family_history":
        return _FAMILY_HISTORY_DIRECTIVE
    return ""


def _load_pending_replies(path: Optional[Path] = None) -> list[dict]:
    """Load the global pending_replies.json. Returns [] on missing/invalid."""
    p = path if path is not None else DEFAULT_PENDING_REPLIES_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _lid_to_phone(lid_prefix: str) -> Optional[str]:
    """Look up the phone for a WhatsApp LID via nanobot's lid_map.json.

    WhatsApp now delivers many inbound messages keyed on the sender's LID
    (privacy-masked identity). Scope participants are stored by phone-form
    JID (``<digits>@s.whatsapp.net``), so without this lookup an inbound
    LID fails to match any scope even when the sender is a real
    participant — which was the Adam regression on prod.

    Returns None if lid_map is missing or the LID isn't mapped.
    """
    persistent = os.environ.get("NANOBOT_PERSISTENT_DATA_DIR", "").strip()
    candidates: list[Path] = []
    if persistent:
        candidates.append(Path(persistent).expanduser() / "lid_map.json")
    candidates.append(Path.home() / ".nanobot" / "lid_map.json")
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        info = raw.get(lid_prefix) if isinstance(raw, dict) else None
        if isinstance(info, dict):
            phone = str(info.get("phone") or "").strip()
            if phone:
                return phone
    return None


_LID_SUFFIXES = ("@lid.whatsapp.net", "@lid")


def _sender_id_variants(sender_id: str) -> list[str]:
    """Expand a sender / chat id to the participant-ID forms stored in scopes.

    WhatsApp delivers either phone-digits form (``"14129739891"``) or
    LID-masked form. The LID may be bare (``"38457841848414"``), suffixed
    with ``"@lid"`` (older bridge), or with the full ``"@lid.whatsapp.net"``
    JID (current Baileys/Neonize bridges). Scope participants are stored
    as ``"<phone>@s.whatsapp.net"``. LIDs get resolved through
    ``lid_map.json`` (written by the WA bridge) so an inbound from — or
    outbound to — a participating guest's LID still matches their
    phone-form party_id.
    """
    if not sender_id:
        return []
    variants = [sender_id]
    if "@" not in sender_id and not sender_id.startswith("tg:"):
        # Bare digits — could be phone OR LID prefix. Add all the forms and
        # let the DB filter.
        variants.append(f"{sender_id}@s.whatsapp.net")
        variants.append(f"{sender_id}@lid")
        variants.append(f"tg:{sender_id}")
        phone = _lid_to_phone(sender_id)
        if phone and phone != sender_id:
            variants.append(phone)
            variants.append(f"{phone}@s.whatsapp.net")
    else:
        for suffix in _LID_SUFFIXES:
            if sender_id.endswith(suffix):
                lid_prefix = sender_id[: -len(suffix)]
                variants.append(lid_prefix)
                phone = _lid_to_phone(lid_prefix)
                if phone:
                    variants.append(phone)
                    variants.append(f"{phone}@s.whatsapp.net")
                break
    return variants


def _lookup_scopes_for_sender(
    sender_id: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return all active scopes for a sender across participant-ID variants + email.

    Runs in a single SQLite connection with one SELECT per index (participant,
    email) rather than one connection per variant — this is the per-turn
    injection hot path for every inbound guest message.
    """
    variants = _sender_id_variants(sender_id)
    is_email = "@" in sender_id and not any(
        sender_id.endswith(suffix)
        for suffix in ("@s.whatsapp.net", "@lid.whatsapp.net", "@lid", "@c.us", "@g.us")
    ) and not sender_id.startswith("tg:")

    scopes: list[dict] = []
    seen_ids: set[str] = set()

    with get_conn(db_path) as conn:
        if variants:
            placeholders = ",".join("?" * len(variants))
            rows = conn.execute(
                f"""SELECT s.envelope FROM scopes s
                    JOIN scope_participants sp ON s.scope_id = sp.scope_id
                    WHERE sp.participant_id IN ({placeholders})
                          AND s.status = 'active'
                    ORDER BY s.created_at""",
                variants,
            ).fetchall()
            for row in rows:
                env = json.loads(row["envelope"])
                if env["scope_id"] not in seen_ids:
                    scopes.append(env)
                    seen_ids.add(env["scope_id"])

        if is_email:
            normalized = normalize_email(sender_id)
            rows = conn.execute(
                """SELECT s.envelope FROM scopes s
                   JOIN scope_email_index sei ON s.scope_id = sei.scope_id
                   WHERE sei.email = ? AND s.status = 'active'
                   ORDER BY s.created_at""",
                (normalized,),
            ).fetchall()
            for row in rows:
                env = json.loads(row["envelope"])
                if env["scope_id"] not in seen_ids:
                    scopes.append(env)
                    seen_ids.add(env["scope_id"])

    return scopes


def _write_current_sender_scopes(scope_ids: list[str]) -> None:
    """Write the sender's scope IDs for guest tools to gate against.

    Read by tools/guest_scope_guard.py. Failures are swallowed so a bad
    filesystem doesn't crash the per-turn injection path.
    """
    workspace = os.environ.get("HOMER_GUEST_WORKSPACE") or os.environ.get("HOMER_WORKSPACE")
    if not workspace:
        return
    path = Path(workspace) / "current_sender_scopes.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scope_ids), encoding="utf-8")
    except OSError:
        pass


def render_scope_context_for_sender(
    sender_id: str,
    db_path: Optional[Path] = None,
    pending_replies_path: Optional[Path] = None,
) -> str:
    """Render the scope context injected per-turn for a guest agent.

    Looks up active scopes for ``sender_id`` via participant-ID variants
    (handles WhatsApp phone-digits, LID, Telegram, and full-JID forms) and
    the email index, then renders only those scopes — ensuring a sender
    sees no scope they are not a participant in. Pending follow-ups from
    ``pending_replies.json`` are matched to each scope by participant name
    and rendered inline with that scope's section.

    Side-effect: writes ``current_sender_scopes.json`` into the guest
    workspace so tools invoked in this turn can gate ``--scope-id`` /
    ``--event-id`` against the sender's membership.

    Returns "" when the sender has no active scopes — the caller (nanobot
    guest loop) should already have gated the inbound on allow_from, so an
    empty return here means a scope mismatch worth logging.
    """
    if not sender_id:
        return ""

    scopes = _lookup_scopes_for_sender(sender_id, db_path)
    _write_current_sender_scopes([env["scope_id"] for env in scopes])
    if not scopes:
        return ""

    pending = _load_pending_replies(pending_replies_path)

    # Pending replies match each scope in one of two ways:
    #   1. party_id match (exact) — when the entry has a party_id, it's only
    #      rendered into the scope that contains that exact participant.
    #      Avoids cross-scope collision when two scopes share a participant
    #      with the same display name.
    #   2. Fallback: participant NAME match (case-insensitive) — for legacy
    #      entries written before party_id was tracked. Same scope as their
    #      homonym, same collision caveat as before.
    sections: list[str] = []
    for env in scopes:
        participants = env.get("participants", [])
        scope_party_ids = {p.get("party_id", "") for p in participants if p.get("party_id")}
        scope_names = {p.get("name", "").lower() for p in participants if p.get("name")}
        matching = [
            e for e in pending
            if (
                e.get("party_id", "") in scope_party_ids
                if e.get("party_id")
                else e.get("from", "").lower() in scope_names
            )
        ]
        sections.append(render_scope_section(env, matching))
    return "# Scope Context\n\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Homer scope store management.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List active scopes")
    group.add_argument("--get-participants", action="store_true",
                       help="Print all active participant IDs (for allow_from)")
    group.add_argument("--summary", action="store_true",
                       help="Print scope summary for main agent")
    group.add_argument("--migrate-acl", nargs="?", const="default", metavar="ACL_PATH",
                       help="Migrate guest_agent_acl.json to SQLite")
    group.add_argument("--terminate", metavar="SCOPE_ID",
                       help="Terminate a scope by ID")
    group.add_argument("--pending-escalations", action="store_true",
                       help="List pending escalations (for heartbeat polling)")
    group.add_argument("--mark-surfaced", metavar="ESCALATION_ID",
                       help="Mark an escalation as surfaced (prevents re-notification)")
    group.add_argument("--undelivered-escalations", action="store_true",
                       help="List resolved but undelivered escalations (for guest heartbeat)")

    parser.add_argument("--db", metavar="DB_PATH", help="Override DB path")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None

    if args.list:
        scopes = list_active_scopes(db_path)
        print(json.dumps(scopes, indent=2))

    elif args.get_participants:
        ids = get_all_active_participant_ids(db_path)
        print(json.dumps(ids, indent=2))

    elif args.summary:
        print(get_scope_summary(db_path))

    elif args.migrate_acl:
        acl_path = None if args.migrate_acl == "default" else Path(args.migrate_acl)
        count = migrate_from_acl(acl_path, db_path)
        print(json.dumps({"migrated": count}))

    elif args.terminate:
        terminate_scope(args.terminate, db_path)
        print(json.dumps({"terminated": args.terminate}))

    elif args.pending_escalations:
        escalations = get_pending_escalations(db_path)
        print(json.dumps(escalations, indent=2))

    elif args.mark_surfaced:
        mark_escalation_surfaced(args.mark_surfaced, db_path)
        print(json.dumps({"surfaced": args.mark_surfaced}))

    elif args.undelivered_escalations:
        escalations = get_resolved_undelivered_escalations(db_path)
        print(json.dumps(escalations, indent=2))


if __name__ == "__main__":
    _cli()

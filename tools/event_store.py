#!/usr/bin/env python3
"""event_store.py — SQLite-backed event guest and RSVP store for Homer.

Separate from scopes.db: scopes handle authorization/context injection,
events.db handles operational event data (guests, RSVPs, invites).

Schema:
  event_guests(event_id, participant_id, name, phone, channel, added_at,
               rsvp_status, headcount, responded_at, rsvp_note, invited_at)

DB location: state/events.db (inside nanobot workspace) or HOMER_EVENTS_DB env var.

Not a Homer exec tool — consumed by event_manage.py and manage_event_guest.py.
"""

import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "events.db"

# Canonical RSVP status sets — import these instead of redefining.
ALL_RSVP_STATUSES = {"enrolled", "invited", "confirmed", "declined", "maybe"}
GUEST_RSVP_STATUSES = {"confirmed", "declined", "maybe"}


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return the events DB path. Override with HOMER_EVENTS_DB env var."""
    env = os.environ.get("HOMER_EVENTS_DB")
    return Path(env) if env else DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

_schema_initialized: set[str] = set()


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the events DB and ensure tables exist."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    key = str(path)
    if key not in _schema_initialized:
        _create_tables(conn)
        _schema_initialized.add(key)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_guests (
            event_id        TEXT NOT NULL,
            participant_id  TEXT NOT NULL,   -- WhatsApp JID or tg:<id>
            name            TEXT NOT NULL,
            phone           TEXT,
            channel         TEXT NOT NULL,   -- whatsapp | telegram
            added_at        TEXT NOT NULL,
            -- RSVP lifecycle
            rsvp_status     TEXT NOT NULL DEFAULT 'enrolled',
                            -- enrolled | invited | confirmed | declined | maybe
            headcount       INTEGER,
            responded_at    TEXT,
            rsvp_note       TEXT,
            invited_at      TEXT,
            -- RSVP webpage
            rsvp_token      TEXT,
            rsvp_fields_response TEXT,       -- JSON blob of custom field responses
            PRIMARY KEY (event_id, participant_id)
        );

        CREATE TABLE IF NOT EXISTS event_meta (
            event_id          TEXT PRIMARY KEY,
            rsvp_fields       TEXT,          -- JSON array of field definitions
            rsvp_deadline     TEXT,          -- ISO date string
            event_description TEXT,          -- rich description for RSVP page
            public_token      TEXT           -- shareable token for open RSVP links
        );
    """)
    _migrate_tables(conn)
    conn.commit()


def _migrate_tables(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing on older databases."""
    guest_cols = {row[1] for row in conn.execute("PRAGMA table_info(event_guests)").fetchall()}
    for col, col_type in [("rsvp_token", "TEXT"), ("rsvp_fields_response", "TEXT")]:
        if col not in guest_cols:
            conn.execute(f"ALTER TABLE event_guests ADD COLUMN {col} {col_type}")

    meta_cols = {row[1] for row in conn.execute("PRAGMA table_info(event_meta)").fetchall()}
    for col, col_type in [("public_token", "TEXT")]:
        if col not in meta_cols:
            conn.execute(f"ALTER TABLE event_meta ADD COLUMN {col} {col_type}")


# ---------------------------------------------------------------------------
# Guest CRUD
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_guest(
    event_id: str,
    participant_id: str,
    name: str,
    phone: Optional[str] = None,
    channel: str = "whatsapp",
    added_at: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Insert a guest into event_guests. Raises IntegrityError on duplicate."""
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO event_guests
               (event_id, participant_id, name, phone, channel, added_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, participant_id, name, phone, channel, added_at or _now_utc()),
        )
        conn.commit()


def remove_guest(
    event_id: str,
    participant_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Remove a guest from event_guests. Returns True if a row was deleted."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM event_guests WHERE event_id = ? AND participant_id = ?",
            (event_id, participant_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_guests(
    event_id: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return all guests for an event as a list of dicts."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM event_guests WHERE event_id = ? ORDER BY added_at",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def guest_count(
    event_id: str,
    db_path: Optional[Path] = None,
) -> int:
    """Return the number of guests enrolled in an event."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM event_guests WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def get_guest(
    event_id: str,
    participant_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return a single guest record or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM event_guests WHERE event_id = ? AND participant_id = ?",
            (event_id, participant_id),
        ).fetchone()
    return dict(row) if row else None


def find_guest_by_name(
    event_id: str,
    name: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Find a guest by case-insensitive name match. Returns first match or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM event_guests WHERE event_id = ? AND LOWER(name) = LOWER(?)",
            (event_id, name),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# RSVP operations
# ---------------------------------------------------------------------------

def update_rsvp(
    event_id: str,
    participant_id: str,
    status: str,
    headcount: Optional[int] = None,
    note: Optional[str] = None,
    fields_response: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Update RSVP status (and optionally custom field responses) for a guest."""
    fields_json = json.dumps(fields_response) if fields_response else None
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """UPDATE event_guests
               SET rsvp_status = ?,
                   headcount = COALESCE(?, headcount),
                   rsvp_note = COALESCE(?, rsvp_note),
                   rsvp_fields_response = COALESCE(?, rsvp_fields_response),
                   responded_at = ?
               WHERE event_id = ? AND participant_id = ?""",
            (status, headcount, note, fields_json, _now_utc(), event_id, participant_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_invited(
    event_id: str,
    participant_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Mark a guest as invited. Returns True if updated."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """UPDATE event_guests
               SET rsvp_status = 'invited', invited_at = ?
               WHERE event_id = ? AND participant_id = ? AND rsvp_status = 'enrolled'""",
            (_now_utc(), event_id, participant_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def rsvp_summary(
    event_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """Return RSVP summary with counts and headcount totals."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT rsvp_status, COUNT(*) as cnt,
                      COALESCE(SUM(COALESCE(headcount, 1)), 0) as total_headcount
               FROM event_guests WHERE event_id = ?
               GROUP BY rsvp_status""",
            (event_id,),
        ).fetchall()

    result: dict = {}
    for row in rows:
        result[row["rsvp_status"]] = {
            "count": row["cnt"],
            "headcount": row["total_headcount"],
        }
    return result


# ---------------------------------------------------------------------------
# RSVP tokens
# ---------------------------------------------------------------------------

def generate_rsvp_token(
    event_id: str,
    participant_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """Generate (or return existing) RSVP token for a guest. Idempotent."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT rsvp_token FROM event_guests WHERE event_id = ? AND participant_id = ?",
            (event_id, participant_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Guest {participant_id} not found in event {event_id}")
        if row["rsvp_token"]:
            return row["rsvp_token"]
        token = secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE event_guests SET rsvp_token = ? WHERE event_id = ? AND participant_id = ?",
            (token, event_id, participant_id),
        )
        conn.commit()
        return token


def get_guest_by_token(
    event_id: str,
    token: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Look up a guest by their RSVP token."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM event_guests WHERE event_id = ? AND rsvp_token = ?",
            (event_id, token),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Event meta (RSVP form config)
# ---------------------------------------------------------------------------

def set_event_meta(
    event_id: str,
    rsvp_fields: Optional[list[dict]] = None,
    rsvp_deadline: Optional[str] = None,
    event_description: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Upsert RSVP form configuration for an event."""
    fields_json = json.dumps(rsvp_fields) if rsvp_fields is not None else None
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO event_meta (event_id, rsvp_fields, rsvp_deadline, event_description)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(event_id) DO UPDATE SET
                   rsvp_fields = COALESCE(excluded.rsvp_fields, event_meta.rsvp_fields),
                   rsvp_deadline = COALESCE(excluded.rsvp_deadline, event_meta.rsvp_deadline),
                   event_description = COALESCE(excluded.event_description, event_meta.event_description)
            """,
            (event_id, fields_json, rsvp_deadline, event_description),
        )
        conn.commit()


def get_event_meta(
    event_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return event meta (RSVP form config) or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM event_meta WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    if result.get("rsvp_fields"):
        result["rsvp_fields"] = json.loads(result["rsvp_fields"])
    return result


def generate_public_token(
    event_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """Generate (or return existing) public RSVP token for an event. Idempotent."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT public_token FROM event_meta WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row and row["public_token"]:
            return row["public_token"]
        token = secrets.token_urlsafe(16)
        # Upsert — event_meta row may or may not exist yet
        conn.execute(
            """INSERT INTO event_meta (event_id, public_token) VALUES (?, ?)
               ON CONFLICT(event_id) DO UPDATE SET public_token = excluded.public_token""",
            (event_id, token),
        )
        conn.commit()
        return token


def get_event_by_public_token(
    event_id: str,
    public_token: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Verify a public token matches the event. Returns event_meta dict or None."""
    meta = get_event_meta(event_id, db_path)
    if not meta or meta.get("public_token") != public_token:
        return None
    return meta


def add_web_guest(
    event_id: str,
    name: str,
    db_path: Optional[Path] = None,
) -> dict:
    """Add or find a self-service web RSVP guest. Returns the guest dict."""
    participant_id = f"web:{name.strip().lower().replace(' ', '_')}"
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM event_guests WHERE event_id = ? AND participant_id = ?",
            (event_id, participant_id),
        ).fetchone()
        if existing:
            return dict(existing)
        now = _now_utc()
        conn.execute(
            """INSERT INTO event_guests
               (event_id, participant_id, name, channel, added_at)
               VALUES (?, ?, ?, 'web', ?)""",
            (event_id, participant_id, name.strip(), now),
        )
        conn.commit()
        return {
            "event_id": event_id,
            "participant_id": participant_id,
            "name": name.strip(),
            "phone": None,
            "channel": "web",
            "added_at": now,
            "rsvp_status": "enrolled",
            "headcount": None,
            "responded_at": None,
            "rsvp_note": None,
            "invited_at": None,
            "rsvp_token": None,
            "rsvp_fields_response": None,
        }


def rsvp_pending(
    event_id: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return guests who haven't responded (enrolled or invited, not confirmed/declined/maybe)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM event_guests
               WHERE event_id = ? AND rsvp_status IN ('enrolled', 'invited')
               ORDER BY added_at""",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Guest summary for status.md rendering
# ---------------------------------------------------------------------------

def render_guest_summary(event_id: str, db_path: Optional[Path] = None) -> str:
    """Render a human-readable guest summary line for status.md.

    Returns something like:
        ## Guests (12)
        8 confirmed (24 ppl) · 1 maybe · 3 pending
    """
    total = guest_count(event_id, db_path)
    summary = rsvp_summary(event_id, db_path)

    if total == 0:
        return "## Guests (0)"

    parts = []
    # Combine invited + enrolled into a single "pending" count
    pending_count = sum(
        summary.get(k, {}).get("count", 0) for k in ("invited", "enrolled")
    )
    for status_key in ("confirmed", "maybe", "declined"):
        info = summary.get(status_key)
        if not info or info["count"] == 0:
            continue
        count = info["count"]
        if status_key == "confirmed" and info["headcount"] > 0:
            parts.append(f"{count} confirmed ({info['headcount']} ppl)")
        else:
            parts.append(f"{count} {status_key}")
    if pending_count > 0:
        parts.append(f"{pending_count} pending")

    detail = " · ".join(parts) if parts else "all enrolled"
    return f"## Guests ({total})\n{detail}"

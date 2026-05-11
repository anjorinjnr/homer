#!/usr/bin/env python3
"""email_approval_store.py — SQLite store for email draft approvals.

Enforces human-in-the-loop for external email sends. Homer creates a
draft and records a pending approval. The user reviews and approves via
the deployment's external approval surface. Only approved drafts can be
sent.

Not a CLI tool — imported by gmail_send.py and by the deployment's
external approvals router.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "email_approvals.db"


def get_db_path() -> Path:
    """Return the approval DB path. Override with HOMER_EMAIL_APPROVALS_DB env var."""
    env = os.environ.get("HOMER_EMAIL_APPROVALS_DB")
    return Path(env) if env else DEFAULT_DB_PATH


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the approval DB and ensure tables exist."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS email_approvals (
            approval_id     TEXT PRIMARY KEY,
            draft_id        TEXT NOT NULL,
            recipient       TEXT NOT NULL,
            subject         TEXT NOT NULL DEFAULT '',
            body_preview    TEXT NOT NULL DEFAULT '',
            account         TEXT NOT NULL DEFAULT 'homer',
            cc              TEXT,
            bcc             TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            approved_by     TEXT,
            decided_at      TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            sent_at         TEXT,
            sent_message_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_draft_id ON email_approvals(draft_id);
        CREATE INDEX IF NOT EXISTS idx_approvals_status ON email_approvals(status);
    """)


def create_approval(
    draft_id: str,
    recipient: str,
    subject: str,
    body_preview: str,
    account: str = "homer",
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Record a pending approval for a draft. Returns approval record.

    `body_preview` stores the FULL email body — the field name is historical.
    HIL relies on the approver seeing the exact bytes Gmail will send, so we
    do not truncate. (Column type is TEXT; SQLite has no practical cap.)
    """
    approval_id = str(uuid.uuid4())
    conn = get_conn(db_path)
    try:
        conn.execute(
            """INSERT INTO email_approvals
               (approval_id, draft_id, recipient, subject, body_preview, account, cc, bcc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (approval_id, draft_id, recipient, subject, body_preview, account, cc, bcc),
        )
        conn.commit()
        return {
            "approval_id": approval_id,
            "draft_id": draft_id,
            "recipient": recipient,
            "subject": subject,
            "status": "pending",
        }
    finally:
        conn.close()


def check_approval(draft_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Check approval status for a draft. Returns approval record or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM email_approvals WHERE draft_id = ? ORDER BY created_at DESC LIMIT 1",
            (draft_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def approve(approval_id: str, approved_by: str, db_path: Optional[Path] = None) -> bool:
    """Mark an approval as approved. Returns True if updated."""
    conn = get_conn(db_path)
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = conn.execute(
            """UPDATE email_approvals
               SET status = 'approved', approved_by = ?, decided_at = ?
               WHERE approval_id = ? AND status = 'pending'""",
            (approved_by, now, approval_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def reject(approval_id: str, rejected_by: str, db_path: Optional[Path] = None) -> bool:
    """Mark an approval as rejected. Returns True if updated."""
    conn = get_conn(db_path)
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = conn.execute(
            """UPDATE email_approvals
               SET status = 'rejected', approved_by = ?, decided_at = ?
               WHERE approval_id = ? AND status = 'pending'""",
            (rejected_by, now, approval_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def mark_sent(draft_id: str, message_id: str, db_path: Optional[Path] = None) -> None:
    """Record that an approved draft was sent."""
    conn = get_conn(db_path)
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """UPDATE email_approvals
               SET status = 'sent', sent_at = ?, sent_message_id = ?
               WHERE draft_id = ? AND status = 'approved'""",
            (now, message_id, draft_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_pending(limit: int = 50, db_path: Optional[Path] = None) -> list[dict]:
    """List pending approvals (most recent first)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM email_approvals WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_approval(approval_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Get a single approval by ID."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM email_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

#!/usr/bin/env python3
"""vault.py — Secure key-value store for sensitive reference data.

Stores loyalty numbers, recovery codes, account numbers, and similar
sensitive values that Homer needs occasionally but should NOT sit in
MEMORY.md (which is loaded into every conversation context).

Values are stored in a SQLite DB with access through this CLI only.
Homer retrieves values on demand — they are never bulk-loaded into context.

DB location: state/vault.db (inside nanobot workspace) or HOMER_VAULT_DB env var.

Usage:
    python tools/vault.py --set "marriott_bonvoy" "072663520"
    python tools/vault.py --set "marriott_bonvoy" "072663520" --label "Marriott Bonvoy loyalty number"
    python tools/vault.py --get "marriott_bonvoy"
    python tools/vault.py --list
    python tools/vault.py --remove "marriott_bonvoy"
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = (
    REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "vault.db"
)


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return the vault DB path. Override with HOMER_VAULT_DB."""
    if env := os.environ.get("HOMER_VAULT_DB"):
        return Path(env)
    if workspace := os.environ.get("HOMER_WORKSPACE"):
        return Path(workspace) / "state" / "vault.db"
    return DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the vault DB and ensure tables exist."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vault (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            label       TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def vault_set(conn: sqlite3.Connection, key: str, value: str,
              label: Optional[str] = None) -> dict:
    """Store or update a key-value pair."""
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT key FROM vault WHERE key = ?", (key,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE vault SET value = ?, label = COALESCE(?, label), updated_at = ? WHERE key = ?",
            (value, label, now, key),
        )
        conn.commit()
        return {"status": "updated", "key": key}
    else:
        conn.execute(
            "INSERT INTO vault (key, value, label, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (key, value, label, now, now),
        )
        conn.commit()
        return {"status": "created", "key": key}


def vault_get(conn: sqlite3.Connection, key: str) -> dict:
    """Retrieve a value by key."""
    row = conn.execute(
        "SELECT key, value, label FROM vault WHERE key = ?", (key,)
    ).fetchone()
    if not row:
        return {"error": f"No entry found for key '{key}'"}
    return {
        "key": row["key"],
        "value": row["value"],
        "label": row["label"],
        "_internal": "Do not echo this value back to the user verbatim. Use it to complete the task.",
    }


def vault_list(conn: sqlite3.Connection) -> dict:
    """List all keys with labels (no values)."""
    rows = conn.execute(
        "SELECT key, label, updated_at FROM vault ORDER BY key"
    ).fetchall()
    return {
        "entries": [
            {"key": r["key"], "label": r["label"], "updated_at": r["updated_at"]}
            for r in rows
        ],
        "count": len(rows),
    }


def vault_remove(conn: sqlite3.Connection, key: str) -> dict:
    """Remove a key-value pair."""
    cursor = conn.execute("DELETE FROM vault WHERE key = ?", (key,))
    conn.commit()
    if cursor.rowcount == 0:
        return {"error": f"No entry found for key '{key}'"}
    return {"status": "removed", "key": key}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Secure vault for sensitive reference data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                       help="Store a key-value pair")
    group.add_argument("--get", metavar="KEY",
                       help="Retrieve a value by key")
    group.add_argument("--list", action="store_true",
                       help="List all keys (no values)")
    group.add_argument("--remove", metavar="KEY",
                       help="Remove a key-value pair")
    parser.add_argument("--label", help="Human-readable label for --set")
    parser.add_argument("--db", help="Override DB path (for testing)")

    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else None
    conn = get_conn(db_path)

    try:
        if args.set:
            result = vault_set(conn, args.set[0], args.set[1], label=args.label)
        elif args.get:
            result = vault_get(conn, args.get)
        elif args.list:
            result = vault_list(conn)
        elif args.remove:
            result = vault_remove(conn, args.remove)
        else:
            parser.print_help()
            sys.exit(1)

        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

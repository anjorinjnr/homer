#!/usr/bin/env python3
"""maintenance.py — SQLite-backed home maintenance tracker for Homer.

Tracks recurring maintenance tasks, service providers, appliances, and
home improvement projects.  All output is JSON.

DB location: state/maintenance.db (inside nanobot workspace) or
HOMER_MAINTENANCE_DB env var.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = (
    REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "maintenance.db"
)


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return the maintenance DB path. Override with HOMER_MAINTENANCE_DB."""
    if env := os.environ.get("HOMER_MAINTENANCE_DB"):
        return Path(env)
    if workspace := os.environ.get("HOMER_WORKSPACE"):
        return Path(workspace) / "state" / "maintenance.db"
    return DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the maintenance DB and ensure tables exist."""
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
        CREATE TABLE IF NOT EXISTS maintenance_tasks (
            task_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            system          TEXT NOT NULL,
            frequency_days  INTEGER NOT NULL,
            last_completed  TEXT,
            next_due        TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS task_completions (
            completion_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL,
            completed_date  TEXT NOT NULL,
            done_by         TEXT,
            cost            REAL,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (task_id) REFERENCES maintenance_tasks(task_id)
        );

        CREATE TABLE IF NOT EXISTS service_providers (
            provider_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            specialty       TEXT NOT NULL,
            phone           TEXT,
            email           TEXT,
            notes           TEXT,
            rating          INTEGER,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS appliances (
            appliance_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            brand           TEXT,
            model           TEXT,
            serial_number   TEXT,
            install_date    TEXT,
            warranty_until  TEXT,
            location        TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS home_projects (
            project_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            description     TEXT,
            started_date    TEXT,
            completed_date  TEXT,
            budget          REAL,
            actual_cost     REAL,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS project_items (
            item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      INTEGER NOT NULL,
            description     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            assignee        TEXT,
            completed_date  TEXT,
            FOREIGN KEY (project_id) REFERENCES home_projects(project_id)
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return date.today().isoformat()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def _err(msg: str) -> None:
    print(json.dumps({"error": msg}))
    sys.exit(1)


# ---------------------------------------------------------------------------
# Maintenance tasks
# ---------------------------------------------------------------------------

def add_task(name: str, system: str, frequency_days: int,
             notes: Optional[str] = None) -> dict:
    """Create a recurring maintenance task. Returns the new task dict."""
    next_due = (date.today() + timedelta(days=frequency_days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO maintenance_tasks (name, system, frequency_days, next_due, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (name, system, frequency_days, next_due, notes),
        )
        conn.commit()
        task_id = cur.lastrowid
    return {"status": "created", "task_id": task_id, "name": name,
            "system": system, "frequency_days": frequency_days,
            "next_due": next_due}


def complete_task(task_id: int, done_by: Optional[str] = None,
                  cost: Optional[float] = None, notes: Optional[str] = None,
                  completed_date: Optional[str] = None) -> dict:
    """Log a completion and advance the schedule."""
    comp_date = completed_date or _today()
    try:
        parsed_date = date.fromisoformat(comp_date)
    except ValueError:
        _err("Invalid date format. Must be YYYY-MM-DD.")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM maintenance_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            _err(f"Task {task_id} not found")
        freq = row["frequency_days"]
        next_due = (parsed_date + timedelta(days=freq)).isoformat()
        conn.execute(
            """INSERT INTO task_completions (task_id, completed_date, done_by, cost, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, comp_date, done_by, cost, notes),
        )
        conn.execute(
            """UPDATE maintenance_tasks
               SET last_completed = ?, next_due = ?
               WHERE task_id = ?""",
            (comp_date, next_due, task_id),
        )
        conn.commit()
    return {"status": "completed", "task_id": task_id,
            "completed_date": comp_date, "next_due": next_due}


def list_tasks(system: Optional[str] = None, due: bool = False) -> list[dict]:
    """List tasks, optionally filtered by system or due-soon status."""
    with get_conn() as conn:
        query = "SELECT * FROM maintenance_tasks"
        params: list = []
        clauses: list[str] = []
        if system:
            clauses.append("LOWER(system) = LOWER(?)")
            params.append(system)
        if due:
            cutoff = (date.today() + timedelta(days=7)).isoformat()
            clauses.append("next_due <= ?")
            params.append(cutoff)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY next_due"
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def task_history(task_id: int) -> list[dict]:
    """Return completion history for a task."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT task_id FROM maintenance_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            _err(f"Task {task_id} not found")
        rows = conn.execute(
            """SELECT * FROM task_completions WHERE task_id = ?
               ORDER BY completed_date DESC, completion_id DESC""",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Service providers
# ---------------------------------------------------------------------------

def add_provider(name: str, specialty: str, phone: Optional[str] = None,
                 email: Optional[str] = None, notes: Optional[str] = None,
                 rating: Optional[int] = None) -> dict:
    if rating is not None and not 1 <= rating <= 5:
        _err("Rating must be between 1 and 5")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO service_providers (name, specialty, phone, email, notes, rating)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, specialty, phone, email, notes, rating),
        )
        conn.commit()
        pid = cur.lastrowid
    return {"status": "created", "provider_id": pid, "name": name,
            "specialty": specialty}


def list_providers(specialty: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if specialty:
            rows = conn.execute(
                "SELECT * FROM service_providers WHERE LOWER(specialty) = LOWER(?) ORDER BY name",
                (specialty,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM service_providers ORDER BY name"
            ).fetchall()
    return [dict(r) for r in rows]


def update_provider(provider_id: int, **kwargs) -> dict:
    allowed = {"name", "phone", "email", "notes", "rating", "specialty"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        _err("No fields to update")
    if "rating" in updates and not 1 <= updates["rating"] <= 5:
        _err("Rating must be between 1 and 5")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT provider_id FROM service_providers WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        if not row:
            _err(f"Provider {provider_id} not found")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [provider_id]
        conn.execute(
            f"UPDATE service_providers SET {set_clause} WHERE provider_id = ?",
            vals,
        )
        conn.commit()
    return {"status": "updated", "provider_id": provider_id}


def remove_provider(provider_id: int) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM service_providers WHERE provider_id = ?", (provider_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            _err(f"Provider {provider_id} not found")
    return {"status": "removed", "provider_id": provider_id}


# ---------------------------------------------------------------------------
# Appliances
# ---------------------------------------------------------------------------

def add_appliance(name: str, brand: Optional[str] = None,
                  model: Optional[str] = None, serial_number: Optional[str] = None,
                  install_date: Optional[str] = None,
                  warranty_until: Optional[str] = None,
                  location: Optional[str] = None,
                  notes: Optional[str] = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO appliances
               (name, brand, model, serial_number, install_date, warranty_until, location, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, brand, model, serial_number, install_date, warranty_until,
             location, notes),
        )
        conn.commit()
        aid = cur.lastrowid
    return {"status": "created", "appliance_id": aid, "name": name}


def list_appliances(location: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if location:
            rows = conn.execute(
                "SELECT * FROM appliances WHERE LOWER(location) = LOWER(?) ORDER BY name",
                (location,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appliances ORDER BY name"
            ).fetchall()
    return [dict(r) for r in rows]


def update_appliance(appliance_id: int, **kwargs) -> dict:
    allowed = {"name", "brand", "model", "serial_number", "install_date",
               "warranty_until", "location", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        _err("No fields to update")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT appliance_id FROM appliances WHERE appliance_id = ?",
            (appliance_id,),
        ).fetchone()
        if not row:
            _err(f"Appliance {appliance_id} not found")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [appliance_id]
        conn.execute(
            f"UPDATE appliances SET {set_clause} WHERE appliance_id = ?", vals
        )
        conn.commit()
    return {"status": "updated", "appliance_id": appliance_id}


def remove_appliance(appliance_id: int) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM appliances WHERE appliance_id = ?", (appliance_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            _err(f"Appliance {appliance_id} not found")
    return {"status": "removed", "appliance_id": appliance_id}


# ---------------------------------------------------------------------------
# Home projects
# ---------------------------------------------------------------------------

def add_project(name: str, description: Optional[str] = None,
                budget: Optional[float] = None,
                started_date: Optional[str] = None) -> dict:
    sd = started_date or _today()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO home_projects (name, description, budget, started_date)
               VALUES (?, ?, ?, ?)""",
            (name, description, budget, sd),
        )
        conn.commit()
        pid = cur.lastrowid
    return {"status": "created", "project_id": pid, "name": name}


def list_projects(status: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM home_projects WHERE LOWER(status) = LOWER(?) ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM home_projects ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_project(project_id: int, **kwargs) -> dict:
    allowed = {"name", "status", "description", "started_date", "completed_date",
               "budget", "actual_cost", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        _err("No fields to update")
    valid_statuses = {"active", "completed", "on_hold"}
    if "status" in updates:
        updates["status"] = updates["status"].lower()
    if "status" in updates and updates["status"] not in valid_statuses:
        _err(f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM home_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            _err(f"Project {project_id} not found")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [project_id]
        conn.execute(
            f"UPDATE home_projects SET {set_clause} WHERE project_id = ?", vals
        )
        conn.commit()
    return {"status": "updated", "project_id": project_id}


def add_project_item(project_id: int, description: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM home_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            _err(f"Project {project_id} not found")
        cur = conn.execute(
            "INSERT INTO project_items (project_id, description) VALUES (?, ?)",
            (project_id, description),
        )
        conn.commit()
        iid = cur.lastrowid
    return {"status": "created", "item_id": iid, "project_id": project_id,
            "description": description}


def check_project_item(project_id: int, item_substr: str) -> dict:
    """Mark a project item as done by case-insensitive substring match."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM home_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            _err(f"Project {project_id} not found")
        items = conn.execute(
            "SELECT * FROM project_items WHERE project_id = ? AND status = 'open'",
            (project_id,),
        ).fetchall()
        matched = [i for i in items if item_substr.lower() in i["description"].lower()]
        if not matched:
            _err(f"No open item matching '{item_substr}' in project {project_id}")
        if len(matched) > 1:
            _err(f"Multiple items match '{item_substr}'. Be more specific.")
        item = matched[0]
        conn.execute(
            "UPDATE project_items SET status = 'done', completed_date = ? WHERE item_id = ?",
            (_today(), item["item_id"]),
        )
        conn.commit()
    return {"status": "checked", "item_id": item["item_id"],
            "description": item["description"]}


def project_status(project_id: int) -> dict:
    """Full project detail with all items."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM home_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            _err(f"Project {project_id} not found")
        items = conn.execute(
            "SELECT * FROM project_items WHERE project_id = ? ORDER BY item_id",
            (project_id,),
        ).fetchall()
    proj = dict(row)
    proj["items"] = [dict(i) for i in items]
    return proj


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard() -> dict:
    """Overdue tasks, upcoming (7d), active projects, warranty expirations (90d)."""
    today_str = _today()
    cutoff_7d = (date.today() + timedelta(days=7)).isoformat()
    cutoff_90d = (date.today() + timedelta(days=90)).isoformat()

    with get_conn() as conn:
        overdue = conn.execute(
            "SELECT * FROM maintenance_tasks WHERE next_due < ? ORDER BY next_due",
            (today_str,),
        ).fetchall()

        upcoming = conn.execute(
            "SELECT * FROM maintenance_tasks WHERE next_due >= ? AND next_due <= ? ORDER BY next_due",
            (today_str, cutoff_7d),
        ).fetchall()

        active_projects = conn.execute(
            "SELECT * FROM home_projects WHERE status = 'active' ORDER BY created_at DESC"
        ).fetchall()

        expiring = conn.execute(
            "SELECT * FROM appliances WHERE warranty_until IS NOT NULL AND warranty_until <= ? AND warranty_until >= ? ORDER BY warranty_until",
            (cutoff_90d, today_str),
        ).fetchall()

    return {
        "overdue_tasks": [dict(r) for r in overdue],
        "upcoming_tasks": [dict(r) for r in upcoming],
        "active_projects": [dict(r) for r in active_projects],
        "warranty_expirations": [dict(r) for r in expiring],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _JsonParser(argparse.ArgumentParser):
    """ArgumentParser that outputs errors as JSON to stdout (tool contract)."""

    def error(self, message: str) -> None:
        print(json.dumps({"error": message}))
        sys.exit(1)


def main(argv: Optional[list[str]] = None) -> None:
    parser = _JsonParser(description="Home maintenance tracker.")

    # Top-level action flags (mutually exclusive groups)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--add-task", action="store_true")
    action.add_argument("--complete-task", action="store_true")
    action.add_argument("--list-tasks", action="store_true")
    action.add_argument("--task-history", action="store_true")
    action.add_argument("--add-provider", action="store_true")
    action.add_argument("--list-providers", action="store_true")
    action.add_argument("--update-provider", action="store_true")
    action.add_argument("--remove-provider", action="store_true")
    action.add_argument("--add-appliance", action="store_true")
    action.add_argument("--list-appliances", action="store_true")
    action.add_argument("--update-appliance", action="store_true")
    action.add_argument("--remove-appliance", action="store_true")
    action.add_argument("--add-project", action="store_true")
    action.add_argument("--list-projects", action="store_true")
    action.add_argument("--update-project", action="store_true")
    action.add_argument("--add-project-item", action="store_true")
    action.add_argument("--check-project-item", action="store_true")
    action.add_argument("--project-status", action="store_true")
    action.add_argument("--dashboard", action="store_true")

    # Shared arguments
    parser.add_argument("--name")
    parser.add_argument("--system")
    parser.add_argument("--frequency", type=int, dest="frequency_days")
    parser.add_argument("--notes")
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--done-by")
    parser.add_argument("--cost", type=float)
    parser.add_argument("--date")
    parser.add_argument("--due", action="store_true")
    parser.add_argument("--specialty")
    parser.add_argument("--phone")
    parser.add_argument("--email")
    parser.add_argument("--rating", type=int)
    parser.add_argument("--provider-id", type=int)
    parser.add_argument("--brand")
    parser.add_argument("--model")
    parser.add_argument("--serial")
    parser.add_argument("--install-date")
    parser.add_argument("--warranty-until")
    parser.add_argument("--location")
    parser.add_argument("--appliance-id", type=int)
    parser.add_argument("--description")
    parser.add_argument("--budget", type=float)
    parser.add_argument("--started-date")
    parser.add_argument("--completed-date")
    parser.add_argument("--actual-cost", type=float)
    parser.add_argument("--status")
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--item")

    args = parser.parse_args(argv)

    # -- Dispatch --
    if args.add_task:
        if not args.name:
            _err("--add-task requires --name")
        if not args.system:
            _err("--add-task requires --system")
        if not args.frequency_days:
            _err("--add-task requires --frequency")
        _ok(add_task(args.name, args.system, args.frequency_days, args.notes))

    elif args.complete_task:
        if not args.task_id:
            _err("--complete-task requires --task-id")
        _ok(complete_task(args.task_id, args.done_by, args.cost, args.notes,
                          args.date))

    elif args.list_tasks:
        _ok(list_tasks(args.system, args.due))

    elif args.task_history:
        if not args.task_id:
            _err("--task-history requires --task-id")
        _ok(task_history(args.task_id))

    elif args.add_provider:
        if not args.name:
            _err("--add-provider requires --name")
        if not args.specialty:
            _err("--add-provider requires --specialty")
        _ok(add_provider(args.name, args.specialty, args.phone, args.email,
                         args.notes, args.rating))

    elif args.list_providers:
        _ok(list_providers(args.specialty))

    elif args.update_provider:
        if not args.provider_id:
            _err("--update-provider requires --provider-id")
        _ok(update_provider(args.provider_id, name=args.name, phone=args.phone,
                            email=args.email, notes=args.notes,
                            rating=args.rating, specialty=args.specialty))

    elif args.remove_provider:
        if not args.provider_id:
            _err("--remove-provider requires --provider-id")
        _ok(remove_provider(args.provider_id))

    elif args.add_appliance:
        if not args.name:
            _err("--add-appliance requires --name")
        _ok(add_appliance(args.name, args.brand, args.model, args.serial,
                          args.install_date, args.warranty_until, args.location,
                          args.notes))

    elif args.list_appliances:
        _ok(list_appliances(args.location))

    elif args.update_appliance:
        if not args.appliance_id:
            _err("--update-appliance requires --appliance-id")
        _ok(update_appliance(args.appliance_id, name=args.name, brand=args.brand,
                             model=args.model, serial_number=args.serial,
                             install_date=args.install_date,
                             warranty_until=args.warranty_until,
                             location=args.location, notes=args.notes))

    elif args.remove_appliance:
        if not args.appliance_id:
            _err("--remove-appliance requires --appliance-id")
        _ok(remove_appliance(args.appliance_id))

    elif args.add_project:
        if not args.name:
            _err("--add-project requires --name")
        _ok(add_project(args.name, args.description, args.budget,
                        args.started_date))

    elif args.list_projects:
        _ok(list_projects(args.status))

    elif args.update_project:
        if not args.project_id:
            _err("--update-project requires --project-id")
        _ok(update_project(args.project_id, name=args.name, status=args.status,
                           description=args.description,
                           started_date=args.started_date,
                           completed_date=args.completed_date,
                           budget=args.budget, actual_cost=args.actual_cost,
                           notes=args.notes))

    elif args.add_project_item:
        if not args.project_id:
            _err("--add-project-item requires --project-id")
        if not args.description:
            _err("--add-project-item requires --description")
        _ok(add_project_item(args.project_id, args.description))

    elif args.check_project_item:
        if not args.project_id:
            _err("--check-project-item requires --project-id")
        if not args.item:
            _err("--check-project-item requires --item")
        _ok(check_project_item(args.project_id, args.item))

    elif args.project_status:
        if not args.project_id:
            _err("--project-status requires --project-id")
        _ok(project_status(args.project_id))

    elif args.dashboard:
        _ok(dashboard())


if __name__ == "__main__":
    main()

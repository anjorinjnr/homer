#!/usr/bin/env python3
"""health_records.py -- SQLite-backed family health & medical records for Homer.

Tracks family members, medical visits, vaccinations, medications, and symptoms.
All data stored locally in SQLite; no external API calls.

DB location: state/health.db (inside nanobot workspace) or HOMER_HEALTH_DB env var.

Usage (via Homer exec tool):
    python tools/health_records.py --add-member --name "Alex" --dob 2021-07-12
    python tools/health_records.py --list-members
    python tools/health_records.py --get-member --name "Alex"
    python tools/health_records.py --log-visit --member "Alex" --date 2026-03-15 --provider "Dr. Smith" --type checkup
    python tools/health_records.py --dashboard

Output is always JSON.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = (
    REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "health.db"
)


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return the health DB path. Override with HOMER_HEALTH_DB env var."""
    env = os.environ.get("HOMER_HEALTH_DB")
    return Path(env) if env else DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the health DB and ensure tables exist."""
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
        CREATE TABLE IF NOT EXISTS family_members (
            member_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            date_of_birth   TEXT,
            blood_type      TEXT,
            allergies       TEXT,
            primary_doctor  TEXT,
            insurance_info  TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS medical_visits (
            visit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id       INTEGER NOT NULL,
            visit_date      TEXT NOT NULL,
            provider        TEXT NOT NULL,
            visit_type      TEXT,
            diagnosis       TEXT,
            treatment       TEXT,
            follow_up_date  TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (member_id) REFERENCES family_members(member_id)
        );

        CREATE TABLE IF NOT EXISTS vaccinations (
            vaccination_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id       INTEGER NOT NULL,
            vaccine_name    TEXT NOT NULL,
            date_given      TEXT NOT NULL,
            provider        TEXT,
            lot_number      TEXT,
            next_due        TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (member_id) REFERENCES family_members(member_id)
        );

        CREATE TABLE IF NOT EXISTS medications (
            medication_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id       INTEGER NOT NULL,
            name            TEXT NOT NULL,
            dosage          TEXT,
            frequency       TEXT,
            prescriber      TEXT,
            pharmacy        TEXT,
            refill_date     TEXT,
            active          INTEGER NOT NULL DEFAULT 1,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (member_id) REFERENCES family_members(member_id)
        );

        CREATE TABLE IF NOT EXISTS symptom_log (
            symptom_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id       INTEGER NOT NULL,
            logged_at       TEXT NOT NULL,
            symptoms        TEXT NOT NULL,
            severity        INTEGER,
            temperature     REAL,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (member_id) REFERENCES family_members(member_id)
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ok(data: dict) -> None:
    print(json.dumps(data, indent=2))


def _err(msg: str) -> None:
    print(json.dumps({"error": msg}, indent=2))
    sys.exit(1)


def _resolve_member(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Case-insensitive match on member name.

    Tries exact match first, then falls back to partial (LIKE) match.
    Returns None if no match, or raises SystemExit if multiple partial matches
    to prevent logging data to the wrong family member.
    """
    # Exact match first (case-insensitive)
    row = conn.execute(
        "SELECT * FROM family_members WHERE LOWER(name) = LOWER(?)",
        (name,),
    ).fetchone()
    if row:
        return dict(row)

    # Partial match fallback
    rows = conn.execute(
        "SELECT * FROM family_members WHERE LOWER(name) LIKE LOWER(?)",
        (f"%{name}%",),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    if len(rows) > 1:
        names = [r["name"] for r in rows]
        _err(f"Ambiguous name '{name}' matches multiple members: {', '.join(names)}. Please be more specific.")
    return None


def _require_member(conn: sqlite3.Connection, name: str) -> dict:
    """Resolve member or exit with error."""
    member = _resolve_member(conn, name)
    if not member:
        _err(f"No member found matching '{name}'")
    return member  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Member CRUD
# ---------------------------------------------------------------------------

def add_member(
    name: str,
    dob: Optional[str] = None,
    blood_type: Optional[str] = None,
    allergies: Optional[str] = None,
    doctor: Optional[str] = None,
    insurance: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO family_members
               (name, date_of_birth, blood_type, allergies, primary_doctor, insurance_info, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, dob, blood_type, allergies, doctor, insurance, notes),
        )
        conn.commit()
        return {"status": "ok", "message": f"Added member '{name}'"}
    except sqlite3.IntegrityError:
        return {"error": f"Member '{name}' already exists"}
    finally:
        conn.close()


def list_members() -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT member_id, name, date_of_birth, blood_type, allergies, primary_doctor FROM family_members ORDER BY name"
    ).fetchall()
    conn.close()
    return {"status": "ok", "members": [dict(r) for r in rows]}


def get_member(name: str) -> dict:
    conn = get_conn()
    member = _resolve_member(conn, name)
    if not member:
        conn.close()
        return {"error": f"No member found matching '{name}'"}

    mid = member["member_id"]

    # Active medications
    meds = conn.execute(
        "SELECT * FROM medications WHERE member_id = ? AND active = 1 ORDER BY name",
        (mid,),
    ).fetchall()

    # Recent visits (last 5)
    visits = conn.execute(
        "SELECT * FROM medical_visits WHERE member_id = ? ORDER BY visit_date DESC LIMIT 5",
        (mid,),
    ).fetchall()

    # Upcoming vaccinations (next_due not null and >= today)
    vaccines = conn.execute(
        "SELECT * FROM vaccinations WHERE member_id = ? AND next_due IS NOT NULL AND next_due >= ? ORDER BY next_due",
        (mid, _today()),
    ).fetchall()

    conn.close()
    return {
        "status": "ok",
        "member": member,
        "active_medications": [dict(r) for r in meds],
        "recent_visits": [dict(r) for r in visits],
        "upcoming_vaccinations": [dict(r) for r in vaccines],
    }


def update_member(
    name: str,
    allergies: Optional[str] = None,
    doctor: Optional[str] = None,
    insurance: Optional[str] = None,
    notes: Optional[str] = None,
    dob: Optional[str] = None,
    blood_type: Optional[str] = None,
) -> dict:
    conn = get_conn()
    member = _resolve_member(conn, name)
    if not member:
        conn.close()
        return {"error": f"No member found matching '{name}'"}

    updates = []
    params = []
    if allergies is not None:
        updates.append("allergies = ?")
        params.append(allergies)
    if doctor is not None:
        updates.append("primary_doctor = ?")
        params.append(doctor)
    if insurance is not None:
        updates.append("insurance_info = ?")
        params.append(insurance)
    if notes is not None:
        updates.append("notes = ?")
        params.append(notes)
    if dob is not None:
        updates.append("date_of_birth = ?")
        params.append(dob)
    if blood_type is not None:
        updates.append("blood_type = ?")
        params.append(blood_type)

    if not updates:
        conn.close()
        return {"error": "No fields to update"}

    params.append(member["member_id"])
    conn.execute(
        f"UPDATE family_members SET {', '.join(updates)} WHERE member_id = ?",
        params,
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Updated member '{member['name']}'"}


def remove_member(name: str) -> dict:
    conn = get_conn()
    member = _resolve_member(conn, name)
    if not member:
        conn.close()
        return {"error": f"No member found matching '{name}'"}

    mid = member["member_id"]
    conn.execute("DELETE FROM symptom_log WHERE member_id = ?", (mid,))
    conn.execute("DELETE FROM medications WHERE member_id = ?", (mid,))
    conn.execute("DELETE FROM vaccinations WHERE member_id = ?", (mid,))
    conn.execute("DELETE FROM medical_visits WHERE member_id = ?", (mid,))
    conn.execute("DELETE FROM family_members WHERE member_id = ?", (mid,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Removed member '{member['name']}' and all associated records"}


# ---------------------------------------------------------------------------
# Medical visits
# ---------------------------------------------------------------------------

def log_visit(
    member_name: str,
    date: str,
    provider: str,
    visit_type: Optional[str] = None,
    diagnosis: Optional[str] = None,
    treatment: Optional[str] = None,
    follow_up: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    conn.execute(
        """INSERT INTO medical_visits
           (member_id, visit_date, provider, visit_type, diagnosis, treatment, follow_up_date, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (member["member_id"], date, provider, visit_type, diagnosis, treatment, follow_up, notes),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Logged {visit_type or 'visit'} for {member['name']} on {date}"}


def list_visits(
    member_name: str,
    year: Optional[int] = None,
    visit_type: Optional[str] = None,
) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    mid = member["member_id"]

    query = "SELECT * FROM medical_visits WHERE member_id = ?"
    params: list = [mid]

    if year:
        query += " AND visit_date LIKE ?"
        params.append(f"{year}-%")
    if visit_type:
        query += " AND visit_type = ?"
        params.append(visit_type)

    query += " ORDER BY visit_date DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {"status": "ok", "member": member["name"], "visits": [dict(r) for r in rows]}


def upcoming_visits() -> dict:
    conn = get_conn()
    today = _today()
    cutoff = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT mv.*, fm.name as member_name
           FROM medical_visits mv
           JOIN family_members fm ON mv.member_id = fm.member_id
           WHERE mv.follow_up_date IS NOT NULL
             AND mv.follow_up_date >= ?
             AND mv.follow_up_date <= ?
           ORDER BY mv.follow_up_date""",
        (today, cutoff),
    ).fetchall()
    conn.close()
    return {"status": "ok", "upcoming": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Vaccinations
# ---------------------------------------------------------------------------

def log_vaccine(
    member_name: str,
    vaccine: str,
    date: str,
    provider: Optional[str] = None,
    lot: Optional[str] = None,
    next_due: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    conn.execute(
        """INSERT INTO vaccinations
           (member_id, vaccine_name, date_given, provider, lot_number, next_due, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (member["member_id"], vaccine, date, provider, lot, next_due, notes),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Logged {vaccine} vaccine for {member['name']}"}


def list_vaccines(member_name: str) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    rows = conn.execute(
        "SELECT * FROM vaccinations WHERE member_id = ? ORDER BY date_given DESC",
        (member["member_id"],),
    ).fetchall()
    conn.close()
    return {"status": "ok", "member": member["name"], "vaccinations": [dict(r) for r in rows]}


def due_vaccines() -> dict:
    conn = get_conn()
    today = _today()
    rows = conn.execute(
        """SELECT v.*, fm.name as member_name
           FROM vaccinations v
           JOIN family_members fm ON v.member_id = fm.member_id
           WHERE v.next_due IS NOT NULL AND v.next_due <= ?
           ORDER BY v.next_due""",
        (today,),
    ).fetchall()
    conn.close()
    return {"status": "ok", "due_vaccines": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------

def add_medication(
    member_name: str,
    med_name: str,
    dosage: Optional[str] = None,
    frequency: Optional[str] = None,
    prescriber: Optional[str] = None,
    pharmacy: Optional[str] = None,
    refill_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    conn.execute(
        """INSERT INTO medications
           (member_id, name, dosage, frequency, prescriber, pharmacy, refill_date, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (member["member_id"], med_name, dosage, frequency, prescriber, pharmacy, refill_date, notes),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Added medication '{med_name}' for {member['name']}"}


def list_medications(member_name: str, active_only: bool = False) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    query = "SELECT * FROM medications WHERE member_id = ?"
    params: list = [member["member_id"]]
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY name"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {"status": "ok", "member": member["name"], "medications": [dict(r) for r in rows]}


def update_medication(
    medication_id: int,
    refill_date: Optional[str] = None,
    active: Optional[int] = None,
    dosage: Optional[str] = None,
    frequency: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    conn = get_conn()
    # Check medication exists
    row = conn.execute(
        "SELECT * FROM medications WHERE medication_id = ?", (medication_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"error": f"No medication found with id {medication_id}"}

    updates = []
    params: list = []
    if refill_date is not None:
        updates.append("refill_date = ?")
        params.append(refill_date)
    if active is not None:
        updates.append("active = ?")
        params.append(active)
    if dosage is not None:
        updates.append("dosage = ?")
        params.append(dosage)
    if frequency is not None:
        updates.append("frequency = ?")
        params.append(frequency)
    if notes is not None:
        updates.append("notes = ?")
        params.append(notes)

    if not updates:
        conn.close()
        return {"error": "No fields to update"}

    params.append(medication_id)
    conn.execute(
        f"UPDATE medications SET {', '.join(updates)} WHERE medication_id = ?",
        params,
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Updated medication {medication_id}"}


def due_refills() -> dict:
    conn = get_conn()
    today = _today()
    cutoff = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT m.*, fm.name as member_name
           FROM medications m
           JOIN family_members fm ON m.member_id = fm.member_id
           WHERE m.active = 1
             AND m.refill_date IS NOT NULL
             AND m.refill_date <= ?
           ORDER BY m.refill_date""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return {"status": "ok", "due_refills": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Symptom log
# ---------------------------------------------------------------------------

def log_symptom(
    member_name: str,
    symptoms: str,
    severity: Optional[int] = None,
    temperature: Optional[float] = None,
    notes: Optional[str] = None,
) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    conn.execute(
        """INSERT INTO symptom_log
           (member_id, logged_at, symptoms, severity, temperature, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (member["member_id"], _now_utc(), symptoms, severity, temperature, notes),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Logged symptoms for {member['name']}"}


def list_symptoms(member_name: str, days: int = 30) -> dict:
    conn = get_conn()
    member = _require_member(conn, member_name)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    rows = conn.execute(
        "SELECT * FROM symptom_log WHERE member_id = ? AND logged_at >= ? ORDER BY logged_at DESC",
        (member["member_id"], cutoff),
    ).fetchall()
    conn.close()
    return {"status": "ok", "member": member["name"], "symptoms": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard(member_name: Optional[str] = None) -> dict:
    conn = get_conn()
    today = _today()
    now = _now_utc()
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    thirty_days = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    seven_days_refill = (datetime.now(timezone.utc) + timedelta(days=7)).strftime(
        "%Y-%m-%d"
    )

    member_filter = ""
    params_base: list = []
    if member_name:
        member = _resolve_member(conn, member_name)
        if not member:
            conn.close()
            return {"error": f"No member found matching '{member_name}'"}
        member_filter = " AND fm.member_id = ?"
        params_base = [member["member_id"]]

    # Upcoming visits (follow-ups in next 30 days)
    upcoming = conn.execute(
        f"""SELECT mv.*, fm.name as member_name
            FROM medical_visits mv
            JOIN family_members fm ON mv.member_id = fm.member_id
            WHERE mv.follow_up_date IS NOT NULL
              AND mv.follow_up_date >= ?
              AND mv.follow_up_date <= ?
              {member_filter}
            ORDER BY mv.follow_up_date""",
        [today, thirty_days] + params_base,
    ).fetchall()

    # Due vaccines
    vaccines = conn.execute(
        f"""SELECT v.*, fm.name as member_name
            FROM vaccinations v
            JOIN family_members fm ON v.member_id = fm.member_id
            WHERE v.next_due IS NOT NULL AND v.next_due <= ?
              {member_filter}
            ORDER BY v.next_due""",
        [today] + params_base,
    ).fetchall()

    # Due refills
    refills = conn.execute(
        f"""SELECT m.*, fm.name as member_name
            FROM medications m
            JOIN family_members fm ON m.member_id = fm.member_id
            WHERE m.active = 1
              AND m.refill_date IS NOT NULL
              AND m.refill_date <= ?
              {member_filter}
            ORDER BY m.refill_date""",
        [seven_days_refill] + params_base,
    ).fetchall()

    # Recent symptoms (last 7 days)
    symptoms = conn.execute(
        f"""SELECT sl.*, fm.name as member_name
            FROM symptom_log sl
            JOIN family_members fm ON sl.member_id = fm.member_id
            WHERE sl.logged_at >= ?
              {member_filter}
            ORDER BY sl.logged_at DESC""",
        [seven_days_ago] + params_base,
    ).fetchall()

    conn.close()
    return {
        "status": "ok",
        "upcoming_visits": [dict(r) for r in upcoming],
        "due_vaccines": [dict(r) for r in vaccines],
        "due_refills": [dict(r) for r in refills],
        "recent_symptoms": [dict(r) for r in symptoms],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Family health records manager")

    # -- Member actions (mutually exclusive) --
    member_group = p.add_mutually_exclusive_group()
    member_group.add_argument("--add-member", action="store_true")
    member_group.add_argument("--list-members", action="store_true")
    member_group.add_argument("--get-member", action="store_true")
    member_group.add_argument("--update-member", action="store_true")
    member_group.add_argument("--remove-member", action="store_true")

    # -- Visit actions --
    member_group.add_argument("--log-visit", action="store_true")
    member_group.add_argument("--list-visits", action="store_true")
    member_group.add_argument("--upcoming-visits", action="store_true")

    # -- Vaccination actions --
    member_group.add_argument("--log-vaccine", action="store_true")
    member_group.add_argument("--list-vaccines", action="store_true")
    member_group.add_argument("--due-vaccines", action="store_true")

    # -- Medication actions --
    member_group.add_argument("--add-medication", action="store_true")
    member_group.add_argument("--list-medications", action="store_true")
    member_group.add_argument("--update-medication", action="store_true")
    member_group.add_argument("--due-refills", action="store_true")

    # -- Symptom actions --
    member_group.add_argument("--log-symptom", action="store_true")
    member_group.add_argument("--list-symptoms", action="store_true")

    # -- Dashboard --
    member_group.add_argument("--dashboard", action="store_true")

    # -- Common options --
    p.add_argument("--name", type=str, help="Member name")
    p.add_argument("--member", type=str, help="Member name (for record operations)")
    p.add_argument("--dob", type=str, help="Date of birth (YYYY-MM-DD)")
    p.add_argument("--blood-type", type=str)
    p.add_argument("--allergies", type=str)
    p.add_argument("--doctor", type=str)
    p.add_argument("--insurance", type=str)
    p.add_argument("--notes", type=str)

    # Visit options
    p.add_argument("--date", type=str, help="Visit date (YYYY-MM-DD)")
    p.add_argument("--provider", type=str)
    p.add_argument("--type", type=str, dest="visit_type", help="Visit type")
    p.add_argument("--diagnosis", type=str)
    p.add_argument("--treatment", type=str)
    p.add_argument("--follow-up", type=str, help="Follow-up date (YYYY-MM-DD)")
    p.add_argument("--year", type=int)

    # Vaccine options
    p.add_argument("--vaccine", type=str)
    p.add_argument("--lot", type=str)
    p.add_argument("--next-due", type=str)

    # Medication options
    p.add_argument("--dosage", type=str)
    p.add_argument("--frequency", type=str)
    p.add_argument("--prescriber", type=str)
    p.add_argument("--pharmacy", type=str)
    p.add_argument("--refill-date", type=str)
    p.add_argument("--active", type=int, choices=[0, 1])
    p.add_argument("--medication-id", type=int)

    # Symptom options
    p.add_argument("--symptoms", type=str)
    p.add_argument("--severity", type=int)
    p.add_argument("--temperature", type=float)

    # List options
    p.add_argument("--days", type=int, default=30)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # -- Members --
    if args.add_member:
        if not args.name:
            _err("--name is required for --add-member")
        result = add_member(
            name=args.name,
            dob=args.dob,
            blood_type=args.blood_type,
            allergies=args.allergies,
            doctor=args.doctor,
            insurance=args.insurance,
            notes=args.notes,
        )

    elif args.list_members:
        result = list_members()

    elif args.get_member:
        if not args.name:
            _err("--name is required for --get-member")
        result = get_member(args.name)

    elif args.update_member:
        if not args.name:
            _err("--name is required for --update-member")
        result = update_member(
            name=args.name,
            allergies=args.allergies,
            doctor=args.doctor,
            insurance=args.insurance,
            notes=args.notes,
            dob=args.dob,
            blood_type=args.blood_type,
        )

    elif args.remove_member:
        if not args.name:
            _err("--name is required for --remove-member")
        result = remove_member(args.name)

    # -- Visits --
    elif args.log_visit:
        if not args.member or not args.date or not args.provider:
            _err("--member, --date, and --provider are required for --log-visit")
        result = log_visit(
            member_name=args.member,
            date=args.date,
            provider=args.provider,
            visit_type=args.visit_type,
            diagnosis=args.diagnosis,
            treatment=args.treatment,
            follow_up=args.follow_up,
            notes=args.notes,
        )

    elif args.list_visits:
        if not args.member:
            _err("--member is required for --list-visits")
        result = list_visits(args.member, year=args.year, visit_type=args.visit_type)

    elif args.upcoming_visits:
        result = upcoming_visits()

    # -- Vaccinations --
    elif args.log_vaccine:
        if not args.member or not args.vaccine or not args.date:
            _err("--member, --vaccine, and --date are required for --log-vaccine")
        result = log_vaccine(
            member_name=args.member,
            vaccine=args.vaccine,
            date=args.date,
            provider=args.provider,
            lot=args.lot,
            next_due=args.next_due,
            notes=args.notes,
        )

    elif args.list_vaccines:
        if not args.member:
            _err("--member is required for --list-vaccines")
        result = list_vaccines(args.member)

    elif args.due_vaccines:
        result = due_vaccines()

    # -- Medications --
    elif args.add_medication:
        if not args.member or not args.name:
            _err("--member and --name are required for --add-medication")
        result = add_medication(
            member_name=args.member,
            med_name=args.name,
            dosage=args.dosage,
            frequency=args.frequency,
            prescriber=args.prescriber,
            pharmacy=args.pharmacy,
            refill_date=args.refill_date,
            notes=args.notes,
        )

    elif args.list_medications:
        if not args.member:
            _err("--member is required for --list-medications")
        result = list_medications(
            args.member, active_only=args.active == 1 if args.active is not None else False
        )

    elif args.update_medication:
        if args.medication_id is None:
            _err("--medication-id is required for --update-medication")
        result = update_medication(
            medication_id=args.medication_id,
            refill_date=args.refill_date,
            active=args.active,
            dosage=args.dosage,
            frequency=args.frequency,
            notes=args.notes,
        )

    elif args.due_refills:
        result = due_refills()

    # -- Symptoms --
    elif args.log_symptom:
        if not args.member or not args.symptoms:
            _err("--member and --symptoms are required for --log-symptom")
        result = log_symptom(
            member_name=args.member,
            symptoms=args.symptoms,
            severity=args.severity,
            temperature=args.temperature,
            notes=args.notes,
        )

    elif args.list_symptoms:
        if not args.member:
            _err("--member is required for --list-symptoms")
        result = list_symptoms(args.member, days=args.days)

    # -- Dashboard --
    elif args.dashboard:
        result = dashboard(member_name=args.member)

    else:
        _err("No action specified. Use --help to see available actions.")
        return

    if "error" in result:
        _err(result["error"])
    else:
        _ok(result)


if __name__ == "__main__":
    main()

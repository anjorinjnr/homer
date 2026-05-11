#!/usr/bin/env python3
"""onboarding.py — Track and drive new-user onboarding for Homer.

Phases: cold_start → progressive → complete (or declined_global).

The skill layer decides *what to say*; this tool decides *what to ask next*,
records answers, writes household.md, and manages the daily heartbeat nudge.

All output is JSON on stdout. Non-zero exit on error.

DB location: state/onboarding.db inside the nanobot workspace, or
HOMER_ONBOARDING_DB env var.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from tools import context_updater as cu
    from tools.google_auth import has_google_token
    from tools.onboarding_fields import (
        EMPTY_MARKERS,
        FIELDS,
        HOUSEHOLD_TEMPLATE,
        FieldDef,
        field_by_key,
    )
    from tools.switch_model import _discover_tier
except ImportError:  # when tools/ is on sys.path directly (sim harness)
    import context_updater as cu  # type: ignore
    from google_auth import has_google_token  # type: ignore
    from onboarding_fields import (  # type: ignore
        EMPTY_MARKERS,
        FIELDS,
        HOUSEHOLD_TEMPLATE,
        FieldDef,
        field_by_key,
    )
    from switch_model import _discover_tier  # type: ignore

REPO_ROOT = Path(__file__).parent.parent.resolve()

PHASE_COLD = "cold_start"
PHASE_PROGRESSIVE = "progressive"
PHASE_COMPLETE = "complete"
PHASE_DECLINED = "declined_global"

STATUS_UNKNOWN = "unknown"
STATUS_ASKED = "asked"
STATUS_ANSWERED = "answered"
STATUS_DECLINED = "declined"
STATUS_DEFERRED = "deferred"
TERMINAL_STATUSES = {STATUS_ANSWERED, STATUS_DECLINED}
TERMINAL_PHASES = {PHASE_COMPLETE, PHASE_DECLINED}

SOURCE_IMPORTED = "imported"
SOURCE_ELICITED = "elicited"
SOURCE_INFERRED = "inferred"

NUDGE_COOLDOWN_HOURS = 24
HEARTBEAT_TASK_DESC = "Onboarding gap nudge"

# Setup checklist priority order: asked before any Tier 1/2/3 field nudge.
SETUP_WORKSPACE = "workspace"
SETUP_CONTEXT_IMPORT = "context_import"
SETUP_BYOK = "byok"
SETUP_ITEMS = [SETUP_WORKSPACE, SETUP_CONTEXT_IMPORT, SETUP_BYOK]

SETUP_STATUS_UNKNOWN = "unknown"
SETUP_STATUS_ASKED = "asked"
SETUP_STATUS_DONE = "done"
SETUP_STATUS_DECLINED = "declined"
SETUP_TERMINAL = {SETUP_STATUS_DONE, SETUP_STATUS_DECLINED}

DETECTION_MISSING = "missing"
DETECTION_OK = {
    SETUP_WORKSPACE: "connected",
    SETUP_CONTEXT_IMPORT: "done",
    SETUP_BYOK: "active",
}

BYOK_ENV_VARS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")


# ---------------------------------------------------------------------------
# DB path + connection
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    if env := os.environ.get("HOMER_ONBOARDING_DB"):
        return Path(env)
    if workspace := os.environ.get("HOMER_WORKSPACE"):
        return Path(workspace) / "state" / "onboarding.db"
    return REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "onboarding.db"


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS onboarding_fields (
            field_key       TEXT PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'unknown',
            last_asked_at   TEXT,
            asked_count     INTEGER NOT NULL DEFAULT 0,
            source          TEXT,
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS onboarding_meta (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            phase               TEXT NOT NULL DEFAULT 'cold_start',
            started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            completed_at        TEXT,
            last_nudge_at       TEXT,
            queued_field_key    TEXT
        );

        CREATE TABLE IF NOT EXISTS onboarding_setup (
            item            TEXT PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'unknown',
            last_asked_at   TEXT,
            asked_count     INTEGER NOT NULL DEFAULT 0,
            notes           TEXT
        );
    """)
    for f in FIELDS:
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_fields (field_key, status) VALUES (?, ?)",
            (f.key, STATUS_UNKNOWN),
        )
    for item in SETUP_ITEMS:
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_setup (item, status) VALUES (?, ?)",
            (item, SETUP_STATUS_UNKNOWN),
        )
    conn.execute("INSERT OR IGNORE INTO onboarding_meta (id) VALUES (1)")
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ok(data: dict) -> None:
    print(json.dumps(data, indent=2))


def _err(msg: str, code: int = 1) -> None:
    print(json.dumps({"error": msg}))
    sys.exit(code)


def _get_meta(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM onboarding_meta WHERE id = 1").fetchone()


def _get_field_row(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM onboarding_fields WHERE field_key = ?", (key,)
    ).fetchone()


def _clear_queued(
    conn: sqlite3.Connection, only_if_key: Optional[str] = None
) -> None:
    """Clear onboarding_meta.queued_field_key. If `only_if_key` is given, only
    clear when the queued pointer currently matches that field."""
    if only_if_key is None:
        conn.execute("UPDATE onboarding_meta SET queued_field_key = NULL WHERE id = 1")
    else:
        conn.execute(
            "UPDATE onboarding_meta SET queued_field_key = NULL "
            "WHERE id = 1 AND queued_field_key = ?",
            (only_if_key,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# household.md reading + gap analysis
# ---------------------------------------------------------------------------

def _household_read_path() -> Path:
    return cu.get_context_file("household")


def _household_write_path() -> Path:
    return cu.get_context_file("household", for_write=True)


def _read_household() -> str:
    path = _household_read_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


_H2_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")


def _parse_sections(content: str) -> dict[str, str]:
    """Parse a markdown document into {section_title_lower: body_text}.

    Single linear pass over the lines — O(lines), not O(fields × lines).
    """
    sections: dict[str, str] = {}
    current: Optional[str] = None
    buf: list[str] = []
    for line in content.splitlines():
        m = _H2_HEADER_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _extract_section_body(content: str, section: str) -> str:
    """Return the body text of '## Section' (case-insensitive)."""
    return _parse_sections(content).get(section.lower(), "")


def _scalar_value(section_body: str, key: str) -> str:
    """Extract value for a '- **Key**: value' or '- Key: value' line."""
    for line in section_body.splitlines():
        m = re.match(
            rf"^\s*-\s*(?:\*\*)?{re.escape(key)}(?:\*\*)?\s*:\s*(.*)$",
            line, re.IGNORECASE,
        )
        if m:
            value = m.group(1).strip()
            if re.match(r"^\[FILL:.*\]$", value):
                return ""
            return value
    return ""


def _section_is_filled(body: str, field: FieldDef) -> bool:
    if field.scalar:
        return bool(_scalar_value(body, field.field))
    if not body:
        return False
    return body.strip().lower() not in {m.lower() for m in EMPTY_MARKERS}


def is_field_filled(content: str, field: FieldDef) -> bool:
    return _section_is_filled(_extract_section_body(content, field.section), field)


def gap_list(conn: sqlite3.Connection) -> list[dict]:
    """Return fields whose status is not terminal, in priority order.

    Priority: lower tier first; within tier, lower asked_count first,
    then alphabetical by key.
    """
    rows = {r["field_key"]: r for r in conn.execute(
        "SELECT * FROM onboarding_fields").fetchall()}
    out = []
    for f in FIELDS:
        row = rows.get(f.key)
        if row is None:
            continue
        if row["status"] in TERMINAL_STATUSES:
            continue
        out.append({
            "key": f.key,
            "tier": f.tier,
            "status": row["status"],
            "asked_count": row["asked_count"],
            "phrasing": f.phrasing,
            "section": f.section,
        })
    out.sort(key=lambda d: (d["tier"], d["asked_count"], d["key"]))
    return out


def tier_complete(conn: sqlite3.Connection, tier: int) -> bool:
    """True when every field in `tier` has a terminal status."""
    tier_keys = [f.key for f in FIELDS if f.tier == tier]
    if not tier_keys:
        return True
    placeholders = ",".join(["?"] * len(tier_keys))
    rows = conn.execute(
        f"SELECT status FROM onboarding_fields WHERE field_key IN ({placeholders})",
        tier_keys,
    ).fetchall()
    return all(r["status"] in TERMINAL_STATUSES for r in rows)


def all_fields_terminal(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM onboarding_fields WHERE status NOT IN (?, ?)",
        (STATUS_ANSWERED, STATUS_DECLINED),
    ).fetchone()
    return row["c"] == 0


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------

def _set_phase(conn: sqlite3.Connection, phase: str) -> None:
    if phase in TERMINAL_PHASES:
        conn.execute(
            "UPDATE onboarding_meta SET phase = ?, completed_at = ? WHERE id = 1",
            (phase, _now_utc()),
        )
    else:
        conn.execute(
            "UPDATE onboarding_meta SET phase = ?, completed_at = NULL WHERE id = 1",
            (phase,),
        )
    conn.commit()


def _maybe_advance_phase(conn: sqlite3.Connection) -> str:
    """Auto-promote phase when exit conditions are met. Returns current phase."""
    meta = _get_meta(conn)
    phase = meta["phase"]
    if phase == PHASE_DECLINED:
        return phase
    if phase == PHASE_COLD and tier_complete(conn, 1):
        _set_phase(conn, PHASE_PROGRESSIVE)
        phase = PHASE_PROGRESSIVE
    if phase == PHASE_PROGRESSIVE and all_fields_terminal(conn):
        _set_phase(conn, PHASE_COMPLETE)
        phase = PHASE_COMPLETE
    return phase


# ---------------------------------------------------------------------------
# Setup checklist — workspace / context_import / byok
# ---------------------------------------------------------------------------

def _detect_workspace() -> str:
    try:
        connected = has_google_token("primary")
    except Exception:
        return DETECTION_MISSING
    return DETECTION_OK[SETUP_WORKSPACE] if connected else DETECTION_MISSING


def _detect_context(conn: sqlite3.Connection) -> str:
    return DETECTION_OK[SETUP_CONTEXT_IMPORT] if tier_complete(conn, 1) else DETECTION_MISSING


def _resolve_active_model() -> str:
    """CURRENT_MODEL file (per-tenant or repo workspace) → ~/.nanobot/config.json
    → HOMER_DEFAULT_MODEL env."""
    workspace = os.environ.get("HOMER_WORKSPACE")
    candidates = []
    if workspace:
        candidates.append(Path(workspace) / "CURRENT_MODEL")
    candidates.append(REPO_ROOT / "context" / ".nanobot_workspace" / "CURRENT_MODEL")
    for cm in candidates:
        try:
            val = cm.read_text().strip()
        except OSError:
            continue
        if val:
            return val
    cfg_path = Path.home() / ".nanobot" / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
        val = cfg.get("agents", {}).get("defaults", {}).get("model", "")
        if val:
            return val
    except (OSError, ValueError):
        pass
    return os.environ.get("HOMER_DEFAULT_MODEL", "unknown")


def _detect_model_tier() -> str:
    tier, _ = _discover_tier()
    return tier


def _detect_byok() -> str:
    # Independent of _discover_tier's OpenRouter precedence: any user-supplied
    # key is enough to silence the BYOK nudge, even on managed-tier containers.
    present = any(os.environ.get(k) for k in BYOK_ENV_VARS)
    return DETECTION_OK[SETUP_BYOK] if present else DETECTION_MISSING


def _detect_setup(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        SETUP_WORKSPACE: _detect_workspace(),
        SETUP_CONTEXT_IMPORT: _detect_context(conn),
        SETUP_BYOK: _detect_byok(),
    }


def _setup_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["item"]: r for r in conn.execute(
        "SELECT * FROM onboarding_setup").fetchall()}


def _sync_setup_detections(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, sqlite3.Row]]:
    """Reconcile detected state with stored status. Auto-flip unknown/asked
    rows to 'done' when detection succeeds. Never overrides 'declined'."""
    detected = _detect_setup(conn)
    rows = _setup_rows(conn)
    flipped = []
    for item in SETUP_ITEMS:
        row = rows.get(item)
        if row is None or row["status"] in SETUP_TERMINAL:
            continue
        if detected.get(item) == DETECTION_OK[item]:
            conn.execute(
                "UPDATE onboarding_setup SET status = ? WHERE item = ?",
                (SETUP_STATUS_DONE, item),
            )
            flipped.append(item)
    if flipped:
        conn.commit()
        rows = _setup_rows(conn)
    return detected, rows


def _next_setup(
    detected: dict[str, str], rows: dict[str, sqlite3.Row]
) -> Optional[str]:
    """Highest-priority setup item still pending and outside its cooldown."""
    now = datetime.now(timezone.utc)
    for item in SETUP_ITEMS:
        row = rows.get(item)
        if row is None or row["status"] in SETUP_TERMINAL:
            continue
        if detected.get(item) == DETECTION_OK[item]:
            continue
        last = _parse_utc(row["last_asked_at"]) if row["last_asked_at"] else None
        if last and now < last + timedelta(hours=NUDGE_COOLDOWN_HOURS):
            continue
        return item
    return None


# ---------------------------------------------------------------------------
# Sync: reconcile DB with household.md
# ---------------------------------------------------------------------------

def sync_from_household(conn: sqlite3.Connection, source: str) -> dict:
    """Mark any field whose section is filled in household.md as answered.

    Used on init (source=inferred), on import (source=imported), and on
    every invocation (hand-edit tolerance — source=inferred).
    """
    sections = _parse_sections(_read_household())
    updated = []
    for f in FIELDS:
        row = _get_field_row(conn, f.key)
        if row is None or row["status"] in TERMINAL_STATUSES:
            continue
        body = sections.get(f.section.lower(), "")
        if _section_is_filled(body, f):
            conn.execute(
                """UPDATE onboarding_fields
                      SET status = ?, source = COALESCE(source, ?)
                    WHERE field_key = ?""",
                (STATUS_ANSWERED, source, f.key),
            )
            updated.append(f.key)
    conn.commit()
    return {"updated": updated, "count": len(updated)}


# ---------------------------------------------------------------------------
# household.md writing
# ---------------------------------------------------------------------------

def _ensure_household_template() -> None:
    """Create a canonical household.md if none exists."""
    read_path = _household_read_path()
    if read_path.exists() and read_path.read_text(encoding="utf-8").strip():
        return
    write_path = _household_write_path()
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(HOUSEHOLD_TEMPLATE, encoding="utf-8")


def _replace_section_body(content: str, section: str, new_body: str) -> str:
    """Replace the body of '## Section' with `new_body`.

    If the section doesn't exist, appends it at the end of the document.
    """
    lines = content.splitlines()
    pattern = rf"^##\s+{re.escape(section)}\s*$"
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line, re.IGNORECASE):
            start = i
            break
    if start is None:
        tail = "" if content.endswith("\n") else "\n"
        return content + f"{tail}\n## {section}\n{new_body}\n"
    body_start = start + 1
    body_end = len(lines)
    for j in range(body_start, len(lines)):
        if re.match(r"^##\s+", lines[j]):
            body_end = j
            break
    new_lines = lines[:body_start] + new_body.splitlines() + [""] + lines[body_end:]
    return "\n".join(new_lines) + ("\n" if content.endswith("\n") else "")


def _write_household_value(field: FieldDef, value: str) -> None:
    """Write a user-supplied value into household.md under the field's section."""
    content = _read_household()
    if not content.strip():
        content = HOUSEHOLD_TEMPLATE
    if field.scalar:
        content = cu.update_key_value(
            content, field.section, None, field.field, value
        )
    else:
        content = _replace_section_body(content, field.section, value.strip())
    content = cu.update_timestamp(content)
    write_path = _household_write_path()
    write_path.parent.mkdir(parents=True, exist_ok=True)
    cu.write_context(write_path, content)
    _maybe_rebuild_memory()


def _maybe_rebuild_memory() -> None:
    if os.environ.get("HOMER_ONBOARDING_SKIP_REBUILD") == "1":
        return
    try:
        cu.rebuild_memory()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # best-effort — tests or constrained envs may not have build_context
        pass


# ---------------------------------------------------------------------------
# Heartbeat integration
# ---------------------------------------------------------------------------

def _heartbeat_path() -> Path:
    if workspace := os.environ.get("HOMER_WORKSPACE"):
        return Path(workspace) / "HEARTBEAT.md"
    return REPO_ROOT / "context" / ".nanobot_workspace" / "HEARTBEAT.md"


def _register_heartbeat() -> dict:
    """Add the daily onboarding nudge task to HEARTBEAT.md. Idempotent:
    re-reads HEARTBEAT.md every time and skips if the task is already there.
    """
    hb_path = _heartbeat_path()
    if not hb_path.exists():
        return {"registered": False, "reason": "heartbeat_missing"}
    if HEARTBEAT_TASK_DESC in hb_path.read_text(encoding="utf-8"):
        return {"registered": False, "reason": "already_in_heartbeat"}

    tasks_update = REPO_ROOT / "tools" / "tasks_update.py"
    schedule = datetime.now(timezone.utc).strftime("%Y-%m-%d 09:00")
    try:
        subprocess.run(
            [
                sys.executable, str(tasks_update),
                "--add",
                "--desc", HEARTBEAT_TASK_DESC,
                "--schedule", schedule,
                "--recur", "every 1 day",
                "--recipients", "primary:whatsapp",
                "--type", "agentic",
                "--goal",
                "Run onboarding queue-next to stash the next gap question. "
                "Do not message the user — the question is appended on their next reply.",
            ],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return {"registered": False, "reason": f"tasks_update_failed: {e}"}
    return {"registered": True, "schedule": schedule}


def _remove_heartbeat() -> None:
    tasks_update = REPO_ROOT / "tools" / "tasks_update.py"
    if not _heartbeat_path().exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(tasks_update), "--remove", HEARTBEAT_TASK_DESC],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(register_heartbeat: bool = True) -> dict:
    with get_conn() as conn:
        _ensure_household_template()
        sync_from_household(conn, source=SOURCE_INFERRED)
        _maybe_advance_phase(conn)
        hb_result = {"registered": False, "reason": "skipped"}
        if register_heartbeat:
            hb_result = _register_heartbeat()
        meta = _get_meta(conn)
        return {
            "status": "initialized",
            "phase": meta["phase"],
            "db_path": str(get_db_path()),
            "household_path": str(_household_write_path()),
            "heartbeat": hb_result,
        }


def cmd_status() -> dict:
    with get_conn() as conn:
        _maybe_advance_phase(conn)
        meta = _get_meta(conn)
        counts = {s: 0 for s in [STATUS_UNKNOWN, STATUS_ASKED, STATUS_ANSWERED,
                                  STATUS_DECLINED, STATUS_DEFERRED]}
        for row in conn.execute("SELECT status FROM onboarding_fields").fetchall():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        gaps = gap_list(conn)
        next_gap = gaps[0] if gaps else None
        detected, rows = _sync_setup_detections(conn)
        setup = {}
        for item in SETUP_ITEMS:
            row = rows.get(item)
            setup[item] = {
                "detected": detected.get(item, DETECTION_MISSING),
                "status": row["status"] if row else SETUP_STATUS_UNKNOWN,
                "asked_count": row["asked_count"] if row else 0,
                "last_asked_at": row["last_asked_at"] if row else None,
            }
        return {
            "phase": meta["phase"],
            "started_at": meta["started_at"],
            "completed_at": meta["completed_at"],
            "last_nudge_at": meta["last_nudge_at"],
            "queued_field_key": meta["queued_field_key"],
            "counts": counts,
            "total_fields": len(FIELDS),
            "next_gap": next_gap,
            "setup": setup,
            "next_setup": _next_setup(detected, rows),
            "current_model": _resolve_active_model(),
            "model_tier": _detect_model_tier(),
            "suppressed": meta["phase"] in TERMINAL_PHASES,
        }


def cmd_gap(tier: Optional[int] = None) -> dict:
    with get_conn() as conn:
        _maybe_advance_phase(conn)
        gaps = gap_list(conn)
        if tier is not None:
            gaps = [g for g in gaps if g["tier"] == tier]
        return {"gaps": gaps, "count": len(gaps)}


def cmd_sync(source: str = SOURCE_INFERRED) -> dict:
    with get_conn() as conn:
        result = sync_from_household(conn, source=source)
        phase = _maybe_advance_phase(conn)
        result["phase"] = phase
        return result


def cmd_parse_import() -> dict:
    """After user pastes an export into household.md, mark filled fields imported."""
    with get_conn() as conn:
        result = sync_from_household(conn, source=SOURCE_IMPORTED)
        phase = _maybe_advance_phase(conn)
        result["phase"] = phase
        return result


def _sanitize_value(value: str) -> str:
    """Reject values that would break the household.md structure.

    Rejects H1/H2 headers anywhere in the input so an answer can never inject
    a new section (and no phantom `## Primary user` can shadow the real one).
    """
    if re.search(r"(^|\n)\s*#{1,2}\s", value):
        _err("value may not contain markdown headers (lines starting with # or ##)")
    return value.strip()


def cmd_answer(field_key: str, value: str, source: str = SOURCE_ELICITED) -> dict:
    field = field_by_key(field_key)
    if field is None:
        _err(f"unknown field_key '{field_key}'")
    if not value or not value.strip():
        _err("value cannot be empty; use 'decline' instead")
    value = _sanitize_value(value)
    with get_conn() as conn:
        _write_household_value(field, value)
        conn.execute(
            """UPDATE onboarding_fields
                  SET status = ?, source = ?, last_asked_at = ?
                WHERE field_key = ?""",
            (STATUS_ANSWERED, source, _now_utc(), field_key),
        )
        _clear_queued(conn, only_if_key=field_key)
        phase = _maybe_advance_phase(conn)
        return {
            "status": "answered",
            "field_key": field_key,
            "phase": phase,
            "source": source,
        }


def cmd_decline(field_key: str, note: Optional[str] = None) -> dict:
    field = field_by_key(field_key)
    if field is None:
        _err(f"unknown field_key '{field_key}'")
    with get_conn() as conn:
        conn.execute(
            """UPDATE onboarding_fields
                  SET status = ?, notes = COALESCE(?, notes), last_asked_at = ?
                WHERE field_key = ?""",
            (STATUS_DECLINED, note, _now_utc(), field_key),
        )
        _clear_queued(conn, only_if_key=field_key)
        phase = _maybe_advance_phase(conn)
        return {"status": "declined", "field_key": field_key, "phase": phase}


def cmd_queue_next() -> dict:
    """Pick the next gap and stash it in onboarding_meta.queued_field_key.

    Respects the 24h frequency cap — if a question was asked inside the
    cooldown window, does nothing.
    """
    with get_conn() as conn:
        phase = _maybe_advance_phase(conn)
        if phase in TERMINAL_PHASES:
            return {"queued": None, "reason": "terminal_phase", "phase": phase}
        if phase == PHASE_COLD:
            # During cold start the skill batches its own Tier-1 asks; queuing
            # now would just burn the 24h cooldown and delay the first real
            # progressive nudge after the user consents.
            return {"queued": None, "reason": "cold_start", "phase": phase}
        meta = _get_meta(conn)
        if meta["queued_field_key"]:
            return {"queued": meta["queued_field_key"], "reason": "already_queued"}
        last = _parse_utc(meta["last_nudge_at"]) if meta["last_nudge_at"] else None
        if last:
            next_allowed = last + timedelta(hours=NUDGE_COOLDOWN_HOURS)
            if datetime.now(timezone.utc) < next_allowed:
                return {
                    "queued": None,
                    "reason": "cooldown",
                    "next_allowed": next_allowed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
        gaps = gap_list(conn)
        if not gaps:
            return {"queued": None, "reason": "no_gaps", "phase": phase}
        pick = gaps[0]
        conn.execute(
            "UPDATE onboarding_meta SET queued_field_key = ?, last_nudge_at = ? WHERE id = 1",
            (pick["key"], _now_utc()),
        )
        conn.commit()
        return {"queued": pick["key"], "phrasing": pick["phrasing"], "tier": pick["tier"]}


def cmd_consume_queued() -> dict:
    """Return (and clear) the queued field. Marks it 'asked' + increments asked_count."""
    with get_conn() as conn:
        meta = _get_meta(conn)
        key = meta["queued_field_key"]
        if not key:
            return {"field_key": None}
        field = field_by_key(key)
        if field is None:
            _clear_queued(conn)
            return {"field_key": None, "reason": "stale"}
        conn.execute(
            """UPDATE onboarding_fields
                  SET status = ?, last_asked_at = ?, asked_count = asked_count + 1
                WHERE field_key = ? AND status NOT IN (?, ?)""",
            (STATUS_ASKED, _now_utc(), key, STATUS_ANSWERED, STATUS_DECLINED),
        )
        _clear_queued(conn)
        return {
            "field_key": key,
            "phrasing": field.phrasing,
            "tier": field.tier,
            "section": field.section,
        }


def cmd_set_phase(phase: str) -> dict:
    if phase not in {PHASE_COLD, PHASE_PROGRESSIVE, PHASE_COMPLETE, PHASE_DECLINED}:
        _err(f"invalid phase '{phase}'")
    with get_conn() as conn:
        _set_phase(conn, phase)
        if phase in TERMINAL_PHASES:
            _remove_heartbeat()
        return {"status": "phase_set", "phase": phase}


def cmd_reset(empty_household: bool = False) -> dict:
    """Wipe onboarding state back to a fresh cold_start.

    Used by the portal when provisioning a new household and by simulation
    setup to put Homer back in a brand-new state. Does NOT delete the DB
    file — just resets every row to its default.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE onboarding_fields SET status = ?, source = NULL, "
            "last_asked_at = NULL, asked_count = 0, notes = NULL",
            (STATUS_UNKNOWN,),
        )
        conn.execute(
            "UPDATE onboarding_setup SET status = ?, last_asked_at = NULL, "
            "asked_count = 0, notes = NULL",
            (SETUP_STATUS_UNKNOWN,),
        )
        conn.execute(
            """UPDATE onboarding_meta
                  SET phase = ?, started_at = ?, completed_at = NULL,
                      last_nudge_at = NULL, queued_field_key = NULL
                WHERE id = 1""",
            (PHASE_COLD, _now_utc()),
        )
        conn.commit()
    if empty_household:
        write_path = _household_write_path()
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(HOUSEHOLD_TEMPLATE, encoding="utf-8")
    return {"status": "reset", "phase": PHASE_COLD,
            "household_reset": empty_household}


def cmd_global_decline() -> dict:
    with get_conn() as conn:
        _set_phase(conn, PHASE_DECLINED)
        _remove_heartbeat()
        _clear_queued(conn)
        return {"status": "declined_global"}


def cmd_setup_mark(item: str, status: str, note: Optional[str] = None) -> dict:
    if item not in SETUP_ITEMS:
        _err(f"unknown setup item '{item}'")
    if status not in {SETUP_STATUS_ASKED, SETUP_STATUS_DONE, SETUP_STATUS_DECLINED}:
        _err(f"invalid status '{status}' (use asked|done|declined)")
    with get_conn() as conn:
        if status == SETUP_STATUS_ASKED:
            conn.execute(
                """UPDATE onboarding_setup
                      SET status = ?, last_asked_at = ?,
                          asked_count = asked_count + 1,
                          notes = COALESCE(?, notes)
                    WHERE item = ?""",
                (status, _now_utc(), note, item),
            )
        else:
            conn.execute(
                """UPDATE onboarding_setup
                      SET status = ?, notes = COALESCE(?, notes)
                    WHERE item = ?""",
                (status, note, item),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM onboarding_setup WHERE item = ?", (item,)
        ).fetchone()
        return {
            "item": item,
            "status": row["status"],
            "asked_count": row["asked_count"],
            "last_asked_at": row["last_asked_at"],
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Homer onboarding state machine.")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Create DB, template household.md, register heartbeat")
    init.add_argument("--no-heartbeat", action="store_true",
                      help="Skip HEARTBEAT.md task registration")

    sub.add_parser("status", help="Show current phase and gap summary")

    gap = sub.add_parser("gap", help="List unfilled fields in priority order")
    gap.add_argument("--tier", type=int, choices=[1, 2, 3], default=None)

    sync = sub.add_parser("sync", help="Reconcile DB with household.md")
    sync.add_argument("--source", default=SOURCE_INFERRED,
                      choices=[SOURCE_INFERRED, SOURCE_IMPORTED, SOURCE_ELICITED])

    sub.add_parser("parse-import",
                   help="Scan household.md after paste, mark filled fields imported")

    answer = sub.add_parser("answer", help="Record an answer for a field")
    answer.add_argument("--field-key", required=True)
    answer.add_argument("--value", required=True)
    answer.add_argument("--source", default=SOURCE_ELICITED,
                        choices=[SOURCE_ELICITED, SOURCE_IMPORTED, SOURCE_INFERRED])

    decline = sub.add_parser("decline", help="Mark a field declined")
    decline.add_argument("--field-key", required=True)
    decline.add_argument("--note", default=None)

    sub.add_parser("queue-next", help="Stash next gap question for the next reply")
    sub.add_parser("consume-queued", help="Return + clear the queued field")

    sp = sub.add_parser("set-phase", help="Force phase (admin override)")
    sp.add_argument("phase", choices=[PHASE_COLD, PHASE_PROGRESSIVE,
                                      PHASE_COMPLETE, PHASE_DECLINED])

    sub.add_parser("global-decline",
                   help="User opted out of all onboarding questions")

    setup_mark = sub.add_parser(
        "setup-mark",
        help="Update a setup-checklist item (workspace/context_import/byok)")
    setup_mark.add_argument("--item", required=True, choices=SETUP_ITEMS)
    setup_mark.add_argument(
        "--status", required=True,
        choices=[SETUP_STATUS_ASKED, SETUP_STATUS_DONE, SETUP_STATUS_DECLINED])
    setup_mark.add_argument("--note", default=None)

    reset = sub.add_parser("reset",
                           help="Wipe state back to cold_start (dev/provisioning only)")
    reset.add_argument("--empty-household", action="store_true",
                       help="Also overwrite household.md with the blank template")
    return p


def main(argv: Optional[list[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    cmd = args.cmd
    if cmd == "init":
        result = cmd_init(register_heartbeat=not args.no_heartbeat)
    elif cmd == "status":
        result = cmd_status()
    elif cmd == "gap":
        result = cmd_gap(tier=args.tier)
    elif cmd == "sync":
        result = cmd_sync(source=args.source)
    elif cmd == "parse-import":
        result = cmd_parse_import()
    elif cmd == "answer":
        result = cmd_answer(args.field_key, args.value, source=args.source)
    elif cmd == "decline":
        result = cmd_decline(args.field_key, note=args.note)
    elif cmd == "queue-next":
        result = cmd_queue_next()
    elif cmd == "consume-queued":
        result = cmd_consume_queued()
    elif cmd == "set-phase":
        result = cmd_set_phase(args.phase)
    elif cmd == "global-decline":
        result = cmd_global_decline()
    elif cmd == "setup-mark":
        result = cmd_setup_mark(args.item, args.status, note=args.note)
    elif cmd == "reset":
        result = cmd_reset(empty_household=args.empty_household)
    else:
        _err(f"unknown command '{cmd}'")
        return
    _ok(result)


if __name__ == "__main__":
    main()

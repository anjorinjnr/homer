#!/usr/bin/env python3
"""
tasks_update.py — Manage user tasks in HEARTBEAT.md.

Safely adds, completes, removes, and ticks tasks in the ## User Tasks section.
Never touches ## Active Tasks (system tasks) or ## Completed header/content above User Tasks.

Usage (via Homer exec tool):
    python tools/tasks_update.py --add --desc "Call HVAC" --schedule "daily at 9am" --until "2026-03-15"
    python tools/tasks_update.py --add --desc "File taxes" --schedule "2026-04-01"
    python tools/tasks_update.py --complete "HVAC"        # move matching task to Completed
    python tools/tasks_update.py --remove "HVAC"          # delete task entirely
    python tools/tasks_update.py --tick "HVAC"            # advance next-run date by recurrence
    python tools/tasks_update.py --list                   # print current user tasks as JSON
    python tools/tasks_update.py --edit "HVAC" --desc "New name" --schedule "2026-05-01"  # edit task fields

Task format in HEARTBEAT.md:
### [description]
Type: [type]            (optional: "agentic" for tool-use tasks)
Schedule: [schedule]
Recur: [recur]          (optional)
Until: [until]          (optional)
Recipients: [id:channel,...]
Goal: [goal]            (optional: detailed instructions for agentic tasks)
Added: [date]
"""

import argparse
import base64
import json
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from heartbeat_lock import atomic_write_text, heartbeat_lock  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent.resolve()
# Repo root on sys.path so `from tools.X import Y` works when this file
# runs as a script (e.g. heartbeat exec'ing /opt/homer/tools/tasks_update.py).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
HEARTBEAT_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "HEARTBEAT.md"
LOCAL_TZ = ZoneInfo("America/New_York")

SECTION_USER = "## User Tasks"
SECTION_COMPLETED = "## Completed"
TASK_TYPE_AGENTIC = "agentic"
TASK_TYPE_SYSTEM = "system"
TASK_TYPE_REMINDER = ""  # default/unset type — plain user reminders

# Stable task identifier — `t_` prefix + 8 lowercase base32 chars (alphabet
# `abcdefghijklmnopqrstuvwxyz234567`). 5 random bytes → 8 base32 chars,
# ~10^12 of address space which is plenty for one household.
TASK_ID_RE = re.compile(r"^t_[a-z2-7]{8}$")


def generate_task_id() -> str:
    """Generate a fresh stable task id like ``t_a1b2c3d4``."""
    raw = secrets.token_bytes(5)
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return f"t_{encoded}"


def is_task_id(s: str) -> bool:
    return bool(TASK_ID_RE.match(s))


def _backfill_user_task_ids(content: str) -> tuple[str, int]:
    """Add an ``Id:`` line to every User Tasks block missing one.

    Returns the (possibly-updated) content and the number of IDs added.
    Pure function — no I/O. Caller is responsible for atomic write while
    holding the heartbeat lock.

    The Id line is inserted directly after the ``###`` heading so it
    sits at the very top of each task block (above Type/Schedule/etc.),
    matching the contract baked into the nanobot fork.

    Only blocks with a ``Schedule:`` field are treated as real tasks —
    instruction prose that happens to contain a ``###`` substring (e.g.
    a literal "### title" inside a code span) is left untouched. This
    matches the contract used by ``_compute_due_tasks`` in nanobot.
    """
    start, end = get_user_tasks_bounds(content)
    if start == -1:
        return content, 0
    section = content[start:end]
    if not section.strip():
        return content, 0

    added = 0
    rebuilt_parts: list[str] = []
    cursor = 0
    for m in re.finditer(r"(###\s+.+?)(?=\n###\s|\Z)", section, re.DOTALL):
        block = m.group(0)
        rebuilt_parts.append(section[cursor:m.start()])
        if re.search(r"^Id:\s*t_[a-z2-7]{8}\s*$", block, re.MULTILINE):
            rebuilt_parts.append(block)
        elif not re.search(r"^Schedule:\s*\S", block, re.MULTILINE):
            # Not a real task block (no Schedule field) — leave untouched.
            rebuilt_parts.append(block)
        else:
            lines = block.split("\n")
            heading = lines[0]
            rest = lines[1:]
            new_id = generate_task_id()
            new_block = "\n".join([heading, f"Id: {new_id}", *rest])
            rebuilt_parts.append(new_block)
            added += 1
        cursor = m.end()
    rebuilt_parts.append(section[cursor:])
    if added == 0:
        return content, 0
    new_section = "".join(rebuilt_parts)
    return content[:start] + new_section + content[end:], added


def _read_and_backfill(in_lock: bool) -> str:
    """Read HEARTBEAT.md, lazy-backfill any missing user-task IDs, return content.

    When ``in_lock`` is True the caller already holds the heartbeat lock; we
    write the backfilled content directly. When False we acquire the lock
    just for the backfill write so tools like ``--list`` and ``--backfill-ids``
    self-heal too.
    """
    content = read_heartbeat()
    new_content, added = _backfill_user_task_ids(content)
    if added == 0:
        return content
    if in_lock:
        write_heartbeat(new_content)
        return new_content
    with heartbeat_lock(HEARTBEAT_FILE.parent):
        # Re-read inside the lock to avoid clobbering a concurrent write.
        fresh = read_heartbeat()
        fresh_new, added2 = _backfill_user_task_ids(fresh)
        if added2 > 0:
            write_heartbeat(fresh_new)
            return fresh_new
        return fresh


def read_heartbeat() -> str:
    if not HEARTBEAT_FILE.exists():
        print(json.dumps({"error": "HEARTBEAT.md not found"}))
        sys.exit(1)
    return HEARTBEAT_FILE.read_text(encoding="utf-8")


def write_heartbeat(content: str) -> None:
    """Atomic write — pair with heartbeat_lock for cross-process safety."""
    atomic_write_text(HEARTBEAT_FILE, content)


def get_user_tasks_bounds(content: str) -> tuple[int, int]:
    """Return (start, end) indices of the User Tasks section content."""
    start = content.find(SECTION_USER)
    end = content.find(SECTION_COMPLETED)
    if start == -1 or end == -1 or start >= end:
        return -1, -1
    # Start after the section header line
    start = content.index("\n", start) + 1
    return start, end


def parse_user_tasks(content: str) -> list[dict]:
    """Parse task entries from the User Tasks section."""
    start, end = get_user_tasks_bounds(content)
    if start == -1:
        return []
    section = content[start:end].strip()
    if not section:
        return []

    tasks = []
    for block in re.split(r"\n(?=###\s)", section):
        block = block.strip()
        if not block.startswith("###"):
            continue
        lines = block.split("\n")
        desc = lines[0].lstrip("#").strip()
        task = {"description": desc, "raw": block}
        for line in lines[1:]:
            if line.startswith("Id:"):
                task["id"] = line.split(":", 1)[1].strip()
            elif line.startswith("Type:"):
                task["type"] = line.split(":", 1)[1].strip()
            elif line.startswith("Schedule:"):
                task["schedule"] = line.split(":", 1)[1].strip()
            elif line.startswith("Until:"):
                task["until"] = line.split(":", 1)[1].strip()
            elif line.startswith("Recur:"):
                task["recur"] = line.split(":", 1)[1].strip()
            elif line.startswith("Last-run:"):
                task["last_run"] = line.split(":", 1)[1].strip()
            elif line.startswith("Recipients:"):
                task["recipients"] = line.split(":", 1)[1].strip()
            elif line.startswith("Model:"):
                task["model"] = line.split(":", 1)[1].strip()
            elif line.startswith("Goal:"):
                task["goal"] = line.split(":", 1)[1].strip()
            elif line.startswith("Added:"):
                task["added"] = line.split(":", 1)[1].strip()
        tasks.append(task)
    return tasks


def format_task(desc: str, schedule: str, until: str | None = None,
                recur: str | None = None, recipients: str | None = None,
                model: str | None = None, task_type: str | None = None,
                goal: str | None = None, task_id: str | None = None) -> str:
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    lines = [f"### {desc}"]
    # Id sits immediately after the title, ahead of any other field, so the
    # nanobot fork's heartbeat prompt can lift it with a simple per-block
    # regex without worrying about field order.
    if task_id:
        lines.append(f"Id: {task_id}")
    if task_type:
        lines.append(f"Type: {task_type}")
    lines.append(f"Schedule: {schedule}")
    if recur:
        lines.append(f"Recur: {recur}")
    if until:
        lines.append(f"Until: {until}")
    if recipients:
        lines.append(f"Recipients: {recipients}")
    if model:
        lines.append(f"Model: {model}")
    if goal:
        lines.append(f"Goal: {goal.replace(chr(10), ' ').strip()}")
    lines.append(f"Added: {now}")
    return "\n".join(lines)


DEFAULT_TIER_USER_TASK_CAP = 5


def _enforce_default_tier_cap(tasks: list[dict]) -> None:
    """On default-tier containers, refuse new user tasks past the cap.

    Counts only user-added tasks (Type: system entries are stamped from the
    HEARTBEAT.md template, not via this tool, so they don't count). On other
    tiers (byok / managed / unset) this is a no-op — current behaviour.

    Exits 1 with a user-facing JSON error message that the agent relays
    verbatim. The link points the household at the BYOK switch.
    """
    if os.environ.get("HOMER_MODEL_TIER") != "default":
        return
    raw_cap = (os.environ.get("HOMER_DEFAULT_TIER_MAX_USER_TASKS") or "").strip()
    try:
        cap = int(raw_cap) if raw_cap else DEFAULT_TIER_USER_TASK_CAP
    except ValueError:
        cap = DEFAULT_TIER_USER_TASK_CAP
    user_count = sum(1 for t in tasks if (t.get("type") or "") != TASK_TYPE_SYSTEM)
    if user_count >= cap:
        print(json.dumps({
            "error": (
                f"Default tier supports up to {cap} reminder tasks. "
                "Switch to BYOK to add more — settings: "
                "<portal>/settings/ai-provider"
            )
        }))
        sys.exit(1)


def add_task(desc: str, schedule: str, until: str | None = None,
             recur: str | None = None, recipients: str | None = None,
             model: str | None = None, task_type: str | None = None,
             goal: str | None = None) -> None:
    with heartbeat_lock(HEARTBEAT_FILE.parent):
        content = read_heartbeat()
        # Backfill any pre-existing blocks missing IDs so the file stays
        # consistent — lazy heal on first write.
        content, _ = _backfill_user_task_ids(content)
        _, end = get_user_tasks_bounds(content)
        if end == -1:
            print(json.dumps({"error": "Could not find User Tasks section in HEARTBEAT.md"}))
            sys.exit(1)

        # Cap check runs inside the lock so concurrent --add calls can't both
        # squeak past the limit. Only counts user-added (non-system) tasks.
        _enforce_default_tier_cap(parse_user_tasks(content))

        task_id = generate_task_id()
        task_block = format_task(desc, schedule, until, recur, recipients, model,
                                 task_type=task_type, goal=goal, task_id=task_id)
        # Insert before ## Completed
        updated = content[:end] + task_block + "\n\n" + content[end:]
        write_heartbeat(updated)
    print(json.dumps({"status": "added", "id": task_id, "task": desc, "schedule": schedule}))


def find_task_block(content: str, keyword: str) -> tuple[int, int] | None:
    """Find the start/end of a task block matching keyword.

    If ``keyword`` is a stable task id (``t_xxxxxxxx``) we match by exact
    ``Id:`` line — this is the LLM-preferred path because it disambiguates
    duplicate-titled blocks (the original Piedmont bug). Otherwise we fall
    back to a case-insensitive substring match against the block contents
    for backward compat with title-based callers.
    """
    start, end = get_user_tasks_bounds(content)
    if start == -1:
        return None
    section = content[start:end]

    if is_task_id(keyword):
        # Match on the Id: field — same parser shape as parse_user_tasks so
        # a manually-edited file with stray whitespace ("Id:  t_xxxx ") still
        # resolves. Substring fallback is intentionally NOT consulted here —
        # otherwise the id `t_iysrfqa4` could match a Goal line that happens
        # to contain the same string.
        for m in re.finditer(r"(###\s+.+?)(?=\n###\s|\Z)", section, re.DOTALL):
            for line in m.group(0).splitlines():
                if line.startswith("Id:") and line.split(":", 1)[1].strip() == keyword:
                    return start + m.start(), start + m.end()
        return None

    keyword_lower = keyword.lower()
    # Find matching ### block by substring (legacy path)
    for m in re.finditer(r"(###\s+.+?)(?=\n###\s|\Z)", section, re.DOTALL):
        if keyword_lower in m.group(0).lower():
            block_start = start + m.start()
            block_end = start + m.end()
            return block_start, block_end
    return None


def complete_task(keyword: str, silent: bool = False) -> None:
    """Move a task to ## Completed. ``silent=True`` suppresses the JSON
    status line — used by tick_task's auto-complete path so the caller
    only sees one ``{"status": "auto-completed"}`` line, not both."""
    with heartbeat_lock(HEARTBEAT_FILE.parent):
        content = read_heartbeat()
        content, added = _backfill_user_task_ids(content)
        if added > 0:
            write_heartbeat(content)
        bounds = find_task_block(content, keyword)
        if not bounds:
            print(json.dumps({"error": f"No user task matching '{keyword}' found"}))
            sys.exit(1)

        block_start, block_end = bounds
        task_block = content[block_start:block_end].strip()
        task_desc = task_block.split("\n")[0].lstrip("#").strip()

        # Remove from User Tasks
        updated = content[:block_start] + content[block_end:]

        # Append to Completed section
        completed_marker = SECTION_COMPLETED
        completed_pos = updated.find(completed_marker)
        if completed_pos == -1:
            print(json.dumps({"error": "Completed section not found"}))
            sys.exit(1)

        insert_pos = updated.index("\n", completed_pos) + 1
        completed_entry = f"- {task_desc} (completed {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')})\n"
        updated = updated[:insert_pos] + completed_entry + updated[insert_pos:]

        write_heartbeat(updated)

    # Fire use_case_completed analytics event. Sender isn't known at
    # task-completion time, so attribute to the household — this keeps
    # per-household rollups accurate and avoids collapsing every tenant to a
    # single global "system" distinct_id. Real user identity is carried by
    # the nanobot on_message_received hook.
    try:
        from tools.analytics.events import track_use_case_completed
        from tools.analytics.classify import classify_message
        from tools.analytics.identity import get_distinct_id, get_household_id

        household_id = get_household_id()
        if household_id:
            tag = classify_message(task_desc)
            distinct_id = get_distinct_id(household_id, "household")
            track_use_case_completed(
                distinct_id,
                use_case_tag=tag,
                turns_to_completion=0,  # not tracked at this layer
                outcome="completed",
            )
    except Exception:
        logging.getLogger(__name__).debug("analytics: use_case_completed failed", exc_info=True)

    if not silent:
        print(json.dumps({"status": "completed", "task": task_desc}))


def remove_task(keyword: str) -> None:
    with heartbeat_lock(HEARTBEAT_FILE.parent):
        content = read_heartbeat()
        content, added = _backfill_user_task_ids(content)
        if added > 0:
            write_heartbeat(content)
        bounds = find_task_block(content, keyword)
        if not bounds:
            print(json.dumps({"error": f"No user task matching '{keyword}' found"}))
            sys.exit(1)

        block_start, block_end = bounds
        task_desc = content[block_start:block_end].split("\n")[0].lstrip("#").strip()
        updated = content[:block_start] + content[block_end:]
        write_heartbeat(updated)
    print(json.dumps({"status": "removed", "task": task_desc}))


def tick_task(keyword: str) -> None:
    """Advance a recurring task's Schedule by its Recur interval.

    Supports:
    - Schedule as YYYY-MM-DD or YYYY-MM-DD HH:MM
    - Recur as 'every N day(s)' or 'every N hour(s)'
    When recurrence is in hours, the next Schedule is expressed as YYYY-MM-DD HH:MM.
    When recurrence is in days with a datetime schedule, the time component is preserved.
    """
    auto_complete = False
    with heartbeat_lock(HEARTBEAT_FILE.parent):
        content = read_heartbeat()
        content, added = _backfill_user_task_ids(content)
        if added > 0:
            write_heartbeat(content)
        bounds = find_task_block(content, keyword)
        if not bounds:
            print(json.dumps({"error": f"No user task matching '{keyword}' found"}))
            sys.exit(1)

        block_start, block_end = bounds
        block = content[block_start:block_end]

        # Parse current schedule — match full YYYY-MM-DD HH:MM or plain YYYY-MM-DD
        schedule_m = re.search(r"Schedule:\s*(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)", block)
        # Parse recur — support 'every N minute(s)', 'every N hour(s)', or 'every N day(s)'
        recur_m = re.search(r"Recur:\s*every\s+(\d+)\s+(minute|hour|day)s?", block, re.IGNORECASE)
        until_m = re.search(r"Until:\s*(\d{4}-\d{2}-\d{2})", block)

        if not schedule_m or not recur_m:
            print(json.dumps({"error": "Task does not have a date-based schedule and Recur interval"}))
            sys.exit(1)

        schedule_str = schedule_m.group(1).strip()
        recur_unit = recur_m.group(2).lower()
        recur_n = int(recur_m.group(1))

        # Parse current schedule datetime
        if " " in schedule_str:
            current_dt = datetime.strptime(schedule_str, "%Y-%m-%d %H:%M")
            has_time = True
        else:
            current_dt = datetime.strptime(schedule_str, "%Y-%m-%d")
            has_time = False

        # Compute next schedule datetime — advance past now so the task isn't
        # immediately due again (handles schedules stuck in the past).
        now_naive = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        if recur_unit == "minute":
            delta = timedelta(minutes=recur_n)
        elif recur_unit == "hour":
            delta = timedelta(hours=recur_n)
        else:
            delta = timedelta(days=recur_n)

        if delta.total_seconds() <= 0:
            print(json.dumps({"error": "Recurrence interval must be greater than 0"}))
            sys.exit(1)

        next_dt = current_dt + delta
        if next_dt <= now_naive:
            intervals = (now_naive - next_dt) // delta + 1
            next_dt += delta * intervals

        if recur_unit in ("minute", "hour") or has_time:
            next_schedule_str = next_dt.strftime("%Y-%m-%d %H:%M")
        else:
            next_schedule_str = next_dt.strftime("%Y-%m-%d")

        # Past Until date → auto-complete. Defer the actual move outside
        # this lock since complete_task takes its own; nesting deadlocks.
        if until_m:
            until_date = datetime.strptime(until_m.group(1), "%Y-%m-%d").date()
            if next_dt.date() > until_date:
                auto_complete = True

        if not auto_complete:
            now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
            updated_block = block.replace(
                f"Schedule: {schedule_str}",
                f"Schedule: {next_schedule_str}"
            )
            # Add or update Last-run so Phase 1 can skip duplicate runs on the same day
            if re.search(r"Last-run:", updated_block):
                updated_block = re.sub(r"Last-run:[^\n]*", f"Last-run: {now_str}", updated_block)
            else:
                updated_block = re.sub(
                    r"(Schedule:[^\n]+\n)",
                    f"\\1Last-run: {now_str}\n",
                    updated_block,
                )
            updated = content[:block_start] + updated_block + content[block_end:]
            write_heartbeat(updated)

    if auto_complete:
        complete_task(keyword, silent=True)
        print(json.dumps({"status": "auto-completed", "reason": "past end date"}))
        return
    print(json.dumps({"status": "ticked", "next": next_schedule_str, "last_run": now_str}))


KNOWN_FIELD_ORDER = ["Id", "Type", "Schedule", "Last-run", "Recur", "Until",
                     "Recipients", "Model", "Goal", "Account", "Institution",
                     "SheetId", "Period", "Anchor", "Added"]


def parse_task_fields(lines: list[str]) -> dict[str, str]:
    """Parse `Key: value` lines into a dict, preserving anything that looks like a field."""
    fields: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        # Field keys are short, alpha-ish. Skip prose lines that happen to contain ":".
        if not key or " " in key or len(key) > 32:
            continue
        fields[key] = value.strip()
    return fields


def edit_task(keyword: str, new_desc: str | None = None, new_schedule: str | None = None,
              new_recur: str | None = None, new_until: str | None = None,
              new_recipients: str | None = None, new_model: str | None = None,
              new_goal: str | None = None,
              extra_fields: list[str] | None = None) -> None:
    """Edit an existing task's fields, identified by a keyword match on its description.

    Only the fields explicitly provided are updated; all other fields are preserved.
    Passing an empty string for any optional field removes it.

    extra_fields is a list of "Key=Value" or "Key=" strings (empty value clears the field).
    Use it for fields without a dedicated flag (Account, Institution, SheetId, Period, Anchor, ...).
    """
    if not any([new_desc, new_schedule, new_recur is not None,
                new_until is not None, new_recipients is not None,
                new_model is not None, new_goal is not None,
                extra_fields]):
        print(json.dumps({"error": "No fields to edit provided"}))
        sys.exit(1)

    parsed_extras: list[tuple[str, str]] = []
    for item in extra_fields or []:
        if "=" not in item:
            print(json.dumps({"error": f"--field expects KEY=VALUE, got '{item}'"}))
            sys.exit(1)
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            print(json.dumps({"error": f"--field key cannot be empty (got '{item}')"}))
            sys.exit(1)
        # Strip embedded newlines so a value can't break the line-based file structure.
        value = value.replace("\r", "").replace("\n", " ").strip()
        parsed_extras.append((key, value))

    with heartbeat_lock(HEARTBEAT_FILE.parent):
        content = read_heartbeat()
        content, added = _backfill_user_task_ids(content)
        if added > 0:
            write_heartbeat(content)
        bounds = find_task_block(content, keyword)
        if not bounds:
            print(json.dumps({"error": f"No user task matching '{keyword}' found"}))
            sys.exit(1)

        block_start, block_end = bounds
        block = content[block_start:block_end]

        lines = block.split("\n")
        old_desc = lines[0].lstrip("#").strip()

        fields = parse_task_fields(lines[1:])

        desc = new_desc if new_desc else old_desc
        if new_schedule is not None:
            fields["Schedule"] = new_schedule
        if new_recur is not None:
            if new_recur == "":
                fields.pop("Recur", None)
            else:
                fields["Recur"] = new_recur
        if new_until is not None:
            if new_until == "":
                fields.pop("Until", None)
            else:
                fields["Until"] = new_until
        if new_recipients is not None:
            if new_recipients == "":
                fields.pop("Recipients", None)
            else:
                fields["Recipients"] = new_recipients
        if new_model is not None:
            if new_model == "":
                fields.pop("Model", None)
            else:
                fields["Model"] = new_model
        if new_goal is not None:
            if new_goal == "":
                fields.pop("Goal", None)
            else:
                fields["Goal"] = new_goal.replace("\n", " ").strip()
        for key, value in parsed_extras:
            if value == "":
                fields.pop(key, None)
            else:
                fields[key] = value

        new_lines = [f"### {desc}"]
        # Emit known fields in canonical order, then any unknown fields after.
        for field in KNOWN_FIELD_ORDER:
            if field in fields:
                new_lines.append(f"{field}: {fields[field]}")
        for field in fields:
            if field not in KNOWN_FIELD_ORDER:
                new_lines.append(f"{field}: {fields[field]}")
        updated_block = "\n".join(new_lines)

        # Preserve the original trailing whitespace (blank lines between tasks / before ## Completed)
        orig_block = content[block_start:block_end]
        trailing_ws = orig_block[len(orig_block.rstrip()):]
        updated = content[:block_start] + updated_block + trailing_ws + content[block_end:]
        write_heartbeat(updated)
    print(json.dumps({"status": "edited", "task": desc, "fields": {
        k: v for k, v in fields.items()
    }}))


def list_tasks() -> None:
    content = _read_and_backfill(in_lock=False)
    tasks = parse_user_tasks(content)
    output = [{"description": t["description"],
               "id": t.get("id", ""),
               "type": t.get("type", ""),
               "schedule": t.get("schedule", ""),
               "until": t.get("until", ""), "recur": t.get("recur", ""),
               "recipients": t.get("recipients", ""),
               "model": t.get("model", ""),
               "goal": t.get("goal", "")} for t in tasks]
    print(json.dumps(output, indent=2))


def backfill_ids() -> None:
    """Explicit one-shot backfill — useful for ops dry-runs."""
    with heartbeat_lock(HEARTBEAT_FILE.parent):
        content = read_heartbeat()
        new_content, added = _backfill_user_task_ids(content)
        if added > 0:
            write_heartbeat(new_content)
    print(json.dumps({"status": "backfilled", "added": added}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage user tasks in HEARTBEAT.md.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true", help="Add a new task")
    group.add_argument("--complete", metavar="KEYWORD", help="Mark task as completed")
    group.add_argument("--remove", metavar="KEYWORD", help="Remove task entirely")
    group.add_argument("--tick", metavar="KEYWORD", help="Advance recurring task to next date")
    group.add_argument("--list", action="store_true", help="List current user tasks as JSON")
    group.add_argument("--edit", metavar="KEYWORD", help="Edit an existing task's fields")
    group.add_argument("--backfill-ids", action="store_true",
                       help="Add stable Id: lines to any user-task block missing one")

    parser.add_argument("--desc", help="Task description (for --add or --edit)")
    parser.add_argument("--schedule", help="Schedule: date (YYYY-MM-DD) or time phrase (for --add)")
    parser.add_argument("--recur", help="Recurrence: e.g. 'every 2 days' (for --add)")
    parser.add_argument("--until", help="End date YYYY-MM-DD (for --add with --recur)")
    parser.add_argument("--recipients", help="Comma-separated list of alias:channel pairs, e.g. 'abc@lid:whatsapp' or 'primary:tg,sam:whatsapp' (for --add)")
    parser.add_argument("--model", help="Model preset: flash, pro, sonnet, haiku (for --add or --edit)")
    parser.add_argument("--type", dest="task_type", help="Task type: 'agentic' for tool-use tasks (for --add)")
    parser.add_argument("--goal", help="Detailed goal/instructions for agentic tasks (for --add or --edit)")
    parser.add_argument("--field", action="append", default=[], metavar="KEY=VALUE",
                        help="Set or clear an arbitrary task field (for --edit). "
                             "Empty value clears. Repeatable. e.g. --field SheetId=1aBc --field Period=biweekly")

    args = parser.parse_args()

    if args.add:
        if not args.desc or not args.schedule:
            parser.error("--add requires --desc and --schedule")
        if not args.recipients:
            parser.error("--add requires --recipients")
        if args.task_type and args.task_type != TASK_TYPE_AGENTIC:
            parser.error(f"--type must be '{TASK_TYPE_AGENTIC}'")
        add_task(args.desc, args.schedule, until=args.until, recur=args.recur,
                 recipients=args.recipients, model=args.model,
                 task_type=args.task_type, goal=args.goal)
    elif args.complete:
        complete_task(args.complete)
    elif args.remove:
        remove_task(args.remove)
    elif args.tick:
        tick_task(args.tick)
    elif args.list:
        list_tasks()
    elif args.backfill_ids:
        backfill_ids()
    elif args.edit:
        if not any([args.desc, args.schedule, args.recur is not None,
                    args.until is not None, args.recipients is not None,
                    args.model is not None, args.goal is not None,
                    args.field]):
            parser.error("--edit requires at least one field to update: --desc, --schedule, --recur, --until, --recipients, --model, --goal, or --field KEY=VALUE")
        edit_task(args.edit, new_desc=args.desc, new_schedule=args.schedule,
                  new_recur=args.recur, new_until=args.until,
                  new_recipients=args.recipients, new_model=args.model,
                  new_goal=args.goal, extra_fields=args.field)


if __name__ == "__main__":
    main()

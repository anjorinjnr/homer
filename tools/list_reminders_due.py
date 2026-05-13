#!/usr/bin/env python3
"""
list_reminders_due.py — Today's user reminders, filtered and ready to render.

Reads HEARTBEAT.md via `tasks_update.py --list`, applies the filters that
keep the morning brief useful (no system/agentic tasks, no goal-bearing
agentic-shaped tasks, only items scheduled for the target date), and
returns each surviving reminder with a friendly `display_when` field.

This replaces the reminder block inside `morning_briefing.py`. Key
behavior change vs. the composer: reminders surface ONLY on the day
they're due, never as a multi-day lookahead. The heartbeat already
fires reminders at their schedule time, so a brief listing today's
reminders is a heads-up; surfacing the next 5 mornings in a row is
nagging.

Usage:
    python tools/list_reminders_due.py
    python tools/list_reminders_due.py --date 2026-05-13
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from tasks_update import TASK_TYPE_REMINDER

REPO_ROOT = Path(__file__).parent.parent.resolve()
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
TOOLS_DIR = REPO_ROOT / "tools"

LOCAL_TZ = ZoneInfo("America/New_York")


def _friendly_time(hour: int, minute: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    if minute == 0:
        return f"{h12}{suffix}"
    return f"{h12}:{minute:02d}{suffix}"


def _friendly_time_from_str(raw: str) -> str:
    """Convert a time-bearing string to '9am' / '2pm' / '12:30pm'."""
    if not raw or raw == "all-day":
        return raw
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", raw.strip(), re.IGNORECASE)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2) or 0)
        suf = m.group(3).lower()
        if suf == "pm" and h != 12:
            h += 12
        elif suf == "am" and h == 12:
            h = 0
        return _friendly_time(h, mm)
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if m:
        return _friendly_time(int(m.group(1)), int(m.group(2)))
    return raw


def _enrich(reminder: dict) -> dict:
    """Add display_when = '9am Today' or 'Today' for all-day reminders."""
    out = dict(reminder)
    schedule = reminder.get("schedule", "")
    parts = schedule.split(" ", 1)
    if len(parts) == 2 and ":" in parts[1]:
        out["display_when"] = f"{_friendly_time_from_str(parts[1])} Today"
    else:
        out["display_when"] = "Today"
    return out


def _run_tasks_list() -> list[dict] | None:
    """Shell out to tasks_update.py --list. Returns the parsed list, or None
    on subprocess / JSON failure (matches morning_briefing.run_tool semantics).
    """
    cmd = [str(VENV_PYTHON), str(TOOLS_DIR / "tasks_update.py"), "--list"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        stdout = result.stdout.strip()
        if not stdout:
            return None
        parsed = json.loads(stdout)
        return parsed if isinstance(parsed, list) else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return None


def filter_reminders(tasks: list[dict], target: date) -> list[dict]:
    """Return reminders scheduled exactly for ``target``.

    Filters: plain reminders only (drop system / agentic / goal-bearing),
    schedule's date portion equals target. Items whose schedule is missing
    or unparseable are dropped — without a date we can't claim they're
    due today.
    """
    out: list[dict] = []
    for t in tasks:
        if t.get("type", TASK_TYPE_REMINDER) != TASK_TYPE_REMINDER:
            continue
        if t.get("goal"):
            continue
        schedule = t.get("schedule", "")
        if not schedule:
            continue
        try:
            rem_date = date.fromisoformat(schedule.split(" ")[0])
        except ValueError:
            continue
        if rem_date != target:
            continue
        out.append(_enrich(t))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="List reminders due on a date.")
    parser.add_argument("--date", dest="target_date",
                        help="ISO date (YYYY-MM-DD); defaults to today in local TZ")
    args = parser.parse_args()

    if args.target_date:
        try:
            target = date.fromisoformat(args.target_date)
        except ValueError:
            print(json.dumps({"error": f"--date expects YYYY-MM-DD, got '{args.target_date}'"}))
            sys.exit(1)
    else:
        target = datetime.now(LOCAL_TZ).date()

    tasks = _run_tasks_list()
    if tasks is None:
        # Match morning_briefing's silent-degrade pattern: empty list, not
        # an error. The brief prompt decides whether to surface the gap.
        print(json.dumps([]))
        return

    print(json.dumps(filter_reminders(tasks, target), indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
morning_briefing.py — Gather data for the daily morning briefing.

Collects calendar events, open email action items, due reminders, per-user
presentation preferences, and recent motivation lines into a single JSON
payload. The LLM formats the message; this tool emits raw data only.

Usage:
    python tools/morning_briefing.py                   # emit briefing JSON
    python tools/morning_briefing.py --log-motivation "<line>"
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
import accounts
from google_auth import has_google_token
from manage_users import list_users as _list_users
from tasks_update import TASK_TYPE_REMINDER

REPO_ROOT = Path(__file__).parent.parent.resolve()
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
TOOLS_DIR = REPO_ROOT / "tools"
STATE_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "state"
MOTIVATIONS_FILE = STATE_DIR / "recent_motivations.txt"
MOTIVATIONS_KEEP = 7
REMINDER_LOOKAHEAD_DAYS = 5

LOCAL_TZ = ZoneInfo("America/New_York")


def run_tool(script: str, extra_args: list[str] | None = None) -> dict | list | None:
    """Run a tool script and parse its JSON output. Returns None on failure."""
    cmd = [str(VENV_PYTHON), str(TOOLS_DIR / script)]
    if extra_args:
        cmd.extend(extra_args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        stdout = result.stdout.strip()
        if not stdout:
            return None
        return json.loads(stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return None


URGENCY_DISPLAY = {
    "today": "today",
    "this_week": "this week",
    "low": "low priority",
}


def _friendly_time(hour: int, minute: int) -> str:
    """Format HH:MM as '9am', '2pm', '12:30pm'."""
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    if minute == 0:
        return f"{h12}{suffix}"
    return f"{h12}:{minute:02d}{suffix}"


def _friendly_time_from_str(raw: str) -> str:
    """Convert a time-bearing string to '9am' / '2pm' / '12:30pm'.

    Accepts '14:00' (24h), '2:00 PM' (calendar_fetch format), '9am', 'all-day'.
    Unknown inputs return unchanged.
    """
    if not raw or raw == "all-day":
        return raw
    # 12h with am/pm suffix, with optional minutes and whitespace
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", raw.strip(), re.IGNORECASE)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2) or 0)
        suf = m.group(3).lower()
        # Convert back to 24h so _friendly_time handles midnight/noon consistently.
        if suf == "pm" and h != 12:
            h += 12
        elif suf == "am" and h == 12:
            h = 0
        return _friendly_time(h, mm)
    # 24h 'HH:MM'
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if m:
        return _friendly_time(int(m.group(1)), int(m.group(2)))
    return raw


def _relative_date(d: date, today: date) -> str:
    """'Today' / 'Tomorrow' / 'Wed Apr 22' (within a week) / 'Apr 29' (further)."""
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if 2 <= delta <= 6:
        return d.strftime("%a %b %-d")
    return d.strftime("%b %-d")


def _enrich_event(e: dict, today: date, account: str | None = None) -> dict:
    """Add display_date + display_time (+ source account label) to a calendar event."""
    out = dict(e)
    iso = e.get("date", "")
    try:
        ev_date = date.fromisoformat(iso)
        out["display_date"] = _relative_date(ev_date, today)
    except ValueError:
        pass
    raw_time = e.get("time", "")
    if raw_time and raw_time != "all-day":
        out["display_time"] = _friendly_time_from_str(raw_time)
    if account:
        out["account"] = account
    return out


def _valid_account_names() -> list[str]:
    """Names of all linked Google accounts whose tokens are usable. Drops
    accounts with unreadable or expired-non-refreshable tokens so a single
    stale account doesn't break the whole brief."""
    return [
        name for name in accounts._discover_account_names()
        if accounts._account_metadata(name).get("valid")
    ]


def _fetch_calendar_for(account: str) -> dict | None:
    """Run calendar_fetch.py for one account. Returns its parsed JSON or
    None if the subprocess failed."""
    return run_tool("calendar_fetch.py", ["--account", account])  # type: ignore[return-value]


def _merge_calendar_payloads(per_account: dict[str, dict | None]) -> tuple[list, list, list[str]]:
    """Combine multiple calendar_fetch payloads into one (today, week, failed).

    Events from each account are stamped with an ``account`` field. Returns
    the merged today_events, week_events, and the list of accounts that
    failed to fetch so the brief can mark itself partial.
    """
    today_events: list[dict] = []
    week_events: list[dict] = []
    failed: list[str] = []
    for name, payload in per_account.items():
        if payload is None:
            failed.append(name)
            continue
        for e in payload.get("today_events", []) or []:
            today_events.append({**e, "account": name})
        for e in payload.get("week_events", []) or []:
            week_events.append({**e, "account": name})
    return today_events, week_events, failed


def _enrich_reminder(r: dict, today: date) -> dict:
    """Add display_when = '9am Today' / '12pm Tomorrow' / '3pm Thu Apr 24'."""
    out = dict(r)
    schedule = r.get("schedule", "")
    # Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'
    parts = schedule.split(" ", 1)
    if not parts[0]:
        return out
    try:
        rem_date = date.fromisoformat(parts[0])
    except ValueError:
        return out
    when = _relative_date(rem_date, today)
    if len(parts) == 2 and ":" in parts[1]:
        t = _friendly_time_from_str(parts[1])
        out["display_when"] = f"{t} {when}"
    else:
        out["display_when"] = when
    return out


def _enrich_action_item(a: dict) -> dict:
    """Add display_urgency ('today' / 'this week' / 'low priority')."""
    out = dict(a)
    u = a.get("urgency", "")
    if u in URGENCY_DISPLAY:
        out["display_urgency"] = URGENCY_DISPLAY[u]
    elif u:
        out["display_urgency"] = u
    return out


def load_users() -> list[dict]:
    """Return [{name, briefing_style}] from the user registry. Style omitted if unset."""
    out = []
    for u in _list_users():
        name = u.get("name")
        if not name:
            continue
        entry = {"name": name}
        style = u.get("briefing_style")
        if style:
            entry["briefing_style"] = style
        out.append(entry)
    return out


def load_recent_motivations() -> list[str]:
    """Return up to the last MOTIVATIONS_KEEP motivation lines, oldest → newest."""
    if not MOTIVATIONS_FILE.exists():
        return []
    try:
        lines = MOTIVATIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-MOTIVATIONS_KEEP:]


def log_motivation(line: str) -> None:
    """Append a motivation line, trimming history to MOTIVATIONS_KEEP."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    history = load_recent_motivations()
    history.append(line.strip())
    history = history[-MOTIVATIONS_KEEP:]
    MOTIVATIONS_FILE.write_text("\n".join(history) + "\n", encoding="utf-8")
    print(json.dumps({"status": "logged", "kept": len(history)}))


def gather_briefing(account: str | None = None) -> dict:
    """Build the morning briefing payload.

    If ``account`` is provided, calendar data is fetched from that account
    only. If omitted, calendar fan-out runs across every linked account
    whose token is valid — events from each are tagged with an ``account``
    field so the LLM can label them per-source in the formatted brief.
    """
    from concurrent.futures import ThreadPoolExecutor

    cal_accounts = [account] if account else _valid_account_names()

    with ThreadPoolExecutor(max_workers=max(3, len(cal_accounts))) as pool:
        cal_futures = {name: pool.submit(_fetch_calendar_for, name) for name in cal_accounts}
        actions_future = pool.submit(run_tool, "email_action_items.py", ["--list"])
        tasks_future = pool.submit(run_tool, "tasks_update.py", ["--list"])

    per_account_calendar = {name: f.result() for name, f in cal_futures.items()}
    action_items_data = actions_future.result()
    tasks_data = tasks_future.result()

    today_events_raw, week_events_raw, failed_accounts = _merge_calendar_payloads(per_account_calendar)
    multi_account = len(cal_accounts) > 1

    today = datetime.now(LOCAL_TZ).date()

    # Only plain user reminders belong in the briefing:
    # - Exclude system/agentic tasks (Homer's own scheduled work)
    # - Exclude tasks with a goal field (agentic even if not explicitly typed)
    # - Exclude reminders scheduled for today or earlier — heartbeat fires them
    #   at their schedule time, so surfacing them in the brief is duplication
    # - Exclude reminders scheduled more than 5 days out (not yet relevant)
    reminders = []
    if isinstance(tasks_data, list):
        for t in tasks_data:
            if t.get("type", TASK_TYPE_REMINDER) != TASK_TYPE_REMINDER:
                continue
            if t.get("goal"):
                continue
            schedule = t.get("schedule", "")
            if schedule:
                try:
                    rem_date = date.fromisoformat(schedule.split(" ")[0])
                    days_out = (rem_date - today).days
                    if days_out <= 0 or days_out > REMINDER_LOOKAHEAD_DAYS:
                        continue
                except ValueError:
                    pass
            reminders.append(_enrich_reminder(t, today))

    action_items = [_enrich_action_item(a)
                    for a in (action_items_data if isinstance(action_items_data, list) else [])]

    briefing: dict = {
        "type": "morning_briefing",
        "date": today.isoformat(),
        "today_events": [_enrich_event(e, today, e.get("account")) for e in today_events_raw],
        "week_events": [_enrich_event(e, today, e.get("account")) for e in week_events_raw],
        "action_items": action_items,
        "reminders": reminders,
        "users": load_users(),
        "recent_motivations": load_recent_motivations(),
    }

    if multi_account:
        briefing["accounts"] = cal_accounts
    if failed_accounts:
        briefing["calendar_partial"] = failed_accounts
        if len(failed_accounts) == len(cal_accounts):
            # All accounts failed — caller wants the same signal the
            # single-account path used to emit.
            briefing["calendar_error"] = "Could not fetch calendar events"
    return briefing


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning briefing data + motivation log")
    parser.add_argument("--log-motivation", dest="log_line",
                        help="Append this motivation line to state (rolling last 7)")
    parser.add_argument("--account",
                        help="Restrict calendar fetch to one account. Omit to fan out across "
                             "every linked account whose token is valid.")
    args = parser.parse_args()

    if args.log_line is not None:
        if not args.log_line.strip():
            print(json.dumps({"error": "--log-motivation requires a non-empty line"}))
            sys.exit(1)
        log_motivation(args.log_line)
        return

    # Early SKIP for tenants who haven't connected Google. The morning
    # briefing pulls Calendar (today + week ahead) AND Gmail action items,
    # so both are gated on the Google token. The matching agent rule in
    # AGENTS.md treats `SKIP:` output as "no message this turn" — same
    # pattern as plaid_balance_check / budget_check. Connect Google →
    # briefing fires. Spell out Gmail + Calendar explicitly so the agent
    # doesn't paraphrase the missing capability as just "Calendar."
    if not has_google_token():
        print(
            "SKIP: Gmail and Calendar are not connected yet — both are "
            "required for the morning briefing. The onboarding workspace "
            "push provides the link; do not message the user from this "
            "heartbeat tick."
        )
        return

    print(json.dumps(gather_briefing(account=args.account)))


if __name__ == "__main__":
    main()

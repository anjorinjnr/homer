#!/usr/bin/env python3
"""
calendar_add.py — Create, edit, or search Google Calendar events via gogcli.

Used by Homer to add events from user messages (including image-based invites).
Output shape preserved from the prior google-api-python-client implementation
so existing consumers (skill workflows, simulation tests) keep working.

Usage:
    # Timed event
    python tools/calendar_add.py --title "Jake's Birthday Party" \\
        --date 2026-03-15 --time 14:00 --duration 120 \\
        --location "123 Oak St" --description "From school invite"

    # All-day event (omit --time)
    python tools/calendar_add.py --title "Jake's Birthday" --date 2026-03-15

    # Multi-day all-day (--end-date inclusive)
    python tools/calendar_add.py --title "Spring Break" --date 2026-03-22 --end-date 2026-03-28

    # Edit existing event
    python tools/calendar_add.py --edit --event-id <id> --title "..." --date ... --time ...

    # Recurring (shorthand or raw RRULE)
    python tools/calendar_add.py --title "Standup" --date 2026-03-15 --time 09:00 --recur weekly
    python tools/calendar_add.py --title "MWF" --date 2026-03-15 --time 09:00 \\
        --recur "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"

    # Search by title + date (returns event_id for editing)
    python tools/calendar_add.py --search --title "Karate" --date 2026-03-15

    # Dry run (print canonical event body, don't write)
    python tools/calendar_add.py --title "Test" --date 2026-03-15 --dry-run

Output (JSON):
    {"status": "created"|"updated", "event_id": "...", "link": "...",
     "title": "...", "date": "...", "time": "..."}
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import DEFAULT_ACCOUNT, load_google_credentials, require_scopes

LOCAL_TZ = ZoneInfo("America/New_York")
SKIP_CALENDARS = {"Birthdays", "Holidays in United States"}
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


# ─── gogcli helpers ──────────────────────────────────────────────────────────


def get_access_token(account: str) -> str:
    creds = load_google_credentials(account)
    require_scopes(creds, account, CALENDAR_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


# ─── time/date parsing (unchanged from prior implementation) ──────────────────


def _parse_time(date_str: str, time_str: str, field: str = "--time") -> datetime:
    """Parse time in HH:MM (24h) or H:MM AM/PM (12h) format."""
    time_str = time_str.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p",
                "%Y-%m-%d %I %p", "%Y-%m-%d %I%p"):
        try:
            return datetime.strptime(f"{date_str} {time_str.upper()}", fmt)
        except ValueError:
            continue
    print(f"ERROR: Could not parse {field} value '{time_str}'. "
          f"Use HH:MM (24h) or H:MM AM/PM — e.g. 15:30 or 3:30 PM.", file=sys.stderr)
    sys.exit(1)


def _resolve_rrule(recur: str) -> str:
    shorthand = {
        "daily": "RRULE:FREQ=DAILY",
        "weekly": "RRULE:FREQ=WEEKLY",
        "monthly": "RRULE:FREQ=MONTHLY",
    }
    rrule = shorthand.get(recur.strip().lower(), recur.strip())
    if not rrule.startswith("RRULE:"):
        raise ValueError(
            "--recur must be daily/weekly/monthly or a raw RRULE string "
            "(e.g. 'RRULE:FREQ=WEEKLY;BYDAY=MO,WE')"
        )
    return rrule


def _resolve_when(args) -> tuple[str, str, bool]:
    """Resolve the start + end + is_all_day from parsed args.

    Single source of truth for the time/date math so build_event_body and
    build_gog_args can't drift apart on duration arithmetic, the all-day
    +1 day adjustment, or timezone handling. Returns:

      - (RFC3339 start, RFC3339 end, False)  for timed events
      - (YYYY-MM-DD start, YYYY-MM-DD end_exclusive, True)  for all-day events

    The +1 day adjustment in the all-day case matches Google Calendar API's
    exclusive-end semantics — gogcli does not auto-adjust this.
    """
    if args.time:
        start_dt = _parse_time(args.date, args.time, "--time").replace(tzinfo=LOCAL_TZ)
        if args.end_time:
            end_dt = _parse_time(args.date, args.end_time, "--end-time").replace(tzinfo=LOCAL_TZ)
        else:
            duration_min = args.duration if args.duration else 60
            end_dt = start_dt + timedelta(minutes=duration_min)
        return start_dt.isoformat(), end_dt.isoformat(), False

    start_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() + timedelta(days=1)
    else:
        end_date = start_date + timedelta(days=1)
    return start_date.isoformat(), end_date.isoformat(), True


def build_event_body(args) -> dict:
    """Construct the canonical Google Calendar event resource dict.

    Used for --dry-run output. The actual create/update calls go through
    gogcli flags (build_gog_args), but consumers expect dry-run to print
    this canonical shape, so we still build it.
    """
    event = {"summary": args.title}
    if args.location:
        event["location"] = args.location
    if args.description:
        event["description"] = args.description

    start, end, all_day = _resolve_when(args)
    if all_day:
        event["start"] = {"date": start}
        event["end"] = {"date": end}
    else:
        tz_str = str(LOCAL_TZ)
        event["start"] = {"dateTime": start, "timeZone": tz_str}
        event["end"] = {"dateTime": end, "timeZone": tz_str}

    if args.recur:
        event["recurrence"] = [_resolve_rrule(args.recur)]
    return event


def build_gog_args(args) -> list[str]:
    """Construct gogcli flags for calendar create/update."""
    flags = ["--summary", args.title]
    if args.location:
        flags += ["--location", args.location]
    if args.description:
        flags += ["--description", args.description]

    start, end, all_day = _resolve_when(args)
    flags += ["--from", start, "--to", end]
    if all_day:
        flags.append("--all-day")

    if args.recur:
        flags += ["--rrule", _resolve_rrule(args.recur)]
    return flags


def format_result(event: dict, status: str) -> dict:
    start = event.get("start", {})
    date_str = start.get("date") or start.get("dateTime", "")[:10]
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"]).astimezone(LOCAL_TZ)
        time_str = dt.strftime("%-I:%M %p")
    else:
        time_str = "all-day"
    return {
        "status": status,
        "event_id": event.get("id", ""),
        "link": event.get("htmlLink", ""),
        "title": event.get("summary", ""),
        "date": date_str,
        "time": time_str,
    }


# ─── search mode ──────────────────────────────────────────────────────────────


def list_calendars(token: str) -> list[dict]:
    data = gogcli.run(token, "calendar", "calendars")
    out = []
    for c in data.get("calendars", []):
        summary = c.get("summary") or c.get("id", "")
        if summary in SKIP_CALENDARS:
            continue
        out.append({"id": c["id"], "summary": summary})
    return out


def search_events(token: str, query: str, date_str: str, calendar: str) -> list[dict]:
    """Find events matching `query` on `date_str` (YYYY-MM-DD).

    Uses gogcli's events command with --calendars=csv so each result carries
    a CalendarID — required so the caller can edit/delete the matched event.
    The `search` subcommand was rejected because its responses don't include
    CalendarID per event.
    """
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise RuntimeError(f"Invalid date '{date_str}'. Use YYYY-MM-DD.")
    next_day = day + timedelta(days=1)

    if calendar == "primary":
        cals = list_calendars(token)
    else:
        cals = [{"id": calendar, "summary": calendar}]
    if not cals:
        return []

    cal_ids = ",".join(c["id"] for c in cals)
    data = gogcli.run(
        token, "calendar", "events",
        f"--calendars={cal_ids}",
        f"--from={day.isoformat()}",
        f"--to={next_day.isoformat()}",
        f"--query={query}",
        "--all-pages",
        "--max=50",
    )
    raw_events = data.get("events", [])

    # gogcli's --query is server-side and matches body/location/attendees as
    # well, so we filter again here to preserve the prior tool's strict
    # title-substring semantics. Consumers (skill workflows) rely on the
    # narrower match.
    q_lower = query.lower()
    out = []
    for ev in raw_events:
        title = ev.get("summary", "")
        if q_lower not in title.lower():
            continue
        start = ev.get("start", {})
        if "dateTime" in start:
            dt = datetime.fromisoformat(start["dateTime"]).astimezone(LOCAL_TZ)
            time_str = dt.strftime("%-I:%M %p")
        else:
            time_str = "all-day"
        out.append({
            "event_id": ev.get("id", ""),
            "calendar_id": ev.get("CalendarID", calendar),
            "title": title,
            "date": date_str,
            "time": time_str,
            "location": ev.get("location", ""),
        })
    return out


# ─── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Create, edit, or search Google Calendar events.")
    parser.add_argument("--title", help="Event title/summary")
    parser.add_argument("--date", help="Event date (YYYY-MM-DD)")
    parser.add_argument("--time", help="Start time: HH:MM (24h) or H:MM AM/PM. Omit for all-day.")
    parser.add_argument("--end-date", help="End date for multi-day all-day events (YYYY-MM-DD, inclusive). Ignored if --time set.")
    parser.add_argument("--end-time", help="End time: HH:MM (24h) or H:MM AM/PM. Overrides --duration.")
    parser.add_argument("--duration", type=int, help="Duration in minutes (default: 60). Ignored if --end-time set.")
    parser.add_argument("--location", help="Event location")
    parser.add_argument("--description", help="Event description or notes")
    parser.add_argument("--calendar", default="primary", help="Calendar ID (default: primary)")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to use (default: {DEFAULT_ACCOUNT})")
    parser.add_argument("--edit", action="store_true", help="Edit an existing event (requires --event-id)")
    parser.add_argument("--event-id", help="Event ID to edit (required with --edit)")
    parser.add_argument("--search", action="store_true", help="Search for events by title+date (returns event_id)")
    parser.add_argument("--recur", help="Recurrence: daily, weekly, monthly, or a raw RRULE string")
    parser.add_argument("--dry-run", action="store_true", help="Print event body without writing")
    args = parser.parse_args()

    # Search mode — no credentials needed for arg validation, but does need them for gogcli.
    if args.search:
        if not args.title or not args.date:
            print(json.dumps({"error": "--search requires --title and --date"}))
            sys.exit(1)
        try:
            token = get_access_token(args.account)
            matches = search_events(token, args.title, args.date, args.calendar)
        except (FileNotFoundError, PermissionError, RuntimeError) as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        print(json.dumps({"matches": matches}))
        return

    if not args.title or not args.date:
        print(json.dumps({"error": "--title and --date are required"}))
        sys.exit(1)
    if args.edit and not args.event_id:
        print(json.dumps({"error": "--edit requires --event-id"}))
        sys.exit(1)

    if args.dry_run:
        try:
            event_body = build_event_body(args)
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        print("=== Dry run — event body ===")
        print(json.dumps(event_body, indent=2, default=str))
        return

    try:
        token = get_access_token(args.account)
        gog_args = build_gog_args(args)
    except (FileNotFoundError, PermissionError, RuntimeError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    try:
        if args.edit:
            data = gogcli.run(token, "calendar", "update", args.calendar, args.event_id, *gog_args)
            status = "updated"
        else:
            data = gogcli.run(token, "calendar", "create", args.calendar, *gog_args)
            status = "created"
    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    event = data.get("event") or {}
    if not event.get("id"):
        print(json.dumps({"error": f"gogcli returned no event in response: {json.dumps(data)[:200]}"}))
        sys.exit(1)
    print(json.dumps(format_result(event, status)))


if __name__ == "__main__":
    main()

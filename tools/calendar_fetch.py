#!/usr/bin/env python3
"""
calendar_fetch.py — Fetch upcoming calendar events via the gogcli wrapper.

Aggregates events across all the user's calendars (excluding noisy system
ones like Holidays/Birthdays), localizes timed events to LOCAL_TZ for display,
and splits into today_events vs week_events for Homer's daily briefing.

Output shape preserved from the prior google-api-python-client implementation
so morning_briefing.py and other consumers continue to work unchanged.

Usage:
    python tools/calendar_fetch.py                     # today + 7 days
    python tools/calendar_fetch.py --days 3            # today + N days
    python tools/calendar_fetch.py --dry-run           # pretty-print
    python tools/calendar_fetch.py --account personal  # different account
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import DEFAULT_ACCOUNT, has_google_token, load_google_credentials, require_scopes

LOCAL_TZ = ZoneInfo("America/New_York")
# Default denylist of low-signal/noise calendars. Most users want them out of
# the brief by default; can be overridden in PR2's brief_preferences.yaml.
SKIP_CALENDARS = {
    "Birthdays",
    "Holidays in United States",
    "Phases of the Moon",
    "Week Numbers",
}
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

# Google access roles that give us only free/busy visibility — title comes
# back as literal "Busy", no description / attendees / location. We pass these
# events through tagged so downstream code (briefing inference, conflict
# detection) can reason about *when* the user is blocked without claiming
# to know what for.
OPAQUE_ACCESS_ROLES = {"freeBusyReader"}


def get_access_token(account: str) -> str:
    creds = load_google_credentials(account)
    require_scopes(creds, account, CALENDAR_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


def list_calendars(token: str) -> list[dict]:
    """Return [{'id', 'summary', 'access_role'}] for non-skipped calendars.

    access_role comes straight from Google's calendarList API and tells us
    what we can see in each calendar. The two we care about distinguishing
    are `freeBusyReader` (only "Busy" blocks, no titles) vs everything else
    (reader/writer/owner — full event details).
    """
    data = gogcli.run(token, "calendar", "calendars")
    out = []
    for c in data.get("calendars", []):
        summary = c.get("summary") or c.get("id", "")
        if summary in SKIP_CALENDARS:
            continue
        out.append({
            "id": c["id"],
            "summary": summary,
            "access_role": c.get("accessRole") or c.get("access_role") or "reader",
        })
    return out


def fetch_events(token: str, days: int, calendars: list[dict]) -> list[dict]:
    """Fetch events across all given calendars in a single gogcli call."""
    if not calendars:
        return []
    cal_ids = ",".join(c["id"] for c in calendars)
    meta_by_id = {c["id"]: c for c in calendars}
    data = gogcli.run(
        token, "calendar", "events",
        f"--calendars={cal_ids}",
        f"--days={days}",
        "--all-pages",
        "--max=50",
    )
    return [normalize_event(e, meta_by_id) for e in data.get("events", [])]


def normalize_event(raw: dict, meta_by_id: dict[str, dict]) -> dict:
    """Map a gogcli event into Homer's existing calendar-event shape, with
    access_role + is_opaque tags carried through from the parent calendar."""
    start = raw.get("start", {})
    if "dateTime" in start:
        start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(LOCAL_TZ)
        event_date = start_dt.date().isoformat()
        time_str = start_dt.strftime("%-I:%M %p")
        is_all_day = False
    else:
        event_date = start.get("date", "")
        time_str = "all-day"
        is_all_day = True
    cal_id = raw.get("CalendarID", "")
    cal_meta = meta_by_id.get(cal_id, {})
    access_role = cal_meta.get("access_role", "reader")
    title = raw.get("summary", "(no title)")
    # An event is opaque (no title visible) if either:
    #   - the parent calendar grants only free/busy access, or
    #   - the title is the literal "Busy" Google substitutes for restricted shares
    is_opaque = access_role in OPAQUE_ACCESS_ROLES or title == "Busy"
    return {
        "title": title,
        "date": event_date,
        "time": time_str,
        "is_all_day": is_all_day,
        "location": raw.get("location", ""),
        "description": (raw.get("description") or "")[:300].strip(),
        "calendar": cal_meta.get("summary", cal_id),
        "event_id": raw.get("id", ""),
        "calendar_id": cal_id,
        "access_role": access_role,
        "is_opaque": is_opaque,
    }


def dedupe_and_sort(events: list[dict]) -> list[dict]:
    """Dedupe across calendars by (title, date, time); sort by date then time."""
    out, seen = [], set()
    for e in events:
        key = (e["title"], e["date"], e["time"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: (x["date"], "00:00" if x["is_all_day"] else x["time"]))
    return out


def split_today_vs_week(events: list[dict], today_str: str) -> tuple[list[dict], list[dict]]:
    today_events = [e for e in events if e["date"] == today_str]
    week_events = [e for e in events if e["date"] > today_str]
    return today_events, week_events


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch upcoming calendar events.")
    parser.add_argument("--days", type=int, default=7, help="Days ahead to fetch (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Pretty-print instead of JSON")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT,
                        help=f"Google account to use (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    # Early SKIP for tenants who haven't connected Google. Heartbeat handler
    # treats `SKIP:` as "no message this turn" — same pattern as morning_briefing.
    if not has_google_token(args.account):
        print(f"SKIP: Google not connected for account '{args.account}' — connect to enable calendar fetch.")
        return

    try:
        token = get_access_token(args.account)
        calendars = list_calendars(token)
        events = fetch_events(token, args.days, calendars)
    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    events = dedupe_and_sort(events)
    today_str = datetime.now(LOCAL_TZ).date().isoformat()
    today_events, week_events = split_today_vs_week(events, today_str)
    output = {"today": today_str, "today_events": today_events, "week_events": week_events}

    if args.dry_run:
        print(f"=== Daily Briefing Preview: {today_str} ===\n")
        if today_events:
            print(f"Today ({len(today_events)} event(s)):")
            for e in today_events:
                loc = f"  @ {e['location']}" if e["location"] else ""
                print(f"  {e['time']:>10}  {e['title']}{loc}")
        else:
            print("Today: no events")
        if week_events:
            print(f"\nNext {args.days} days ({len(week_events)} event(s)):")
            for e in week_events:
                loc = f"  @ {e['location']}" if e["location"] else ""
                print(f"  {e['date']}  {e['time']:>10}  {e['title']}{loc}")
        else:
            print(f"\nNext {args.days} days: nothing scheduled")
        return

    print(json.dumps(output))


if __name__ == "__main__":
    main()

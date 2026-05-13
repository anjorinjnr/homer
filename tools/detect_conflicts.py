#!/usr/bin/env python3
"""
detect_conflicts.py — Find overlapping timed events.

Pure transform: a list of calendar events in, a list of conflict pairs
out. No fetching — the caller is expected to gather events first (typically
via `calendar_fetch.py --account <each>` fanned out across linked accounts
and concatenated). This lets the morning-brief prompt orchestrate the
fan-out while a single deterministic tool finds the clashes.

Each detected conflict carries enough metadata for the agent to render a
useful heads-up:
  - both event views (title, calendar, account, opacity, location)
  - the overlap window in minutes
  - cross_account: did the clash span linked accounts?
  - both_opaque: are both sides free/busy-only blocks the user can't peek into?

Cross-account dedup runs first: a shared event surfaced on two linked
accounts (e.g. a family calendar visible to both work and personal) is
collapsed to a single entry so it doesn't phantom-conflict with itself.

Usage:
    cat events.json | python tools/detect_conflicts.py
    python tools/detect_conflicts.py --events-file events.json
    python tools/detect_conflicts.py --events-file events.json --date 2026-05-13
"""

import argparse
import json
import sys
from pathlib import Path


def cross_account_dedup_key(e: dict) -> tuple:
    """Best-effort identity for "this is the same calendar event, just
    surfaced on multiple linked accounts." A shared family calendar
    visible to both work and personal accounts would otherwise produce
    two entries that conflict-detection then flags as a cross-account
    overlap with itself.

    event_id is the strongest signal when present (Google Calendar IDs
    are stable across the calendars an event appears on). Falls back to
    (date, start_minutes, end_minutes, normalized title) so single-cal
    payloads without ids still dedupe reasonably.
    """
    eid = e.get("event_id")
    if eid:
        return ("id", eid)
    return (
        "shape",
        e.get("date") or "",
        e.get("start_minutes"),
        e.get("end_minutes"),
        (e.get("title") or "").strip().lower(),
    )


def dedup_preserve_order(events: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in events:
        key = cross_account_dedup_key(e)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _conflict_event_view(e: dict) -> dict:
    """Trim a full event dict to just the fields a conflict entry needs.
    Keeps the structure deterministic and small for the LLM. `location`
    is high-signal for "you're in two places" framing; `event_id` lets
    the agent dedupe across multiple briefing renders for the same day."""
    return {
        "title": e.get("title") or "(no title)",
        "time": e.get("time") or "",
        "end_time": e.get("end_time") or "",
        "location": e.get("location") or "",
        "calendar": e.get("calendar") or "",
        "account": e.get("account") or "",
        "event_id": e.get("event_id") or "",
        "is_opaque": bool(e.get("is_opaque")),
    }


def detect_conflicts(timed_events: list[dict]) -> list[dict]:
    """Find pairs of overlapping timed events.

    Two events conflict if they share the same date and their [start, end)
    intervals overlap. Events without resolvable start/end times (all-day,
    legacy payloads pre-end_minutes) are skipped — they can't physically
    double-book the user the way two timed events can.
    """
    deduped = dedup_preserve_order(timed_events)
    candidates = [
        e for e in deduped
        if not e.get("is_all_day")
        and isinstance(e.get("start_minutes"), int)
        and isinstance(e.get("end_minutes"), int)
        and e["end_minutes"] > e["start_minutes"]
    ]
    candidates.sort(key=lambda e: (e.get("date") or "", e["start_minutes"]))

    conflicts: list[dict] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if (a.get("date") or "") != (b.get("date") or ""):
                break  # sorted by date; further b's are later days
            if b["start_minutes"] >= a["end_minutes"]:
                # Sorted by start_minutes within a date; once b starts after
                # a ends, every later b' (b'.start >= b.start) also misses a.
                break
            overlap_start = max(a["start_minutes"], b["start_minutes"])
            overlap_end = min(a["end_minutes"], b["end_minutes"])
            conflicts.append({
                "date": a.get("date") or "",
                "overlap_start_minutes": overlap_start,
                "overlap_end_minutes": overlap_end,
                "event_a": _conflict_event_view(a),
                "event_b": _conflict_event_view(b),
                "cross_account": (a.get("account") or "") != (b.get("account") or ""),
                "both_opaque": bool(a.get("is_opaque") and b.get("is_opaque")),
            })
    return conflicts


def _read_events(path: str | None) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    if not raw.strip():
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("input must be a JSON array of calendar events")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Find overlapping timed events.")
    parser.add_argument("--events-file", dest="events_file",
                        help="Path to a JSON file containing the events array")
    parser.add_argument("--date",
                        help="Restrict input to this ISO date before detection")
    args = parser.parse_args()

    try:
        events = _read_events(args.events_file)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)

    if args.date:
        events = [e for e in events if (e.get("date") or "") == args.date]

    print(json.dumps({"conflicts": detect_conflicts(events)}, indent=2))


if __name__ == "__main__":
    main()

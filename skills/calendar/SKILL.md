---
name: calendar
description: Read, create, and edit Google Calendar events, including recurring/repeating events. Use for schedule questions and adding one-time or recurring events from invites or user requests.
metadata: {"nanobot":{"always":false,"emoji":"📅"}}
---

# Calendar Skill

Homer reads and writes Google Calendar events via `calendar_fetch.py` and `calendar_add.py`.

## Account selection

Both tools accept `--account <name>` (default: `primary`, the household calendar).
Pass `--account <name>` (e.g. `--account personal`) when the user asks about a
different calendar account. Note: `--account` selects the Google login;
`--calendar` selects which calendar within that account (e.g. "primary",
"Work", or a calendar ID).

## Rules

- Always confirm event details with the user before creating or editing.
- For image-based invites: extract title, date, time, and location from the image, propose the details, and wait for confirmation before running.
- TIMEZONE: pass times exactly as the user stated ("3:30 PM", "15:30") — do NOT convert to UTC. The script applies Eastern timezone automatically.

## Reading events

```
{HOMER_VENV} {HOMER_TOOLS}/calendar_fetch.py
{HOMER_VENV} {HOMER_TOOLS}/calendar_fetch.py --days 3
{HOMER_VENV} {HOMER_TOOLS}/calendar_fetch.py --account personal
```
Outputs today's events and the next N days as JSON. Each event includes `event_id` and `calendar_id`.

## Creating an event

```
{HOMER_VENV} {HOMER_TOOLS}/calendar_add.py \
  --title "EVENT" --date YYYY-MM-DD \
  [--time "H:MM AM/PM or HH:MM"] [--end-time "H:MM AM/PM or HH:MM"] \
  [--duration MINUTES] [--location "..."] [--description "..."] \
  [--recur daily|weekly|monthly]
```
Omit `--time` for an all-day event. `--duration` defaults to 60 min if `--end-time` is not provided.

Use `--recur` for recurring events: `daily`, `weekly`, or `monthly`. For custom schedules (e.g. every Mon/Wed/Fri) pass a raw RRULE string: `--recur "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"`.

## Editing an event

First search to get `event_id` and `calendar_id`:
```
{HOMER_VENV} {HOMER_TOOLS}/calendar_add.py --search --title "Jake" --date YYYY-MM-DD
```
Then edit using the IDs from the search output:
```
{HOMER_VENV} {HOMER_TOOLS}/calendar_add.py \
  --edit --event-id ID --calendar CALENDAR_ID \
  --title "EVENT" --date YYYY-MM-DD [--time "..."] [--end-time "..."] [--location "..."]
```
Always use `calendar_id` from search output — never hardcode "primary".

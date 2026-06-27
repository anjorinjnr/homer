---
name: breeze-roster
description: Volunteer scheduling and roster management via BreezeRoster — look up teams, check schedules, assign volunteers, manage songs, and trigger outreach. Org-mode only.
metadata: {"nanobot":{"always":false,"emoji":"📋"}}
---

# BreezeRoster Skill

Volunteer scheduling for the org. Use this skill when anyone asks about team rosters, upcoming service assignments, availability, scheduling, or worship songs.

All commands output JSON. Always check for an `"error"` key before using the result; surface the error message to the user if present.

## Teams

```
# List all teams in the org
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-teams

# Get details for one team
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-team <team_id>

# List volunteers on a team's roster
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-roster --team <team_id>

# List admin/viewer members of a team
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-members --team <team_id>
```

## Volunteers

```
# List all org volunteers
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-volunteers

# Search volunteers by name (optionally scoped to a team)
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --search-volunteers "Jane"
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --search-volunteers "Jane" --team <team_id>

# Get one volunteer with memberships and eligibility
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-volunteer <volunteer_id>
```

## Schedules

```
# List all schedules
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-schedules

# Get a schedule with its instances and slot grid
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-schedule <schedule_id>

# AI-generate assignments for a schedule
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --generate-schedule <schedule_id>

# Publish (make visible to volunteers)
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --publish-schedule <schedule_id>

# Revert to draft
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --unpublish-schedule <schedule_id>
```

## Events and Instances

An *event template* is a recurring service (e.g. "Sunday Worship"). An *instance* is one occurrence with its own date and slot grid.

```
# List event templates for a team
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-events --team <team_id>

# Get one event template with upcoming instances
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-event <event_id> --team <team_id>

# List all instances of an event
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-instances <event_id> --team <team_id>

# Get one event instance with its slot grid
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-instance <instance_id>
```

## Slot Assignment

```
# Assign a volunteer to a slot
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --assign-slot <slot_id> --volunteer <volunteer_id>

# Unassign a slot
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --unassign-slot <slot_id>
```

## Availability

```
# Get the availability matrix for a schedule
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-availability --schedule <schedule_id>

# Send bulk availability outreach (SMS/email) for a schedule
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --send-outreach --schedule <schedule_id>
```

Confirm with the user before sending bulk outreach — it messages all volunteers.

## Songs (Worship)

```
# List the team's song library
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-songs --team <team_id>

# List songs assigned to a specific service instance
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --event-songs <instance_id> --team <team_id>
```

## Scheduling Rules

```
# List the org's active scheduling rules
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --list-rules

# Parse a natural-language rule into structured form (preview before saving)
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --parse-rule "Vocalists can't lead two Sundays in a row"
```

## Org

```
# Read org settings
{HOMER_VENV} {HOMER_TOOLS}/breeze_roster.py --get-org
```

---

## Workflows

### "Who's scheduled for Sunday?"
1. `--list-events --team <worship_team_id>` to find the Sunday Worship event template.
2. `--list-instances <event_id> --team <team_id>` to get upcoming instances; pick the soonest.
3. `--get-instance <instance_id>` to see the slot grid with volunteer assignments.
4. Present assignments by role/category clearly. Note any unfilled slots.

### "Who's available / who hasn't responded?"
1. `--list-schedules` to find the relevant schedule.
2. `--get-availability --schedule <schedule_id>` to get the matrix.
3. Report confirmed available, unavailable, and no-response counts.

### "Generate and publish the schedule"
1. Confirm with user which schedule period.
2. `--generate-schedule <id>` — present the summary output to the user for review.
3. Only after user confirms: `--publish-schedule <id>`.

### "Add a volunteer to a slot"
1. `--search-volunteers "<name>"` to get `volunteer_id`.
2. `--get-schedule <id>` or `--get-instance <id>` to find the `slot_id`.
3. `--assign-slot <slot_id> --volunteer <volunteer_id>`.
4. Confirm the assignment with the user.

### "What songs are we doing Sunday?"
1. Find the Sunday instance (see "Who's scheduled for Sunday?" steps 1–2).
2. `--event-songs <instance_id> --team <team_id>`.
3. List songs with their assigned roles/sections.

---

## Safety

- **Confirm before mutating**: generate, publish, send-outreach, assign-slot all change state visible to volunteers. Always confirm intent before executing.
- **Bulk outreach**: "send-outreach" messages everyone on the roster. Confirm once and be explicit about who will be contacted.
- **Error key**: every response may contain `"error"`. Check before presenting data; surface the message verbatim if set.

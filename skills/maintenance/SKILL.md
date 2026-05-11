---
name: maintenance
description: Track home maintenance tasks, service providers, appliances, and projects. Operational tracking layer on top of property.md.
metadata: {"nanobot":{"always":false,"emoji":"🔧"}}
---

# Home Maintenance Tracker

## Rules

- **property.md** = static home facts (HVAC model, roof material, square footage). Update via `context_updater.py`.
- **maintenance.db** = operational tracking (task schedules, completion history, vendor contacts, appliance warranties, project progress). Managed exclusively via `maintenance.py`.
- When the user mentions a permanent home fact (e.g. "our water heater is a Rheem"), update property.md via context_updater.py.
- When the user mentions a recurring task, service visit, or project milestone, use maintenance.py.
- Always use **YYYY-MM-DD** format for all dates passed to maintenance.py.

## maintenance.py — CLI Reference

All commands output JSON. Errors return `{"error": "..."}` with exit code 1.

### Maintenance Tasks

```bash
# Add a recurring task (next_due computed from today + frequency)
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --add-task --name "Replace HVAC filter" --system HVAC --frequency 90 --notes "Use MERV-13 20x25x1"

# Log a completion (advances next_due by frequency_days)
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --complete-task --task-id 1 --done-by "DIY" --cost 25.00 --notes "Used Filtrete brand"

# Complete with a specific date
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --complete-task --task-id 1 --date 2026-03-15

# List all tasks
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-tasks

# Filter by system
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-tasks --system HVAC

# Show only overdue or due within 7 days
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-tasks --due

# View completion history for a task
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --task-history --task-id 1
```

### Service Providers

```bash
# Add a provider
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --add-provider --name "Malcolm HVAC" --specialty HVAC --phone "770-555-1234" --rating 5

# List all providers
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-providers

# Filter by specialty
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-providers --specialty Plumbing

# Update a provider
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --update-provider --provider-id 1 --rating 4 --notes "Raised prices"

# Remove a provider
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --remove-provider --provider-id 1
```

### Appliances

```bash
# Add an appliance
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --add-appliance --name "Refrigerator" --brand "Samsung" --model "RF28" --serial "SN12345" --install-date "2024-06-15" --warranty-until "2029-06-15" --location "Kitchen"

# List all appliances
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-appliances

# Filter by location
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-appliances --location Kitchen

# Update an appliance
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --update-appliance --appliance-id 1 --warranty-until "2030-06-15"

# Remove an appliance
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --remove-appliance --appliance-id 1
```

### Home Projects

```bash
# Add a project
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --add-project --name "Fence replacement" --description "Replace rotting sections" --budget 5000

# List projects (all or by status)
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-projects
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --list-projects --status active

# Update a project
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --update-project --project-id 1 --status completed --actual-cost 4800 --completed-date 2026-04-01

# Add a checklist item
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --add-project-item --project-id 1 --description "Get quotes from 3 vendors"

# Check off an item (substring match)
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --check-project-item --project-id 1 --item "quotes"

# Full project status with items
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --project-status --project-id 1
```

### Dashboard

```bash
# Overview: overdue tasks, upcoming (7d), active projects, warranty expirations (90d)
{HOMER_VENV} {HOMER_TOOLS}/maintenance.py --dashboard
```

## Workflows

### Completing maintenance
1. User says "I just replaced the HVAC filter" or "pool guy came today"
2. Find the matching task with `--list-tasks --system <system>`
3. Log completion with `--complete-task --task-id N --done-by "..." --cost ...`
4. Confirm to user with the next due date

### Checking what's due
1. User asks "what maintenance is coming up?" or "anything overdue?"
2. Run `--list-tasks --due` for overdue + next-7-days view
3. Or run `--dashboard` for the full overview including projects and warranties
4. Present naturally: "Your HVAC filter is due Thursday. Pool cleaning is overdue by 3 days."

### Adding a new vendor
1. User says "our plumber is Mike at 770-555-0000, he's great"
2. Add with `--add-provider --name "Mike" --specialty Plumbing --phone "770-555-0000" --rating 5`
3. Confirm: "Got it — Mike added as your plumber."

### Tracking a home project
1. User says "we're replacing the fence, budget is $5000"
2. Create project: `--add-project --name "Fence replacement" --budget 5000`
3. Add checklist items: `--add-project-item --project-id N --description "Get quotes"`
4. As work progresses, check items off: `--check-project-item --project-id N --item "quotes"`
5. When done: `--update-project --project-id N --status completed --actual-cost 4800`

## Examples

**User:** "I just had the HVAC serviced, Malcolm charged $150"
**Homer:** Logs completion, confirms next service date: "Logged! HVAC service done by Malcolm for $150. Next one is due October 1."

**User:** "What maintenance do I have coming up?"
**Homer:** Runs `--list-tasks --due`, presents: "You have 2 things this week: HVAC filter change (due tomorrow) and gutter cleaning (due Saturday). Pool treatment is 5 days overdue."

**User:** "Our dishwasher is a Bosch 500 series, installed last year, warranty runs until 2030"
**Homer:** Adds the appliance with `--add-appliance`, confirms: "Added your Bosch 500 dishwasher. I'll flag you when the warranty is getting close to expiring."

**User:** "We need to redo the deck. Budget around $8000."
**Homer:** Creates the project, asks about checklist items: "Started tracking the deck project with an $8K budget. Want me to add some checklist items like getting quotes and picking materials?"

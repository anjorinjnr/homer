---
name: skyvern
description: Use a real browser to complete web tasks that have no API — buying tickets, filling forms, checking sites that require login or navigation. Powered by Skyvern Cloud.
metadata: {"nanobot":{"always":false,"emoji":"🌐"}}
---

# Skyvern Skill

Homer uses Skyvern to automate browser tasks: purchasing tickets, submitting forms, navigating sites that don't have an API. Tasks run **asynchronously** — submit and get a run_id, then check the result later.

## Rules

- Always confirm with the user before purchasing anything or submitting a form with real-world consequences.
- Never pass sensitive fields (email, names, dates, quantities) in --prompt — they appear in logs. Use --data-file instead.
- After submitting, tell the user the task is running and you'll confirm when it's done.
- If the task fails, report the failure_reason and ask whether to retry.

## Submitting a task

Write form data to a tmp file first (keeps sensitive fields out of logs), then submit:

```
# 1. Write data file
write_file path={HOMER_WORKSPACE}/tmp/skyvern_zoo.json content={{"date": "2026-04-05", "adult_qty": 2, "email": "household@example.com"}}

# 2. Submit task
{HOMER_VENV} {HOMER_TOOLS}/skyvern_task.py \
  --prompt "Buy 2 adult tickets at Zoo Atlanta for April 5 2026" \
  --url "https://www.zooatlanta.org" \
  --data-file {HOMER_WORKSPACE}/tmp/skyvern_zoo.json
```

**Output:**
```json
{{"status": "submitted", "run_id": "tsk_v2_...", "app_url": "https://app.skyvern.com/runs/..."}}
```

Save the `run_id`. Then immediately add a check task to the heartbeat:

```
# 3. Add check task (replace <run_id>, <YYYY-MM-DD HH:MM> with ~5 min from now, <recipient> with caller's chat_id:channel)
{HOMER_VENV} {HOMER_TOOLS}/tasks_update.py --add \
  --desc "Skyvern: check <run_id>" \
  --schedule "<YYYY-MM-DD HH:MM>" \
  --recur "5 minutes" \
  --until "<YYYY-MM-DD HH:MM 24h from now>" \
  --recipients "<recipient>"
```

Homer checks on every heartbeat cycle. When the result arrives it notifies the user and removes the task.

## Checking a task result

```
{HOMER_VENV} {HOMER_TOOLS}/skyvern_task.py --check tsk_v2_abc123
```

**Output (completed):**
```json
{{"status": "completed", "run_id": "...", "output": {{...}}, "app_url": "..."}}
```

**Output (still running):**
```json
{{"status": "running", "run_id": "..."}}
```

## When to use

- Buying tickets (zoo, museum, events) when there is no API or the site requires browser interaction
- Filling out forms (service requests, HOA portals, utility sites)
- Checking prices or availability on sites that block scraping

## When NOT to use

- Tasks Plaid, Google APIs, or other existing tools already handle
- Simple web lookups — use the research or web_search skill instead

## Example prompts

```
# Check ticket prices (no purchase — no confirmation needed):
--prompt "Go to the Zoo Atlanta website and tell me the current adult and child ticket prices"
--url "https://www.zooatlanta.org"

# Buy tickets (always confirm with user first — write data file first, then submit):
write_file path={HOMER_WORKSPACE}/tmp/skyvern_zoo.json content={{"adult_qty": 2, "child_qty": 1, "date": "2026-04-05", "email": "household@example.com"}}
--prompt "Buy 2 adult and 1 child ticket at Zoo Atlanta for April 5 2026"
--url "https://www.zooatlanta.org"
--data-file {HOMER_WORKSPACE}/tmp/skyvern_zoo.json
```

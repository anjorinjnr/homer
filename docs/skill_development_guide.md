# Homer Skill Development Guide

How to create, test, and evaluate new skills for Homer. This guide captures the established architecture so future work is consistent and fast.

---

## Architecture Overview

A Homer skill has 4 components:

```
tools/<name>.py              # SQLite-backed CLI tool (the engine)
skills/<name>/SKILL.md       # Nanobot skill doc (how Homer uses the tool)
tests/test_<name>.py         # Unit tests (pytest)
agent/AGENTS.md              # Whitelist entry (allows Homer to exec the tool)
```

Required 5th component — end-to-end quality validation:

```
tests/simulation/flows/<name>.yaml   # Conversation simulation flow
```

**All 5 components are required.** Unit tests verify the tool works. Simulations verify Homer uses the tool correctly in realistic conversations. A skill is not shippable without both.

### How it all connects

```
User (Telegram/WhatsApp)
  → nanobot AgentLoop
    → reads SKILL.md to know what tools are available
    → calls exec tool: python tools/<name>.py --action --args
      → tool reads/writes SQLite DB
      → tool returns JSON to stdout
    → Homer formats the JSON into a natural response
    → sends response back to user
```

### File flow during deployment

```
skills/<name>/SKILL.md
  → build_context.py copies to workspace/skills/<name>/SKILL.md
    → nanobot loads on every conversation turn

agent/AGENTS.md
  → build_context.py template-substitutes {HOMER_VENV}, {HOMER_TOOLS}
    → nanobot uses this as the exec whitelist
```

---

## Step 1: Design the Tool (`tools/<name>.py`)

### Boilerplate

Every tool starts with the same structure. Copy this skeleton:

```python
#!/usr/bin/env python3
"""<name>.py — <one-line description>.

All output is JSON. DB location: state/<name>.db (inside nanobot workspace)
or HOMER_<NAME>_DB env var.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = (
    REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "<name>.db"
)


def get_db_path() -> Path:
    """Return the DB path. Override with HOMER_<NAME>_DB."""
    if env := os.environ.get("HOMER_<NAME>_DB"):
        return Path(env)
    if workspace := os.environ.get("HOMER_WORKSPACE"):
        return Path(workspace) / "state" / "<name>.db"
    return DEFAULT_DB_PATH


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection and ensure tables exist."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ...
    """)
```

### Key rules

1. **DB path**: always `context/.nanobot_workspace/state/<name>.db` with `HOMER_<NAME>_DB` env var override
2. **HOMER_WORKSPACE fallback**: also check `HOMER_WORKSPACE` env var (set in containerized deployments)
3. **WAL mode + foreign keys**: always enable both in `get_conn()`
4. **Auto-create tables**: `_create_tables()` runs on every connection — idempotent schema
5. **JSON output**: success → `json.dumps(result, indent=2)` to stdout; error → `{"error": "..."}` + `sys.exit(1)`
6. **Argparse**: use mutually exclusive group for top-level actions (`--add-X`, `--list-X`, `--update-X`, `--remove-X`)
7. **Date format**: always ISO `YYYY-MM-DD`. Validate with `datetime.strptime(d, "%Y-%m-%d").date()` and return a clear error on invalid dates
8. **No external API calls** from the tool itself — tools are pure data layers. API integrations go in separate tools or the skill doc orchestrates existing tools (e.g., `calendar_add.py`)

### CLI pattern

```python
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="...")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add-thing", action="store_true")
    group.add_argument("--list-things", action="store_true")
    # ... more actions

    parser.add_argument("--name", help="...")
    parser.add_argument("--id", type=int, help="...")

    args = parser.parse_args(argv)

    if args.add_thing:
        if not args.name:
            parser.error("--add-thing requires --name")
        result = add_thing(args.name)
        print(json.dumps(result, indent=2))
    elif args.list_things:
        items = list_things()
        print(json.dumps(items, indent=2))


if __name__ == "__main__":
    main()
```

**Important**: `main()` accepts an optional `argv` parameter. This lets tests call `main(["--add-thing", "--name", "foo"])` without monkeypatching `sys.argv`.

### Library functions

Keep business logic in standalone functions that tests can call directly:

```python
def add_thing(name: str, notes: str = "") -> dict:
    """Add a thing. Returns dict with status and id."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO things (name, notes) VALUES (?, ?)",
        (name, notes),
    )
    conn.commit()
    return {"status": "created", "thing_id": cur.lastrowid, "name": name}
```

### Error handling

```python
def remove_thing(thing_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM things WHERE thing_id = ?", (thing_id,)).fetchone()
    if not row:
        return {"error": f"Thing {thing_id} not found"}
    conn.execute("DELETE FROM things WHERE thing_id = ?", (thing_id,))
    conn.commit()
    return {"status": "removed", "thing_id": thing_id}
```

In `main()`, check for error and exit:

```python
result = remove_thing(args.id)
print(json.dumps(result, indent=2))
if "error" in result:
    sys.exit(1)
```

### Name matching

When users reference records by name (not ID), use case-insensitive matching with ambiguity protection:

```python
def _resolve_by_name(conn, name: str) -> Optional[dict]:
    """Try exact match first, then unique partial match."""
    # Exact (case-insensitive)
    row = conn.execute(
        "SELECT * FROM things WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if row:
        return dict(row)
    # Partial — only if unambiguous
    rows = conn.execute(
        "SELECT * FROM things WHERE LOWER(name) LIKE LOWER(?)", (f"%{name}%",)
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    if len(rows) > 1:
        return None  # ambiguous — caller should ask user to be more specific
    return None
```

### Existing reference tools

Study these files as patterns:
- `tools/maintenance.py` — 6-table tool with dashboard, task completion tracking, projects
- `tools/meal_plan.py` — recipe library, grocery list generation with pantry dedup
- `tools/health_records.py` — family member profiles, visit logging, name resolution
- `tools/event_store.py` — library-only module (no CLI, imported by other tools)
- `tools/pending_reply.py` — simple 1-table tool with clean CLI

---

## Step 2: Write the Skill Doc (`skills/<name>/SKILL.md`)

### Frontmatter

```yaml
---
name: <name>
description: <one-line description for nanobot skill matching>
metadata: {"nanobot":{"always":false,"emoji":"🎯"}}
---
```

- `always: false` means the skill is loaded only when relevant (most skills)
- `always: true` means it's injected into every conversation turn (rarely needed)
- `emoji`: a single emoji representing the skill — displayed on the portal Skills page. Always include one.

### Required sections

```markdown
# <Skill Title>

## Rules
- What this skill manages vs what belongs in context files
- Date format requirements
- When to use this tool vs context_updater.py
- Any privacy/security notes

## <tool>.py — CLI Reference
All commands output JSON. Errors return `{"error": "..."}` with exit code 1.

### <Feature Group 1>
\`\`\`bash
{HOMER_VENV} {HOMER_TOOLS}/<tool>.py --action --args
\`\`\`

### <Feature Group 2>
...

## Workflows
### <Workflow Name>
1. Step-by-step instructions for common scenarios
2. Include which tool commands to run in order
3. Note what to confirm with the user before acting

## Examples
**User:** "natural language request"
**Homer:** natural response showing how tool output is formatted for the user
```

### Template variables

Always use these placeholders — build_context.py substitutes them at deploy time:
- `{HOMER_VENV}` → path to .venv/bin/python
- `{HOMER_TOOLS}` → path to tools/ directory
- `{HOMER_HOME}` → repo root
- `{HOMER_WORKSPACE}` → nanobot workspace path
- `{PRIMARY_USER}` → admin user's name

### Design principles

1. **Skill doc = instructions for Homer, not documentation for humans**. Write it as if briefing Homer on how to handle requests.
2. **Workflows over reference**. The CLI reference is needed, but workflows are what Homer actually follows during conversation.
3. **Static facts vs operational data**. Be explicit about what goes in context files (via `context_updater.py`) vs what goes in the skill's SQLite DB. Rule of thumb: if it rarely changes and should be in Homer's always-loaded context, it's a context file fact. If it changes frequently or has history, it's DB data.
4. **Examples ground behavior**. Include 3-4 realistic conversation exchanges showing the expected tone, tool usage, and response format.

### Existing reference skills

- `skills/maintenance/SKILL.md` — clean rules/CLI/workflows/examples pattern
- `skills/finance/SKILL.md` — complex skill with multiple tools, budget flows, alert behavior
- `skills/calendar/SKILL.md` — read/write integration with external API
- `skills/weather/SKILL.md` — simple curl-based skill (no SQLite)

---

## Step 3: Whitelist in AGENTS.md

Add to the "Approved Scripts" section in `agent/AGENTS.md` (before the "Any other exec call" line):

```
- {HOMER_VENV} {HOMER_TOOLS}/<tool>.py [args]
```

Without this, Homer cannot execute the tool — the exec whitelist is enforced strictly.

---

## Step 3b: Register env vars in the hosted config (if needed)

If your tool reads **any env vars beyond `HOMER_*` and the standard LLM/channel keys**, you must add them to `config/config.hosted.json.template` under `tools.exec.allowedEnvKeys`:

```json
"allowedEnvKeys": [
  ...existing keys...,
  "MY_API_KEY",
  "MY_SERVICE_URL"
]
```

**Why:** nanobot strips subprocess env before running exec'd tools. Only keys listed in `allowedEnvKeys` are forwarded. The tool runs successfully in local dev (where env is inherited from the shell) but silently fails in production containers until the key is whitelisted. This is the most common source of "works locally, breaks in prod" bugs for new skills.

**Deployment-level secrets** — if your deployment has tenant-shared secrets (a database the deployment owns, third-party API tokens shared across users), wire them through your provisioning layer's env-injection. The pattern is to read the secret from the host/deployment env at provision time and emit it into each tenant container's per-instance `.env`. For example:

```python
# MyService — pass through from deployment env to tenant env
my_key = os.environ.get("MY_SERVICE_KEY", "")
if my_key:
    lines.append(_env_line("MY_SERVICE_KEY", my_key))
```

---

## Step 3c: Register the capability in capabilities.yaml

Every skill **must** have an entry in `config/capabilities.yaml`. `build_context.py` uses this to control which skill docs, tool whitelist entries, and heartbeat tasks are included. An unregistered capability is treated as unknown and stripped — the AGENTS.md whitelist entry and the skill itself silently disappear from the container.

```yaml
capabilities:
  my_feature:
    description: >
      One-line description shown on the portal Skills page.
    skills:
      - my-skill-dir-name
    tools:
      - my_tool.py
    requires_env:          # optional — vars checked by HOMER_VERIFY_CAPABILITIES=1
      - MY_API_KEY
```

**`default_enabled`** controls the ship default:
- Omit (or set `true`) — on for all households by default. Use for skills that work without extra credentials.
- Set `false` — off until the household explicitly opts in via `features.yaml`. Use when third-party credentials are required or the feature is opt-in.

Households can always override the default with their own `context/user_context/features.yaml`.

---

## Step 4: Write Unit Tests (`tests/test_<name>.py`)

### Test skeleton

```python
"""Tests for <name>.py."""

import json
import sys
from datetime import date, timedelta
from io import StringIO

import pytest

import tools.<name> as mod


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point <name> at a temp DB for every test."""
    db_path = tmp_path / "<name>.db"
    monkeypatch.setenv("HOMER_<NAME>_DB", str(db_path))
    return db_path


def _today() -> str:
    return date.today().isoformat()


def _days_from_now(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()
```

### Testing patterns

**Direct library function calls** (preferred for most tests):

```python
class TestThings:
    def test_add_thing(self):
        result = mod.add_thing("Widget")
        assert result["status"] == "created"
        assert result["thing_id"] == 1

    def test_add_thing_duplicate(self):
        mod.add_thing("Widget")
        result = mod.add_thing("Widget")
        assert "error" in result  # or assert it succeeds — depends on schema

    def test_list_things_empty(self):
        result = mod.list_things()
        assert result == []

    def test_remove_nonexistent(self):
        result = mod.remove_thing(999)
        assert "error" in result
```

**CLI integration tests** (test the full argparse → JSON path):

```python
def _capture_main(argv: list[str]) -> dict:
    """Run main() with given argv and return parsed JSON output."""
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        mod.main(argv)
    finally:
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
    return json.loads(output)


class TestCLI:
    def test_add_via_cli(self):
        result = _capture_main(["--add-thing", "--name", "Widget"])
        assert result["status"] == "created"

    def test_missing_required_arg(self):
        with pytest.raises(SystemExit):
            _capture_main(["--add-thing"])  # missing --name
```

### What to test

For each entity type (table), test:
- **Create**: basic, with all optional fields, missing required field → error
- **Read**: list all, list with filters, get single by ID, empty result
- **Update**: change fields, update nonexistent → error
- **Delete**: remove existing, remove nonexistent → error, cascade behavior
- **Dashboard/summary**: aggregation across entities, empty state

### Test organization

- One test class per entity/feature group: `TestThings`, `TestProjects`, `TestDashboard`
- Name tests descriptively: `test_add_thing_with_notes`, `test_remove_nonexistent_returns_error`
- ~10-15 tests per entity, ~50-100 tests per skill

### Running tests

```bash
# Single skill
.venv/bin/python -m pytest tests/test_<name>.py -v

# All tests
.venv/bin/python -m pytest tests/ -v
```

---

## Step 5: Evaluate with Conversation Simulations (Required)

Unit tests verify the tool works. **Simulations verify Homer uses the tool correctly in realistic conversations.** They run through a real nanobot AgentLoop with actual LLM calls. This step is not optional — it catches problems unit tests cannot: bad tool selection, missing context, poor response formatting, and broken conversation flow.

### Simulation architecture

```
tests/simulation/
├── harness.py              # SimulationHarness — isolated AgentLoop setup
├── runner.py               # CLI runner, generates reports
├── fixtures/
│   └── personas.yaml       # Actor definitions (Alex, Sam, guests)
├── flows/
│   ├── meal_planning.yaml  # Skill simulation flows
│   ├── home_maintenance.yaml
│   ├── health_records.yaml
│   ├── mtb_trip.yaml       # Event management flows
│   ├── birthday_followup.yaml
│   ├── guest_rsvp.yaml     # Guest agent RSVP handling (decline/confirm/maybe)
│   ├── email_send.yaml     # Email send/draft/reply with mocked Gmail API
│   └── whatsapp_lid_identity.yaml  # LID identity resolution
└── runs/                   # Generated output (gitignored)
    └── <run_id>_<flow>_<model>/
        ├── trajectory.json # Full beat-by-beat data
        ├── transcript.md   # Human-readable conversation
        └── report.html     # Interactive HTML report
```

### Flow YAML structure

```yaml
name: "Human-readable flow name"
event_id: "sim_<unique_id>"         # Used for workspace isolation
event_name: "Flow description"
model: "gemini/gemini-3-pro-preview" # LLM model to use

# Setup: seed data using tool CLI directly (no LLM, no latency)
setup:
  - tool: <tool_name>              # Must match a supported tool in harness.py
    args: ["--flag", "value", ...]  # Exact CLI argv

# Beats: sequential conversation turns
beats:
  - actor: alex                     # Actor key from personas.yaml
    message: >                      # What the user says
      Natural language message to Homer.
    expect:                          # Optional — auto-checked assertions
      keywords: ["word1", "word2"]   # Must appear in response (case-insensitive)
      no_keywords: ["forbidden"]     # Must NOT appear in response
      tools_used: ["exec"]           # Tool names that must appear in tool calls
      tools_not_used: ["message"]    # Tool names that must NOT appear
      escalation: true               # Check that an escalation was created
      rsvp:                          # Check RSVP was recorded in event DB
        guest: "Name"
        status: "confirmed"          # confirmed | declined | maybe
      no_text_response: true         # Fail if agent wrote inline text (heartbeat tests)
      max_tool_calls: 5              # Fail if Homer uses more than N tool calls
      tool_sequence:                  # Check exec commands contain these patterns (in order)
        - pattern: "gmail_search"
          note: "Should search Gmail first"
        - pattern: "gmail_send.*draft"
          note: "Should create a draft"
    note: "What this beat tests"     # Displayed in transcript, for humans
    rebuild: true                    # Optional — rebuild guest context after this beat

# Mock external APIs (optional — intercepts exec commands matching patterns)
mock_tools:
  - pattern: "gmail_search\\.py.*query"    # Regex matched against exec command
    response: |                             # Canned JSON response returned instead
      [{"id": "msg_001", "subject": "Invoice", "from": "vendor@example.com"}]
  - pattern: "gmail_send\\.py.*draft"
    response: |
      {"status": "drafted", "draft_id": "r_123", "approval_url": "https://..."}
```

### Writing good simulation flows

**1. Seed realistic data in setup**

Don't start from empty — seed the DB with enough data that Homer has something to work with. This mirrors production where the user has been using the skill for weeks.

```yaml
setup:
  - tool: maintenance
    args: ["--add-task", "--name", "Replace HVAC filter", "--system", "HVAC", "--frequency", "90"]
  - tool: maintenance
    args: ["--add-provider", "--name", "Malcolm", "--specialty", "HVAC", "--phone", "770-555-0101"]
```

**2. Test natural conversation arcs, not individual commands**

Each flow should tell a story — a realistic session where the user accomplishes something meaningful:

- Meal planning: browse recipes → plan the week → generate grocery list → add extras → rate a meal
- Maintenance: check dashboard → complete a task → look up a vendor → start a project → check off items
- Health: check family dashboard → log a visit → track symptoms over 2 days → review patterns

**3. Keep expectations loose**

LLM responses vary between runs. Test for:
- **Keywords**: key facts that must appear (names, dates, amounts)
- **Tool usage**: verify Homer called the right tool (not hallucinating an answer)

Don't test for:
- Exact phrasing or formatting
- Response length
- Emoji usage

**4. 6-10 beats per flow**

Enough to test the conversation arc, not so many that runs take forever. Each beat costs an LLM API call (~15-30s).

**5. Actor messages should sound human**

Write messages the way a real household member texts — casual, sometimes incomplete, sometimes bundling multiple requests:

```yaml
# Good — sounds like a real person
message: >
  Kemi's been coughing since yesterday and had a low fever this morning —
  100.4. She's also been a little fussy. Log that please.

# Bad — sounds like a test script
message: >
  Please log a symptom for family member Kemi with symptoms "cough, fever"
  and temperature 100.4.
```

### Mocking external APIs

Tools that call external APIs (Gmail, Calendar, Drive, etc.) can't run in the simulation environment without real credentials. Use `mock_tools` to intercept exec commands and return canned responses:

```yaml
mock_tools:
  - pattern: "gmail_search\\.py.*Utility"
    response: |
      [{"id": "msg_001", "thread_id": "t_001", "subject": "Water Bill", "from": "utility@example.com", "date": "2026-04-10", "body": "Your bill is $47.82."}]

  - pattern: "gmail_send\\.py.*draft(?!-)"
    response: |
      {"status": "drafted", "draft_id": "r_sim_001", "to": "utility@example.com", "subject": "Autopay", "approval_id": "a-001", "approval_url": "https://example.com/approve/a-001"}
```

**How it works:** The harness wraps the nanobot ExecTool. Before running a subprocess, it checks the command against each `pattern` (regex). If it matches, the `response` string is returned directly — no subprocess is spawned.

**Tips:**
- Use negative lookahead to distinguish similar commands: `draft(?!-)` matches `draft` but not `draft-send` or `draft-delete`
- Mock responses should match the real tool's JSON output format exactly
- Patterns are checked with `re.search()` so they match anywhere in the command string

### Latency and tool call efficiency

**Tool calls are the primary driver of latency.** Each exec call adds ~2-5s of overhead. A beat with 2 tool calls finishes in ~10s; a beat with 8 calls takes ~30s. Optimizing tool call count is the most effective way to improve response time.

**Every simulation beat should set `max_tool_calls`.** This is not optional — it's the primary guard against instruction regressions. Set it to the minimum number of calls needed for the task, not a generous upper bound. If a beat can be done in 2 calls, set `max_tool_calls: 3` (one margin), not 6.

**When a simulation fails on `max_tool_calls`, fix the instructions — not the limit.** Increasing `max_tool_calls` to accommodate poor behavior hides regressions. Instead:
1. Read the transcript to understand what Homer is doing with the extra calls
2. Fix the skill instructions so Homer knows exactly which tool to use and how
3. If it's a harness issue (missing env var, wrong DB path), fix the harness

Use `max_tool_calls` and `tool_sequence` together:

```yaml
expect:
  max_tool_calls: 3          # Fail if Homer uses more than 3 tool calls
  tool_sequence:
    - pattern: "gmail_search"
      note: "Should search Gmail first"
    - pattern: "gmail_send.*draft"
      note: "Then create a draft"
```

`tool_sequence` checks that each pattern appears somewhere in the exec commands (in any order — it verifies presence, not strict ordering). This catches cases where Homer skips expected tools or uses unnecessary ones.

**Benchmarks:** A well-instructed beat typically uses 1-3 tool calls. If a beat needs 5+, the instructions likely need tightening — Homer is probably exploring (running `--help`, `cat`-ing source files, or retrying failed commands).

### Adding a new tool to the harness

When creating a new skill, register the tool in `tests/simulation/harness.py`:

**1. Add DB env var** in `setup()`:

```python
# In SimulationHarness.setup(), add alongside existing skill DBs:
self._set_env("HOMER_<NAME>_DB", str(state_dir / "<name>.db"))
```

**2. Add tool import** in `_run_setup_steps()`:

```python
# In the _get_tool() function inside _run_setup_steps():
elif name == "<tool_name>":
    import <tool_module>
    _tool_modules[name] = <tool_module>
```

The tool name in `_get_tool()` must match the `tool:` field in the flow YAML setup steps.

### Running simulations

```bash
# Run a single flow
.venv/bin/python -m tests.simulation.runner tests/simulation/flows/<name>.yaml

# Run specific beats only (for debugging)
.venv/bin/python -m tests.simulation.runner tests/simulation/flows/<name>.yaml --beats 1-3

# Keep workspace for inspection after run
.venv/bin/python -m tests.simulation.runner tests/simulation/flows/<name>.yaml --keep-workspace

# Compare two runs (e.g., after model change)
.venv/bin/python -m tests.simulation.runner --compare path/to/traj_a.json path/to/traj_b.json
```

### Reading results

Each run generates:
- **Terminal output**: colored transcript with PASS/FAIL per beat
- **trajectory.json**: full beat data (response, tool calls, tokens, latency, expectations) + `context_files` dict with all workspace .md files
- **transcript.md**: human-readable markdown
- **report.html**: interactive HTML report with collapsible Context Files viewer (shows AGENTS.md, SOUL.md, USER.md, HEARTBEAT.md for both main and guest workspaces)
- **artifacts/**: raw copies of workspace files, session logs, event files, scope DB

The HTML report auto-expands failed beats. Use the "Context Files" section to inspect exactly what instructions and context the agent had when it made a decision.

### Troubleshooting simulations

**Agent hits 20-iteration tool limit ("max tool call iterations")**

The agent is burning iterations exploring the filesystem or retrying failed commands instead of acting. Common causes:
- **Missing env vars in exec sandbox**: nanobot v0.1.5+ strips subprocess env vars. If a tool needs `HOMER_SCOPE_DB` or similar, it must be in `allowedEnvKeys` in the sim config (`_build_nanobot_config()` in harness.py). Symptom: tool returns an error, agent spends remaining iterations trying to debug env.
- **Agent doesn't know what tools it has**: if AGENTS.md doesn't list exact exec commands, the agent will `ls`/`cat` to discover them. Fix by adding an explicit "Available Tools" section.
- **Agent uses read_file/list_dir**: guest agent instructions must explicitly forbid filesystem tools and tell it to use only `exec`. Without this, it reads files instead of acting.

**Escalation expectation fails but agent called escalate.py**

Check trajectory.json — look at the escalate.py tool call result. If it's empty or errored, the scope DB path wasn't accessible from the exec subprocess (env var stripping, see above).

**RSVP expectation fails with "enrolled" (default status)**

The agent didn't call `event_manage.py --rsvp`. Check if the AGENTS.md instructions list the exact RSVP command. The agent needs the full command template, not just a description.

**Guest agent leaks internals ("Note to Alex", "Internal Actions")**

The guest AGENTS.md needs the CRITICAL section at the top explaining that all text is sent to the guest. Without it, the LLM treats its response as a scratchpad. Test with `no_keywords: ["escalate", "scope", "Internal Actions", "Note to"]`.

**Simulation takes >5 minutes**

Each beat makes a real LLM API call. Normal latency is 15-30s/beat. If beats take 60s+, the agent is probably hitting the iteration limit (20 tool calls). Check the tool call count in terminal output — if you see 20+ calls per beat, the instructions need tightening.

### Running simulations in parallel

The runner uses a per-flow workspace directory (`tests/sim_workspace_<flow_slug>/`) so multiple simulations can run simultaneously without conflict:

```bash
# These can run in parallel
.venv/bin/python -m tests.simulation.runner tests/simulation/flows/meal_planning.yaml &
.venv/bin/python -m tests.simulation.runner tests/simulation/flows/home_maintenance.yaml &
.venv/bin/python -m tests.simulation.runner tests/simulation/flows/health_records.yaml &
wait
```

---

## Step 6: Deploy

After all tests pass:

1. **build_context.py** copies `skills/<name>/SKILL.md` to the workspace
2. Trigger your deployment pipeline to ship the new code; on startup the
   container re-runs `build_context.py` and restarts nanobot
3. Homer picks up the new skill on next conversation turn

No config changes needed beyond the AGENTS.md whitelist — build_context.py auto-discovers skills.

After deploying a new skill, start a `/new` session on Telegram to clear the conversation cache, then test with a real message.

---

## Checklist: New Skill PR

All items are required. A skill PR will not be merged without passing both unit tests and simulation.

- [ ] `tools/<name>.py` — tool with SQLite schema, argparse CLI, JSON output
- [ ] `skills/<name>/SKILL.md` — skill doc with rules, CLI reference, workflows, examples, and emoji in metadata
- [ ] `tests/test_<name>.py` — unit tests (aim for 50+ tests)
- [ ] `agent/AGENTS.md` — whitelist entry added
- [ ] `config/config.hosted.json.template` — any new env vars added to `allowedEnvKeys`
- [ ] `config/capabilities.yaml` — capability registered (set `default_enabled: false` if third-party creds required)
- [ ] Provisioning-layer env passthrough — deployment-level secrets wired through (if tool needs credentials the deployment holds)
- [ ] `tests/simulation/flows/<name>.yaml` — conversation simulation (6-10 beats)
- [ ] `tests/simulation/harness.py` — DB env var + tool import registered
- [ ] Unit tests pass: `.venv/bin/python -m pytest tests/test_<name>.py -v`
- [ ] Simulation passes: `.venv/bin/python -m tests.simulation.runner tests/simulation/flows/<name>.yaml`
- [ ] Simulation expectations all green (fix keyword expectations if too strict, but never remove tool_used checks)

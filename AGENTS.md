# CLAUDE.md — Homer Project Instructions

You are working on Homer, a personal life agent. Homer is multitenant — the same code runs for multiple households, with per-tenant data loaded at deploy time from `context/*.md`.

When writing or modifying any system prompt, follow the rules in `PROMPT_ENGINEERING.md`.

## Multitenancy

Never hardcode per-tenant identifiers in agent templates, prompts, skill instructions, or eval flows. This includes:

- **Household email addresses** (e.g. a specific family's shared inbox)
- **Homer's own outbound email** — each tenant gets its own Homer address; do not hardcode any specific one
- **Primary user names, phone numbers, JIDs, or home locations**

Per-tenant data is injected into `USER.md` / `SOUL.md` by `build_context.py`. When a template needs to reference one of these values, use a role name or placeholder (`{PRIMARY_USER}`, "the household's primary email account", "Homer's own outbound email") — never a literal. Evals inject tenant values via setup fixtures, not inline in flow YAML.

## Architecture Quick Reference

- **Runtime**: nanobot-ai (Python). A typical deployment runs Homer inside a long-running container; for local dev a single `nanobot gateway` process is enough. Hosted-deploy artifacts (Dockerfile, build/release workflow, provisioning scripts) live in your deployment-side repo, not here.
- **Nanobot fork**: `github.com/anjorinjnr/nanobot` branch `homer-patches`. Installed via `pip install git+...`. Changes to agent runtime behavior (message routing, channel handling, workspace loading, heartbeat execution) go in the fork, not in this repo.
- **LLM**: Multi-model — Claude and Gemini models, switchable at runtime via `tools/switch_model.py`. Default model is configurable per deployment.
- **Channels**: Telegram + WhatsApp (via the bridge in the nanobot fork) + inbound email.
- **Agent behavior**: Templates `agent/SOUL.md`, `agent/AGENTS.md`, `agent/HEARTBEAT.md` with `{HOMER_HOME}`, `{HOMER_VENV}`, `{HOMER_TOOLS}`, `{HOMER_WORKSPACE}` placeholders — assembled by `tools/build_context.py` at startup.
- **Main vs guest nanobot**: two gateways may run side-by-side — main on `$PORT` (default 18790) against `.nanobot_workspace`, guest on `$GUEST_PORT` (18791) against `.guest_workspace`. Per-sender scope context is injected per-turn by the guest gateway via `scope_store.render_scope_context_for_sender` — guest `USER.md` is a 202-char stub.
- **Household context**: `context/*.md` files (all gitignored). Assembled into `.nanobot_workspace/USER.md` by `build_context.py`. Never modify the workspace directly.
- **Tools**: Python scripts in `tools/`. Called via nanobot's exec tool. Each tool must be whitelisted in `agent/AGENTS.md`.
- **Skills**: `skills/*/SKILL.md`. Copied to workspace by `build_context.py`.
- **Events**: `context/events/<event_id>/status.md`. Managed by `tools/event_manage.py` and `tools/manage_event_guest.py`.

### Containerised deployments

When Homer is run in a container (the typical hosted shape), the runtime layout is:

- `/opt/homer/` — baked homer code (image-bundled, read-only for homer code).
- `/data/` — persistent per-household volume. Holds `.env`, household context (`/data/context/...`), workspaces (`.nanobot_workspace`, `.guest_workspace`), and runtime state (scopes.db, events.db, etc.). Owned by the provisioning layer, not by Homer itself.
- `/home/homer/.nanobot/` — nanobot configs (main `config.json`, `guest_config.json`), rendered from templates at boot.
- Container names follow `homer-<household-id>`.
- Tail logs with `docker logs -f <container>`.
- Inside the container, `PYTHONPATH` for `scope_store` and friends is set by the gateway env but NOT by `docker exec` — pass `-e PYTHONPATH=/opt/homer/tools` when running tools ad hoc.

### Deploy

The build/release workflow and provisioning scripts live in your deployment-side repo. This repo ships only the agent code + dev tooling. To roll a new homer build, trigger that workflow from the deployment repo with the desired homer ref.

The nanobot fork is pulled in at image build time via `pip install git+https://github.com/anjorinjnr/nanobot.git@homer-patches`. To ship a fork change, push to `homer-patches` and then trigger a homer image rebuild from the deployment repo.


## Homer vs. Nanobot Fork — Where Does This Change Go?

Most features are implemented entirely in this repo (Homer). The nanobot fork is only modified when you need to change **runtime behavior that nanobot controls**.

**Changes in this repo (homer):**
- New tools, skills, agent instructions
- Context management, build_context.py
- Event management, guest access, budget tracking
- Heartbeat task definitions (HEARTBEAT.md)
- Deploy scripts, tests, docs

**Changes in the nanobot fork (anjorinjnr/nanobot@homer-patches):**
- Message routing logic (e.g., sender-based workspace switching for guest isolation)
- Channel handling (WhatsApp bridge behavior, Telegram polling)
- Heartbeat execution engine (e.g., the `send_reasoning: false` fix)
- Workspace file loading order or format changes
- New nanobot-level capabilities (new tool types, new channel support)

**When a feature spans both repos:**
1. Implement the Homer-side changes first (tools, agent instructions, tests)
2. Implement the nanobot fork changes separately on `scope-context-injection`-style feature branches off `homer-patches`
3. Merge the fork PR to `homer-patches`
4. Merge the Homer PR to `main` — the image rebuild runs `pip install git+...@homer-patches`, so both halves ship in the same container
5. Note the cross-repo dependency in the PR description (link the nanobot PR)

The fork is installed into the homer image at build time via: `.venv/bin/pip install git+https://github.com/anjorinjnr/nanobot.git@homer-patches`. No separate fork deploy step.

## Workflow

1. **Always create a worktree** — use `EnterWorktree` before making any changes. Never commit directly to `main`.
2. **Create a PR** — when your changes are ready, push the branch and open a PR.
3. **Watch for auto-review** — the PR will receive an automatic review. Monitor it, fix any requested changes, and push until the review approves.
4. **Update the PR description** — before merging, update the PR title and body to reflect the final state of the code, not the initial submission. If review feedback changed the approach, the description must match what actually shipped.
5. **File follow-ups as GitHub issues, not doc bullets** — for any review finding we decide not to fix in this PR (efficiency follow-up, optional refactor, design suggestion), run `gh issue create --repo <owner>/<repo> --title "..." --body "..." --label tech-debt` BEFORE merging. Link the issue numbers from the PR description. Plan docs and PR descriptions decay; issues stay searchable. The only acceptable skip is a finding the author confirms is a false positive — say so explicitly in the PR thread.
6. **Squash and merge** — always use `gh pr merge --squash` with a clean, descriptive commit message summarizing the final change.
7. Do not consider the task done until the PR is approved and merged.

## Before You Start

1. If working on a specific feature, check `docs/features/` for a design doc.
2. If modifying agent behavior, read the relevant `agent/*.md` template files.
3. **If creating or modifying a skill, you MUST read `docs/skill_development_guide.md`** — it covers the full tool/skill/test/simulation pattern end-to-end. Every skill requires unit tests AND a conversation simulation flow. Do not skip the simulation.

## Writing New Tools

All tools in `tools/` follow the same contract:

1. **argparse** for CLI arguments
2. **JSON to stdout** — nanobot parses this. All output must be valid JSON.
3. **JSON error output** — on failure, print `{"error": "message"}` and `sys.exit(1)`.
4. **No disk writes for sensitive data** — Drive content stays in-memory (`io.BytesIO`).
5. **Add to AGENTS.md whitelist** — Homer can only exec whitelisted scripts. If you create a new tool, add it to both the "Approved Scripts" list and the "Tool Reference" section in `agent/AGENTS.md`.
6. **Add tests** — every tool has tests in `tests/test_<toolname>.py`. Mock external APIs; test pure logic.

## Writing Tests

```bash
# Run all tests
.venv/bin/python -m pytest tests/ -v --tb=short

# Run a specific test file
.venv/bin/python -m pytest tests/test_event_manage.py -v

# Run tests matching a pattern
.venv/bin/python -m pytest tests/ -k "test_budget" -v
```

Tests mock external services (Google APIs, Plaid, etc.). No real credentials needed.
Pre-commit hook runs pytest automatically — don't skip it.

## Key Patterns

### Tool output pattern
```python
# Success
print(json.dumps({"status": "created", "event_id": "trip_1", ...}))

# Error
print(json.dumps({"error": "Event not found"}))
sys.exit(1)
```

### Agent template variables
In `agent/*.md` files, use these placeholders (resolved by `build_context.py`):
- `{HOMER_HOME}` → repo root (e.g., `/opt/homer`)
- `{HOMER_VENV}` → `.venv/bin/python`
- `{HOMER_TOOLS}` → `tools/` directory path
- `{HOMER_WORKSPACE}` → `.nanobot_workspace/` path

### Context update flow
Users tell Homer a fact → Homer proposes update → user confirms → Homer runs `context_updater.py` → file updated + `build_context.py` rebuilds workspace.

### Event lifecycle
`event_manage.py --create` → `--update`/`--add-item` → `--set-status` → `--close` (archives + revokes guests).

## Do NOT

- Modify `secrets/`, `.env`, `*.key`, `*.pickle`, `*_tokens.*`
- Modify `context/*.md` directly (gitignored household data, managed by Homer at runtime)
- Modify `.nanobot_workspace/` or `.guest_workspace/` files directly — they are regenerated by `build_context.py` at every container boot. Anything you write there will be overwritten.
- Write to a guest workspace's `USER.md` with scope data — it is intentionally a stub. Per-sender scope context is injected at turn time by nanobot (see `scope_store.render_scope_context_for_sender`). `tools/scope_leakage_check.py` locks this down.
- Edit `context/*.md` files directly on a deployed instance — context is regenerated from gitignored sources at runtime; direct edits will be lost. Use Homer's own update tools.
- Write code that Homer executes at runtime without adding it to the AGENTS.md whitelist
- Use `subprocess.run` with shell=True in tools (injection risk)
- Store API responses or sensitive data on disk — always use in-memory buffers

## Commit Convention

Use conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.

Examples:
- `feat: add venue search to event planning flow`
- `fix: calendar_add.py timezone handling for all-day events`
- `chore: update AGENTS.md whitelist for new tool`
- `test: add budget summary edge case tests`

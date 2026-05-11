# Homer — Personal Life Agent

Homer is a personal AI agent for a household. It holds long-running context
about the people who live there, monitors email and calendar, and reaches
the right person at the right time over chat. Homer is designed to be run
by anyone for their household, not by a hosted service.

The agent runtime is [nanobot-ai](https://github.com/HKUDS/nanobot). Homer
extends it with household-specific tools, skills, and agent prompts.

## Quickstart (local development)

Prerequisites:
- Python 3.12+
- Node.js 20+ (the WhatsApp bridge in the nanobot fork is Node)
- git
- [`gogcli`](https://github.com/steipete/gogcli) — Homer's Gmail / Drive / Calendar / Docs / Sheets tools shell out to the `gog` binary. It isn't a Python package, so `requirements.txt` can't install it. Install with `brew install steipete/tap/gogcli` (macOS) or `go install github.com/steipete/gogcli/cmd/gog@latest` (any platform with Go ≥ 1.22).

```bash
git clone https://github.com/anjorinjnr/homer.git
cd homer
cp secrets/.env.template secrets/.env
# Edit secrets/.env — see the template for the full list of supported keys
bash scripts/setup.sh --dev   # uses TELEGRAM_BOT_TOKEN_DEV — won't conflict with a hosted bot
nanobot gateway               # start Homer locally
```

`scripts/setup.sh` (no flag) uses `TELEGRAM_BOT_TOKEN`. Telegram only allows
one polling instance per token, so use a separate dev bot if you also run
Homer somewhere else.

## Architecture

- **Runtime**: [nanobot-ai](https://github.com/HKUDS/nanobot) — a Python
  agent runtime. Homer pins a fork at
  [`anjorinjnr/nanobot@homer-patches`](https://github.com/anjorinjnr/nanobot)
  for changes that aren't yet upstream.
- **Agent prompts**: Templates in `agent/` — `SOUL.md` (identity),
  `AGENTS.md` (instructions), `HEARTBEAT.md` (recurring tasks).
- **Tools**: Python scripts in `tools/`. Each tool is a CLI that takes
  arguments, talks to an external API or local store, and emits one JSON
  line. Tools are exposed to the agent through nanobot's exec contract and
  must be whitelisted in `agent/AGENTS.md`.
- **Skills**: Markdown specs in `skills/<name>/SKILL.md`. Skills compose
  one or more tools into a higher-level capability the agent can invoke.
- **Context**: `context/*.md` — gitignored household state (people,
  property, projects, finances, health). Source of truth for who and what
  Homer knows about.
- **Workspace assembly**: `tools/build_context.py` resolves
  `{HOMER_HOME}` / `{HOMER_VENV}` / `{HOMER_TOOLS}` / `{HOMER_WORKSPACE}`
  placeholders in the agent templates and writes the rendered files into
  the nanobot workspace before launch.
- **Channels**: Telegram and WhatsApp (via the nanobot fork's Baileys
  bridge), plus inbound email.
- **Two nanobot gateways**: a "main" gateway against the household
  workspace, and a "guest" gateway with per-sender scoped context for
  external participants (event planners, vendors, etc.).
- **Adding a tool or skill**: see [`docs/skill_development_guide.md`](docs/skill_development_guide.md)
  for the full tool/skill/test/simulation pattern.

## Self-hosting

This repo ships only the agent code, agent prompts, tools, skills, tests,
config templates, and dev tooling. There is no turnkey hosted-deploy
recipe in here on purpose — bring your own.

The shape of a Homer deployment is:

- A long-running Python process executing `nanobot gateway` against an
  assembled workspace.
- Secrets in `secrets/.env` (see `secrets/.env.template` for the supported
  keys; many are optional and only needed if you enable that integration).
- A persistent volume for `context/*` and the runtime workspace state
  (`.nanobot_workspace`, scopes/events SQLite stores, etc.).
- For WhatsApp: a Baileys bridge process the agent talks to. Source lives
  in the nanobot fork.
- Optional: containerise it with your own `Dockerfile` and ship via your
  own CI/CD. Any process supervisor (systemd, container restart policy,
  Kubernetes, etc.) works.

If you're running Homer for one household on one host, a manual
`nanobot gateway` under your favourite supervisor is enough. Multi-tenant
hosting is non-trivial and intentionally out of scope for this repo.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -v
```

Tests mock external services (Google APIs, Plaid, etc.); no real
credentials needed. The pre-commit hook runs the suite — don't skip it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Branch off `main`, work in a
worktree (the convention is documented in `AGENTS.md`), open a PR, and
let the auto-review run before merging.

## License

[MIT](LICENSE).

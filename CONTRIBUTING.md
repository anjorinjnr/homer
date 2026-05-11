# Contributing to Homer

Thanks for your interest in contributing. Homer is opinionated and
single-tenant by design, but bug reports, PRs, and discussion are welcome.

## Dev setup

See [`README.md`](README.md) for installation and the local-development
workflow. The short version: clone the repo, run `scripts/setup.sh`, then
launch nanobot.

## Workflow

The full project workflow — branch conventions, the worktree pattern, prompt-
update rules, and tool-contract requirements — lives in
[`AGENTS.md`](AGENTS.md). Read it before opening a non-trivial PR.

Key points:

- **Branch from a worktree.** Use `git worktree add` rather than editing
  `main` directly. AGENTS.md explains why and how.
- **Branch naming.** `feature/<slug>`, `fix/<slug>`, `chore/<slug>`,
  `docs/<slug>`.
- **Commit format.** Conventional Commits (`feat:`, `fix:`, `chore:`,
  `docs:`, `refactor:`, `test:`). Keep the subject under ~70 chars.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v --tb=short
```

A pre-commit hook runs the suite automatically. Don't bypass it with
`--no-verify`; if a hook fails, fix the underlying issue.

For new tools or skills, include unit tests in `tests/` and (where
appropriate) a simulation flow in `tests/simulation/flows/`. The
[skill development guide](docs/skill_development_guide.md) walks through
the pattern.

## Pull requests

- Squash-and-merge is the default; write a descriptive PR body that explains
  the **why**, not just the **what**.
- Link the related issue (or the design doc under `docs/features/`).
- Include a short test plan.
- If you found a follow-up worth doing but out of scope, file a GitHub issue
  rather than leaving it as a PR-body bullet.

## Security

For security issues, please follow [`SECURITY.md`](SECURITY.md) instead of
opening a public issue.

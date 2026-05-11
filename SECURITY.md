# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Homer, please report it privately.
**Do not file a public GitHub issue.**

Preferred channels:

- Email: `security@joybuild.ai`
- GitHub: open a [private security advisory](https://github.com/anjorinjnr/homer/security/advisories/new)

Please include:

- A description of the issue and its impact
- Steps to reproduce (or a proof-of-concept, if available)
- The commit SHA or release version you tested against

We aim to acknowledge reports within 72 hours and to provide a remediation
timeline within 7 days. Coordinated disclosure is appreciated; please give us
reasonable time to ship a fix before publishing details.

## Scope

In scope:

- The Homer agent code in this repository (`tools/`, `skills/`, `agent/`,
  `scripts/`, `config/`)
- Documented tool contracts and prompt-injection defenses

Out of scope:

- Hosted-deployment infrastructure (server provisioning, CDN, container registry)
- Customer-facing services and other internal services
  (report those to the same channels above; we will route appropriately)
- Third-party dependencies (please report upstream first; we will track and
  pull in fixes once released)

## Known limitations

Homer is an LLM agent operating against external APIs (Gmail, Calendar,
Drive, etc.). It includes prompt-injection defenses and an exec-tool
allowlist, but no LLM agent is fully immune to adversarial input. If you
deploy Homer, treat its outputs as untrusted and keep its credentials scoped
to the minimum required.

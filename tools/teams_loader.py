#!/usr/bin/env python3
"""
teams_loader.py — registry + membership helpers for org-mode teams.

Org mode (HOMER_ORG_MODE=1) turns a single Homer instance into an
organization's chief of staff: members belong to one or more *teams*, and a
member's agent turn is scoped to the team(s) they're on (see
tools/team_scope_context.py). This module is the read side of that model:

- The **team registry** (``context/teams.yaml``) names the teams and carries
  their description / coordination notes. Symbol-keyed like users.yaml.

      schema_version: 1
      teams:
        worship:
          name: "Worship Team"
          description: "Sunday service music + AV."

- **Per-member team membership** lives on each user record in users.yaml under
  a ``teams`` field, mapping team slug → that member's role on the team:

      users:
        jordan:
          display_name: "Jordan"
          role: member            # org-level role (member of the org)
          teams:
            worship: admin        # team-level role
            ushers: member

  ``role: admin`` at the org level (the ``primary`` symbol) is the org admin —
  they see across every team regardless of their ``teams`` map.

The ``teams`` field round-trips through users_loader untouched (it preserves
unrecognised keys), so this module never writes users.yaml — it only reads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.users_loader import ADMIN_SYMBOL, iter_users  # noqa: E402

DEFAULT_TEAMS_FILE = REPO_ROOT / "context" / "teams.yaml"
TEAMS_SCHEMA_VERSION = 1


def _teams_file() -> Path:
    """Path to teams.yaml. Overridable via HOMER_TEAMS_YAML for tests."""
    override = os.environ.get("HOMER_TEAMS_YAML")
    return Path(override) if override else DEFAULT_TEAMS_FILE


# ── Registry ─────────────────────────────────────────────────────────────────

def load_teams(path: Path | None = None) -> dict:
    """Read teams.yaml and return ``{schema_version, teams: {slug: record}}``.

    A missing file returns an empty registry so callers don't branch on
    existence. A corrupt file raises — silent fallbacks hide drift.
    """
    path = path or _teams_file()
    if not path.exists():
        return {"schema_version": TEAMS_SCHEMA_VERSION, "teams": {}}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"teams.yaml is not a mapping at top level: {path}")
    teams = raw.get("teams")
    if not isinstance(teams, dict):
        teams = {}
    return {"schema_version": TEAMS_SCHEMA_VERSION, "teams": teams}


def team_record(teams_data: dict, slug: str) -> dict | None:
    """Return the registry record for a slug, or None if unknown."""
    rec = (teams_data.get("teams") or {}).get(slug)
    return rec if isinstance(rec, dict) else None


def team_display_name(teams_data: dict, slug: str) -> str:
    """Human name for a team, falling back to the slug itself."""
    rec = team_record(teams_data, slug)
    return (rec or {}).get("name") or slug


# ── Membership (read off user records) ───────────────────────────────────────

def normalize_user_teams(record: dict) -> dict[str, str]:
    """Return ``{team_slug: team_role}`` for a user record.

    Tolerant of two hand-edited shapes:
      teams: {worship: admin, ushers: member}   → as-is
      teams: [worship, ushers]                   → every slug role 'member'
    Anything else (missing, scalar, junk) → ``{}``.
    """
    raw = record.get("teams")
    if isinstance(raw, dict):
        return {
            str(slug): (str(role) if role in ("admin", "member") else "member")
            for slug, role in raw.items()
            if slug
        }
    if isinstance(raw, list):
        return {str(slug): "member" for slug in raw if slug}
    return {}


def is_org_admin(record: dict) -> bool:
    """Org admin = household-level admin role; sees across all teams."""
    return (record.get("role") or "member") == "admin"


def user_team_slugs(record: dict) -> set[str]:
    return set(normalize_user_teams(record).keys())


def members_of_team(users_data: dict, slug: str) -> list[tuple[str, dict, str]]:
    """Return ``(symbol, record, team_role)`` for every member on ``slug``.

    The org admin (``primary``) is included with role 'admin' even if not
    explicitly listed under the team, since they oversee every team.
    """
    out: list[tuple[str, dict, str]] = []
    for symbol, record in iter_users(users_data):
        teams = normalize_user_teams(record)
        if slug in teams:
            out.append((symbol, record, teams[slug]))
        elif symbol == ADMIN_SYMBOL or is_org_admin(record):
            out.append((symbol, record, "admin"))
    return out


def teams_for_user(record: dict, teams_data: dict) -> set[str]:
    """The set of team slugs a user can see.

    Org admins see every team in the registry; everyone else sees only the
    teams listed on their record (strict per-team isolation).
    """
    if is_org_admin(record):
        return set((teams_data.get("teams") or {}).keys())
    return user_team_slugs(record)

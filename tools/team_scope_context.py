#!/usr/bin/env python3
"""
team_scope_context.py — per-sender team scoping for org mode.

Wired as the **main** gateway's ``agents.defaults.scope_context_provider``
(``team_scope_context:render_team_context_for_sender``) when a tenant runs in
org mode (HOMER_ORG_MODE=1). nanobot calls ``render_team_context_for_sender
(sender_id)`` before each turn and injects the returned markdown into the
agent's context (see nanobot agent loop ``_get_scope_context``).

The contract (per nanobot): a single positional ``sender_id: str`` in, a
``str`` out; ``""`` means "nothing to inject". Any exception is swallowed by
the caller, but we fail closed here too — a sender we can't resolve, or who is
on no team, gets an empty string rather than another member's context.

Strict per-team isolation: a member sees only the team(s) on their users.yaml
record. The org admin (``primary`` / org-level ``role: admin``) sees every team
in the registry. Cross-team leakage is prevented here, at injection time —
that's the isolation boundary, since the whole org shares one main workspace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.teams_loader import (  # noqa: E402
    load_teams,
    members_of_team,
    normalize_user_teams,
    team_display_name,
    team_record,
    teams_for_user,
)
from tools.users_loader import iter_users, load_users  # noqa: E402

USERS_YAML = Path(
    os.environ.get("HOMER_USERS_YAML", REPO_ROOT / "context" / "users.yaml")
)

_WA_JID_SUFFIXES = ("@s.whatsapp.net", "@lid", "@g.us", "@c.us")


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _normalize_email(s: str) -> str:
    """Lowercase + strip; dotless Gmail local part, matching scope_store's
    contract. Imported lazily so this module loads without nanobot present."""
    try:
        import scope_store as ss  # type: ignore[import-untyped]

        return ss.normalize_email(s)
    except Exception:
        return s.strip().lower()


def _looks_like_email(sender_id: str) -> bool:
    if "@" not in sender_id:
        return False
    return not any(sender_id.endswith(suf) for suf in _WA_JID_SUFFIXES)


def resolve_symbol(sender_id: str, users_data: dict | None = None) -> str | None:
    """Map a raw channel ``sender_id`` to a users.yaml symbol, or None.

    Handles the three channel shapes Homer sees inbound:
      - telegram: ``tg:123`` or bare digits → match channels.telegram digits
      - whatsapp: JID / LID / bare digits   → match channels.whatsapp digits
      - email:    ``a@b.com``                → match channels.email (normalized)
    """
    if not sender_id:
        return None
    data = users_data if users_data is not None else load_users(USERS_YAML)

    if _looks_like_email(sender_id):
        target = _normalize_email(sender_id)
        for symbol, record in iter_users(data):
            channels = record.get("channels") or {}
            for key in ("email", "gmail"):
                if channels.get(key) and _normalize_email(str(channels[key])) == target:
                    return symbol
        return None

    digits = _digits(sender_id.removeprefix("tg:"))
    if not digits:
        return None
    for symbol, record in iter_users(data):
        channels = record.get("channels") or {}
        for key in ("telegram", "whatsapp"):
            handle = channels.get(key)
            if handle and _digits(str(handle)) == digits:
                return symbol
    return None


def _render_team_section(
    slug: str,
    teams_data: dict,
    users_data: dict,
    viewer_role: str,
) -> str:
    name = team_display_name(teams_data, slug)
    rec = team_record(teams_data, slug) or {}
    lines = [f"## {name}  (`{slug}`)"]
    if viewer_role:
        lines.append(f"Your role on this team: **{viewer_role}**")
    if (desc := (rec.get("description") or "").strip()):
        lines.append(desc)
    roster = members_of_team(users_data, slug)
    if roster:
        lines.append("Members:")
        for _symbol, member, team_role in roster:
            display = member.get("display_name") or "?"
            lines.append(f"- {display} ({team_role})")
    return "\n".join(lines)


def render_team_context_for_sender(sender_id: str) -> str:
    """Provider entrypoint. Render the team context visible to ``sender_id``.

    Returns ``""`` when org mode is off, the sender can't be resolved, or the
    resolved member is on no team — never another member's context.
    """
    if os.environ.get("HOMER_ORG_MODE") not in ("1", "true", "True"):
        return ""
    try:
        users_data = load_users(USERS_YAML)
        symbol = resolve_symbol(sender_id, users_data)
        if not symbol:
            return ""
        record = (users_data.get("users") or {}).get(symbol) or {}
        teams_data = load_teams()
        visible = teams_for_user(record, teams_data)
        if not visible:
            return ""
        member_roles = normalize_user_teams(record)
        # Deterministic order: registry order, then any extras alphabetically.
        registry_order = list((teams_data.get("teams") or {}).keys())
        ordered = [s for s in registry_order if s in visible]
        ordered += sorted(visible - set(ordered))

        sections = [
            _render_team_section(
                slug, teams_data, users_data, member_roles.get(slug, "")
            )
            for slug in ordered
        ]
    except Exception as exc:  # never break a turn over context injection
        logger.warning("team_scope_context: render failed for {}: {}", sender_id, exc)
        return ""
    if not sections:
        return ""
    return "# Your Teams\n\n" + "\n\n".join(sections)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Render team scope context for a sender")
    p.add_argument("sender_id")
    print(render_team_context_for_sender(p.parse_args().sender_id))

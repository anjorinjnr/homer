"""Tests for org-mode team scoping: tools/teams_loader.py +
tools/team_scope_context.py.

The load-bearing property is strict per-team isolation: a member's injected
context shows only the team(s) on their record, the org admin sees every team,
and an unresolved sender sees nothing.
"""

from __future__ import annotations

import importlib

import pytest
import yaml

from tools.teams_loader import (
    is_org_admin,
    load_teams,
    members_of_team,
    normalize_user_teams,
    team_display_name,
    teams_for_user,
)

USERS_DOC = {
    "schema_version": 2,
    "users": {
        "primary": {
            "display_name": "Pat Lead",
            "role": "admin",
            "channels": {
                "telegram": "5550000001",
                "whatsapp": "14125550001",
                "email": "pat@example.com",
            },
        },
        "jordan": {
            "display_name": "Jordan Keys",
            "role": "member",
            "channels": {"telegram": "5550000002"},
            "teams": {"worship": "admin"},
        },
        "riley": {
            "display_name": "Riley Doors",
            "role": "member",
            "channels": {"whatsapp": "+1 (412) 555-0003"},
            "teams": {"ushers": "member"},
        },
    },
}

TEAMS_DOC = {
    "schema_version": 1,
    "teams": {
        "worship": {"name": "Worship Team", "description": "Sunday music + AV."},
        "ushers": {"name": "Ushers & Greeters", "description": "Welcome + seating."},
    },
}


@pytest.fixture()
def org(tmp_path, monkeypatch):
    """Isolated users.yaml + teams.yaml in org mode, with a freshly-imported
    team_scope_context (its USERS_YAML global is captured at import time)."""
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text(yaml.safe_dump(USERS_DOC))
    teams_yaml = tmp_path / "teams.yaml"
    teams_yaml.write_text(yaml.safe_dump(TEAMS_DOC))

    monkeypatch.setenv("HOMER_USERS_YAML", str(users_yaml))
    monkeypatch.setenv("HOMER_TEAMS_YAML", str(teams_yaml))
    monkeypatch.setenv("HOMER_ORG_MODE", "1")

    for name in ("tools.team_scope_context", "team_scope_context"):
        importlib.sys.modules.pop(name, None)
    return importlib.import_module("tools.team_scope_context")


# ── teams_loader ─────────────────────────────────────────────────────────────

def test_load_teams_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMER_TEAMS_YAML", str(tmp_path / "nope.yaml"))
    assert load_teams()["teams"] == {}


def test_load_teams_rejects_non_mapping(tmp_path, monkeypatch):
    bad = tmp_path / "teams.yaml"
    bad.write_text("- just\n- a list\n")
    monkeypatch.setenv("HOMER_TEAMS_YAML", str(bad))
    with pytest.raises(ValueError):
        load_teams()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"teams": {"a": "admin", "b": "member"}}, {"a": "admin", "b": "member"}),
        ({"teams": ["a", "b"]}, {"a": "member", "b": "member"}),
        ({"teams": {"a": "bogus"}}, {"a": "member"}),  # invalid role coerced
        ({}, {}),
        ({"teams": "nonsense"}, {}),
    ],
)
def test_normalize_user_teams(raw, expected):
    assert normalize_user_teams(raw) == expected


def test_teams_for_user_admin_sees_all_members_see_own():
    data, teams = USERS_DOC, TEAMS_DOC
    assert teams_for_user(data["users"]["primary"], teams) == {"worship", "ushers"}
    assert teams_for_user(data["users"]["jordan"], teams) == {"worship"}
    assert teams_for_user(data["users"]["riley"], teams) == {"ushers"}


def test_is_org_admin():
    assert is_org_admin(USERS_DOC["users"]["primary"]) is True
    assert is_org_admin(USERS_DOC["users"]["jordan"]) is False


def test_members_of_team_includes_org_admin():
    roster = members_of_team(USERS_DOC, "worship")
    by_symbol = {sym: role for sym, _rec, role in roster}
    assert by_symbol["jordan"] == "admin"
    assert by_symbol["primary"] == "admin"  # org admin oversees every team
    assert "riley" not in by_symbol  # not on worship


def test_team_display_name_falls_back_to_slug():
    assert team_display_name(TEAMS_DOC, "worship") == "Worship Team"
    assert team_display_name(TEAMS_DOC, "unknown") == "unknown"


# ── resolve_symbol ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "sender_id,expected",
    [
        ("tg:5550000002", "jordan"),
        ("5550000002", "jordan"),
        # nanobot delivers telegram sender_id as "{id}|{username}"; the username
        # (even one containing digits) must not corrupt the numeric match.
        ("5550000002|jordan9", "jordan"),
        ("tg:5550000002|jk", "jordan"),
        ("14125550003@s.whatsapp.net", "riley"),  # JID form, formatting stripped
        ("14125550003", "riley"),  # bare full-number digits
        ("pat@example.com", "primary"),
        ("PAT@example.com", "primary"),  # case-insensitive email
        ("5559999999", None),
        ("", None),
    ],
)
def test_resolve_symbol(org, sender_id, expected):
    assert org.resolve_symbol(sender_id) == expected


# ── render_team_context_for_sender ───────────────────────────────────────────

def test_render_member_sees_only_own_team(org):
    out = org.render_team_context_for_sender("tg:5550000002")  # jordan → worship
    assert "Worship Team" in out
    assert "Ushers" not in out  # strict isolation: no other team leaks
    assert "Your role on this team: **admin**" in out


def test_render_org_admin_sees_all_teams(org):
    out = org.render_team_context_for_sender("pat@example.com")
    assert "Worship Team" in out
    assert "Ushers & Greeters" in out


def test_render_unknown_sender_is_empty(org):
    assert org.render_team_context_for_sender("5559999999") == ""


def test_render_off_when_org_mode_disabled(org, monkeypatch):
    monkeypatch.setenv("HOMER_ORG_MODE", "0")
    assert org.render_team_context_for_sender("tg:5550000002") == ""


def test_render_member_with_no_teams_is_empty(org, tmp_path, monkeypatch):
    doc = {
        "schema_version": 2,
        "users": {
            "primary": {"display_name": "Pat", "role": "admin", "channels": {}},
            "casey": {
                "display_name": "Casey",
                "role": "member",
                "channels": {"telegram": "5550000099"},
            },
        },
    }
    (tmp_path / "users2.yaml").write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("HOMER_USERS_YAML", str(tmp_path / "users2.yaml"))
    importlib.sys.modules.pop("tools.team_scope_context", None)
    mod = importlib.import_module("tools.team_scope_context")
    assert mod.render_team_context_for_sender("5550000099") == ""

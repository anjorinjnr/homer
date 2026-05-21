"""Tests for manage_users.py."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

def _load(path: Path) -> dict:
    """Inspect users.yaml for assertions, returning the legacy v1 list shape.

    The on-disk file may be v1 (test fixture seeds it as v1) or v2 (after any
    mutation, since manage_users.py writes v2 going forward). Tests assert
    against the legacy shape because that's what their assertions were
    written for; the loader handles the schema difference."""
    from tools.users_loader import as_legacy_list, load_users
    return {"users": as_legacy_list(load_users(path))}


def _save(path: Path, data: dict) -> None:
    """Write a v1-shape fixture to disk. The loader auto-migrates v1 → v2
    on first read, which is what the fixtures want to exercise."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


@pytest.fixture
def users_file(tmp_path, monkeypatch):
    f = tmp_path / "users.yaml"
    _save(f, {"users": [
        {"name": "Alice", "role": "admin", "channels": {"telegram": "123"}},
        {"name": "Bob", "role": "member", "channels": {"whatsapp": "14155551234"}},
    ]})
    monkeypatch.setattr("tools.manage_users.USERS_FILE", f)
    return f


@pytest.fixture
def empty_users_file(tmp_path, monkeypatch):
    f = tmp_path / "users.yaml"
    monkeypatch.setattr("tools.manage_users.USERS_FILE", f)
    return f


class TestList:
    def test_lists_all_users(self, users_file, capsys):
        from tools.manage_users import cmd_list
        import argparse
        cmd_list(argparse.Namespace())
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2
        assert out[0]["name"] == "Alice"
        assert out[1]["name"] == "Bob"

    def test_empty_file(self, empty_users_file, capsys):
        from tools.manage_users import cmd_list
        import argparse
        cmd_list(argparse.Namespace())
        out = json.loads(capsys.readouterr().out)
        assert out == []


@pytest.fixture
def as_admin(monkeypatch):
    """Stamp the runtime sender env to a known admin (Alice) — mirrors what
    nanobot does in production. CLI tests for privileged commands depend on
    this; without it the new requester check refuses with 'Not authorized.'"""
    monkeypatch.setenv("NANOBOT_SENDER_ID", "123")
    monkeypatch.setenv("NANOBOT_SENDER_CHANNEL", "telegram")


@pytest.fixture
def as_member(monkeypatch):
    """Stamp the runtime sender env to a known member (Bob)."""
    monkeypatch.setenv("NANOBOT_SENDER_ID", "14155551234")
    monkeypatch.setenv("NANOBOT_SENDER_CHANNEL", "whatsapp")


class TestAdd:
    @pytest.fixture(autouse=True)
    def _admin(self, as_admin):
        pass

    def test_add_member(self, users_file, capsys):
        from tools.manage_users import cmd_add
        import argparse
        cmd_add(argparse.Namespace(name="Charlie", role="member", telegram="456", whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert out["user"]["name"] == "Charlie"

        data = _load(users_file)
        assert len(data["users"]) == 3
        assert data["users"][2]["name"] == "Charlie"

    def test_add_duplicate_fails(self, users_file, capsys):
        from tools.manage_users import cmd_add
        import argparse
        with pytest.raises(SystemExit):
            cmd_add(argparse.Namespace(name="Alice", role="member", telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert "already exists" in out["error"]

    def test_add_second_admin_fails(self, users_file, capsys):
        from tools.manage_users import cmd_add
        import argparse
        with pytest.raises(SystemExit):
            cmd_add(argparse.Namespace(name="Charlie", role="admin", telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert "Admin already exists" in out["error"]

    def test_add_admin_to_empty(self, empty_users_file):
        # First-admin bootstrap goes through the portal's in-process
        # add_user() call (Supabase auth, bypasses the CLI requester gate).
        # The CLI path is never used to create the first admin in
        # production — there's no household to message the agent from yet.
        from tools.manage_users import add_user
        add_user(name="Alice", role="admin", telegram="123", whatsapp="14155551234")

        data = _load(empty_users_file)
        assert len(data["users"]) == 1

    def test_case_insensitive_duplicate(self, users_file, capsys):
        from tools.manage_users import cmd_add
        import argparse
        with pytest.raises(SystemExit):
            cmd_add(argparse.Namespace(name="alice", role="member", telegram=None, whatsapp=None, briefing_style=None))


class TestUpdate:
    @pytest.fixture(autouse=True)
    def _admin(self, as_admin):
        pass

    def test_update_channel(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        cmd_update(argparse.Namespace(name="Bob", rename=None, role=None, telegram="789", whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "updated"
        assert out["user"]["channels"]["telegram"] == "789"
        # whatsapp preserved
        assert out["user"]["channels"]["whatsapp"] == "14155551234"

    def test_rename(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        cmd_update(argparse.Namespace(name="Bob", rename="Robert", role=None, telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert out["user"]["name"] == "Robert"

        data = _load(users_file)
        names = [u["name"] for u in data["users"]]
        assert "Robert" in names
        assert "Bob" not in names

    def test_remove_channel(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        cmd_update(argparse.Namespace(name="Bob", rename=None, role=None, telegram=None, whatsapp="", briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert "whatsapp" not in out["user"]["channels"]

    def test_update_nonexistent_fails(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        with pytest.raises(SystemExit):
            cmd_update(argparse.Namespace(name="Nobody", rename=None, role=None, telegram=None, whatsapp=None, briefing_style=None))

    def test_rename_capitalization_allowed(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        cmd_update(argparse.Namespace(name="bob", rename="Bob", role=None, telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "updated"
        assert out["user"]["name"] == "Bob"

    def test_rename_collision_fails(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        with pytest.raises(SystemExit):
            cmd_update(argparse.Namespace(name="Bob", rename="Alice", role=None, telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert "already exists" in out["error"]

    def test_demote_only_admin_fails(self, users_file, capsys):
        from tools.manage_users import cmd_update
        import argparse
        with pytest.raises(SystemExit):
            cmd_update(argparse.Namespace(name="Alice", rename=None, role="member", telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert "Cannot demote" in out["error"]

    def test_promote_to_admin_atomic_swap(self, users_file, capsys):
        """Promoting a member to admin auto-demotes the current admin."""
        from tools.manage_users import cmd_update
        import argparse
        cmd_update(argparse.Namespace(name="Bob", rename=None, role="admin", telegram=None, whatsapp=None, briefing_style=None))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "updated"
        assert out["user"]["role"] == "admin"

        data = _load(users_file)
        alice = next(u for u in data["users"] if u["name"] == "Alice")
        bob = next(u for u in data["users"] if u["name"] == "Bob")
        assert alice["role"] == "member"
        assert bob["role"] == "admin"


class TestRemove:
    @pytest.fixture(autouse=True)
    def _admin(self, as_admin):
        pass

    def test_remove_member(self, users_file, capsys):
        from tools.manage_users import cmd_remove
        import argparse
        cmd_remove(argparse.Namespace(name="Bob"))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"

        data = _load(users_file)
        assert len(data["users"]) == 1
        assert data["users"][0]["name"] == "Alice"

    def test_remove_admin_fails(self, users_file, capsys):
        from tools.manage_users import cmd_remove
        import argparse
        with pytest.raises(SystemExit):
            cmd_remove(argparse.Namespace(name="Alice"))
        out = json.loads(capsys.readouterr().out)
        assert "Cannot remove the admin" in out["error"]

    def test_remove_nonexistent_fails(self, users_file, capsys):
        from tools.manage_users import cmd_remove
        import argparse
        with pytest.raises(SystemExit):
            cmd_remove(argparse.Namespace(name="Nobody"))


# ── Tests for shared pure-Python functions ───────────────────────────────────

class TestSharedListUsers:
    def test_returns_list(self, users_file):
        from tools.manage_users import list_users
        users = list_users()
        assert len(users) == 2
        assert users[0]["name"] == "Alice"

    def test_empty_registry(self, empty_users_file):
        from tools.manage_users import list_users
        assert list_users() == []


class TestSharedAddUser:
    def test_add_member(self, users_file):
        from tools.manage_users import add_user, list_users
        result = add_user(name="Charlie", role="member", telegram="456")
        assert result["name"] == "Charlie"
        assert result["role"] == "member"
        assert result["channels"]["telegram"] == "456"
        assert len(list_users()) == 3

    def test_add_duplicate_raises(self, users_file):
        from tools.manage_users import add_user
        with pytest.raises(ValueError, match="already exists"):
            add_user(name="Alice")

    def test_add_case_insensitive_duplicate_raises(self, users_file):
        from tools.manage_users import add_user
        with pytest.raises(ValueError, match="already exists"):
            add_user(name="alice")

    def test_add_second_admin_raises(self, users_file):
        from tools.manage_users import add_user
        with pytest.raises(ValueError, match="Admin already exists"):
            add_user(name="Charlie", role="admin")

    def test_add_admin_to_empty_registry(self, empty_users_file):
        from tools.manage_users import add_user
        result = add_user(name="Alice", role="admin", telegram="123")
        assert result["role"] == "admin"

    def test_add_with_both_channels(self, users_file):
        from tools.manage_users import add_user
        result = add_user(name="Dana", telegram="t1", whatsapp="w1")
        assert result["channels"]["telegram"] == "t1"
        assert result["channels"]["whatsapp"] == "w1"


class TestSharedUpdateUser:
    def test_update_channel(self, users_file):
        from tools.manage_users import update_user
        result = update_user(name="Bob", telegram="789")
        assert result["channels"]["telegram"] == "789"
        assert result["channels"]["whatsapp"] == "14155551234"

    def test_rename(self, users_file):
        from tools.manage_users import update_user
        result = update_user(name="Bob", rename="Robert")
        assert result["name"] == "Robert"

    def test_rename_collision_raises(self, users_file):
        from tools.manage_users import update_user
        with pytest.raises(ValueError, match="already exists"):
            update_user(name="Bob", rename="Alice")

    def test_update_nonexistent_raises(self, users_file):
        from tools.manage_users import update_user
        with pytest.raises(KeyError, match="not found"):
            update_user(name="Nobody", telegram="999")

    def test_remove_channel_with_empty_string(self, users_file):
        from tools.manage_users import update_user
        result = update_user(name="Bob", whatsapp="")
        assert "whatsapp" not in result["channels"]

    def test_demote_only_admin_raises(self, users_file):
        from tools.manage_users import update_user
        with pytest.raises(ValueError, match="Cannot demote"):
            update_user(name="Alice", role="member")


class TestSharedRemoveUser:
    def test_remove_member(self, users_file):
        from tools.manage_users import remove_user, list_users
        result = remove_user("Bob")
        assert result["status"] == "removed"
        assert len(list_users()) == 1

    def test_remove_admin_raises(self, users_file):
        from tools.manage_users import remove_user
        with pytest.raises(ValueError, match="Cannot remove the admin"):
            remove_user("Alice")

    def test_remove_nonexistent_raises(self, users_file):
        from tools.manage_users import remove_user
        with pytest.raises(KeyError, match="not found"):
            remove_user("Nobody")


class TestBriefingStyle:
    """briefing_style is a free-form per-user presentation preference."""

    def test_add_with_briefing_style(self, users_file):
        from tools.manage_users import add_user, list_users
        add_user(name="Charlie", briefing_style="dry, no emoji")
        charlie = next(u for u in list_users() if u["name"] == "Charlie")
        assert charlie["briefing_style"] == "dry, no emoji"

    def test_add_without_briefing_style_omits_field(self, users_file):
        from tools.manage_users import add_user, list_users
        add_user(name="Dana")
        dana = next(u for u in list_users() if u["name"] == "Dana")
        assert "briefing_style" not in dana

    def test_update_sets_briefing_style(self, users_file):
        from tools.manage_users import update_user
        result = update_user(name="Bob", briefing_style="hype mode")
        assert result["briefing_style"] == "hype mode"

    def test_update_preserves_other_fields(self, users_file):
        from tools.manage_users import update_user
        result = update_user(name="Bob", briefing_style="plain bullets")
        assert result["channels"]["whatsapp"] == "14155551234"
        assert result["role"] == "member"

    def test_update_with_empty_string_clears(self, users_file):
        from tools.manage_users import add_user, update_user, list_users
        add_user(name="Charlie", briefing_style="dry")
        update_user(name="Charlie", briefing_style="")
        charlie = next(u for u in list_users() if u["name"] == "Charlie")
        assert "briefing_style" not in charlie

    def test_update_none_leaves_existing(self, users_file):
        """Passing briefing_style=None (not provided) must not clear the field."""
        from tools.manage_users import add_user, update_user, list_users
        add_user(name="Charlie", briefing_style="dry")
        update_user(name="Charlie", telegram="999")
        charlie = next(u for u in list_users() if u["name"] == "Charlie")
        assert charlie["briefing_style"] == "dry"


class TestAdminTransfer:
    """Dedicated tests for the admin transfer (atomic swap) logic."""

    def test_promote_member_demotes_current_admin(self, users_file):
        from tools.manage_users import update_user, list_users
        result = update_user(name="Bob", role="admin")
        assert result["role"] == "admin"

        users = {u["name"]: u for u in list_users()}
        assert users["Alice"]["role"] == "member"
        assert users["Bob"]["role"] == "admin"

    def test_promote_preserves_other_fields(self, users_file):
        """Admin transfer should not alter channels or names."""
        from tools.manage_users import update_user, list_users
        update_user(name="Bob", role="admin")

        users = {u["name"]: u for u in list_users()}
        assert users["Alice"]["channels"] == {"telegram": "123"}
        assert users["Bob"]["channels"] == {"whatsapp": "14155551234"}

    def test_set_admin_to_admin_is_noop(self, users_file):
        """Setting an admin's role to 'admin' should succeed (no-op)."""
        from tools.manage_users import update_user, list_users
        result = update_user(name="Alice", role="admin")
        assert result["role"] == "admin"
        # No other user should be demoted
        users = {u["name"]: u for u in list_users()}
        assert users["Alice"]["role"] == "admin"
        assert users["Bob"]["role"] == "member"

    def test_double_swap(self, users_file):
        """Transfer admin to Bob, then back to Alice."""
        from tools.manage_users import update_user, list_users
        update_user(name="Bob", role="admin")
        update_user(name="Alice", role="admin")

        users = {u["name"]: u for u in list_users()}
        assert users["Alice"]["role"] == "admin"
        assert users["Bob"]["role"] == "member"

    def test_transfer_with_three_users(self, users_file):
        """With Alice (admin), Bob, Charlie — promote Charlie."""
        from tools.manage_users import add_user, update_user, list_users
        add_user(name="Charlie")
        update_user(name="Charlie", role="admin")

        users = {u["name"]: u for u in list_users()}
        assert users["Alice"]["role"] == "member"
        assert users["Bob"]["role"] == "member"
        assert users["Charlie"]["role"] == "admin"


class TestAnalyticsEvents:
    """add_user / remove_user fire the PostHog lifecycle events."""

    def test_add_user_fires_household_member_added(self, users_file):
        from tools.manage_users import add_user
        with patch("tools.analytics.events.track_household_member_added") as mock:
            add_user(name="Charlie", role="member", telegram="456")
        assert mock.called
        _, kwargs = mock.call_args
        assert kwargs["member_count_after"] == 3
        assert kwargs["role"] == "member"

    def test_remove_user_fires_household_member_removed(self, users_file):
        from tools.manage_users import remove_user
        with patch("tools.analytics.events.track_household_member_removed") as mock:
            remove_user("Bob")
        assert mock.called
        _, kwargs = mock.call_args
        assert kwargs["member_count_after"] == 1
        assert kwargs["role"] == "member"

    def test_distinct_id_is_canonical_person_hash(self, users_file):
        """The fired distinct_id must match the hash nanobot emits for
        `person:<slug>` so homer + nanobot events attach to one person."""
        from tools.manage_users import add_user
        from tools.analytics.identity import get_person_distinct_id
        with patch("tools.analytics.events.track_household_member_added") as mock:
            add_user(name="Charlie Chaplin")
        assert mock.call_args.args[0] == get_person_distinct_id("Charlie Chaplin")

    def test_analytics_failure_does_not_block_add(self, users_file):
        """A PostHog outage can't prevent a successful add."""
        from tools.manage_users import add_user
        with patch(
            "tools.analytics.events.track_household_member_added",
            side_effect=RuntimeError("posthog down"),
        ):
            result = add_user(name="Charlie")
        assert result["name"] == "Charlie"


class TestRequesterCheck:
    """Privileged CLI commands must refuse unless NANOBOT_SENDER_ID +
    NANOBOT_SENDER_CHANNEL (set by nanobot's runtime, not the LLM) resolve
    to an admin user. Closes SECURITY_ASSESSMENT finding #2."""

    def _add_args(self):
        return argparse.Namespace(
            name="Mallory", role="member", telegram="999",
            whatsapp=None, briefing_style=None,
        )

    def test_refuses_when_env_missing(self, users_file, capsys, monkeypatch):
        monkeypatch.delenv("NANOBOT_SENDER_ID", raising=False)
        monkeypatch.delenv("NANOBOT_SENDER_CHANNEL", raising=False)
        from tools.manage_users import cmd_add
        with pytest.raises(SystemExit):
            cmd_add(self._add_args())
        out = json.loads(capsys.readouterr().out)
        assert out == {"error": "Not authorized."}
        # Side effect: yaml unchanged.
        assert len(_load(users_file)["users"]) == 2

    def test_refuses_when_only_id_set(self, users_file, capsys, monkeypatch):
        monkeypatch.setenv("NANOBOT_SENDER_ID", "123")
        monkeypatch.delenv("NANOBOT_SENDER_CHANNEL", raising=False)
        from tools.manage_users import cmd_add
        with pytest.raises(SystemExit):
            cmd_add(self._add_args())
        assert json.loads(capsys.readouterr().out) == {"error": "Not authorized."}

    def test_refuses_when_only_channel_set(self, users_file, capsys, monkeypatch):
        """Symmetric to test_refuses_when_only_id_set — both branches of the
        partial-env guard must fail closed."""
        monkeypatch.delenv("NANOBOT_SENDER_ID", raising=False)
        monkeypatch.setenv("NANOBOT_SENDER_CHANNEL", "telegram")
        from tools.manage_users import cmd_add
        with pytest.raises(SystemExit):
            cmd_add(self._add_args())
        assert json.loads(capsys.readouterr().out) == {"error": "Not authorized."}

    def test_refuses_unknown_sender(self, users_file, capsys, monkeypatch):
        monkeypatch.setenv("NANOBOT_SENDER_ID", "ghost-42")
        monkeypatch.setenv("NANOBOT_SENDER_CHANNEL", "telegram")
        from tools.manage_users import cmd_add
        with pytest.raises(SystemExit):
            cmd_add(self._add_args())
        assert json.loads(capsys.readouterr().out) == {"error": "Not authorized."}

    def test_refuses_when_yaml_is_corrupt(self, users_file, capsys, as_admin):
        """A malformed users.yaml must fail closed, not crash with a
        traceback that confuses the agent (and fails open if the caller
        retries differently)."""
        users_file.write_text("this is: not: valid: yaml: [[[")
        from tools.manage_users import cmd_add
        with pytest.raises(SystemExit):
            cmd_add(self._add_args())
        assert json.loads(capsys.readouterr().out) == {"error": "Not authorized."}

    def test_resolves_int_typed_channel_id(self, tmp_path, monkeypatch):
        """If users.yaml ever ends up with an int chat_id (hand-edit, schema
        drift), str-coercion must keep the comparison working — env vars
        are always strings."""
        f = tmp_path / "users.yaml"
        _save(f, {"users": [
            # Note: int, not str.
            {"name": "Alice", "role": "admin", "channels": {"telegram": 123}},
        ]})
        monkeypatch.setattr("tools.manage_users.USERS_FILE", f)
        monkeypatch.setenv("NANOBOT_SENDER_ID", "123")
        monkeypatch.setenv("NANOBOT_SENDER_CHANNEL", "telegram")
        from tools.manage_users import _resolve_requester
        user = _resolve_requester()
        # _resolve_requester is internal and returns the v2 record directly
        # (display_name, not name). _require_admin_requester downstream only
        # consults `role`, so the field rename is invisible to callers.
        assert user is not None and user["display_name"] == "Alice"

    def test_refuses_member_sender(self, users_file, capsys, as_member):
        """A member-role user can't escalate themselves to admin."""
        from tools.manage_users import cmd_update
        with pytest.raises(SystemExit):
            cmd_update(argparse.Namespace(
                name="Bob", rename=None, role="admin",
                telegram=None, whatsapp=None, briefing_style=None,
            ))
        assert json.loads(capsys.readouterr().out) == {"error": "Not authorized."}
        # Bob is still a member.
        bob = next(u for u in _load(users_file)["users"] if u["name"] == "Bob")
        assert bob["role"] == "member"

    def test_refuses_member_sender_on_remove(self, users_file, capsys, as_member):
        from tools.manage_users import cmd_remove
        with pytest.raises(SystemExit):
            cmd_remove(argparse.Namespace(name="Alice"))
        assert json.loads(capsys.readouterr().out) == {"error": "Not authorized."}
        # Alice is still there.
        assert len(_load(users_file)["users"]) == 2

    def test_admin_sender_succeeds(self, users_file, capsys, as_admin):
        from tools.manage_users import cmd_add
        cmd_add(self._add_args())
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert out["user"]["name"] == "Mallory"

    def test_list_is_not_gated(self, users_file, capsys, monkeypatch):
        """list is not privileged — the agent reads it constantly to answer
        'who lives here?' and gating it would break normal household use."""
        monkeypatch.delenv("NANOBOT_SENDER_ID", raising=False)
        monkeypatch.delenv("NANOBOT_SENDER_CHANNEL", raising=False)
        from tools.manage_users import cmd_list
        cmd_list(argparse.Namespace())
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2

    def test_pure_function_path_unaffected(self, users_file, monkeypatch):
        """The portal calls add_user/update_user/remove_user directly in
        process. The runtime gate sits only on cmd_*; pure functions stay
        callable so we don't break the portal's authz path."""
        monkeypatch.delenv("NANOBOT_SENDER_ID", raising=False)
        monkeypatch.delenv("NANOBOT_SENDER_CHANNEL", raising=False)
        from tools.manage_users import add_user, remove_user
        u = add_user(name="Zoe", role="member", telegram="789")
        assert u["name"] == "Zoe"
        result = remove_user("Zoe")
        assert result["name"] == "Zoe"

"""Tests for tools/build_identity_map.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tools.build_identity_map import (
    _expand_whatsapp_forms,
    _slugify,
    build_map,
    write_map,
)


@pytest.fixture(autouse=True)
def _isolate_lid_map(tmp_path, monkeypatch):
    """Redirect the default lid_map path to a nonexistent file inside
    tmp_path so tests don't accidentally read the dev's ~/.nanobot/lid_map.json."""
    monkeypatch.setattr(
        "tools.build_identity_map.DEFAULT_LID_MAP",
        tmp_path / "_no_lid_map.json",
    )


def _write_users_yaml(path: Path, users: list[dict]) -> Path:
    path.write_text(yaml.safe_dump({"users": users}, sort_keys=False))
    return path


def _write_lid_map(path: Path, mapping: dict[str, str]) -> Path:
    """Write a lid_map.json in the bridge-ack shape `{lid: {phone: ...}}`."""
    path.write_text(json.dumps({k: {"phone": v} for k, v in mapping.items()}))
    return path


def test_slugify():
    assert _slugify("Alex") == "alex"
    assert _slugify("Alex Johnson") == "alex_johnson"
    assert _slugify("  Alex  ") == "alex"
    assert _slugify("Kemi Johnson-Jnr") == "kemi_johnson_jnr"
    # Unicode / apostrophes collapse to a single underscore.
    assert _slugify("O'Brien") == "o_brien"


def test_build_map_happy_path(tmp_path):
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {
            "name": "Alex",
            "role": "admin",
            "channels": {"whatsapp": "14125550001", "telegram": "5550000001"},
        },
        {
            "name": "Sam",
            "role": "member",
            "channels": {"whatsapp": "15551234567"},
        },
    ])
    m = build_map(users_yaml)
    assert m == {
        "whatsapp:14125550001": "person:alex",
        "telegram:5550000001": "person:alex",
        "whatsapp:15551234567": "person:sam",
    }


def test_build_map_skips_empty_channels(tmp_path):
    """Users with partial channel data (empty strings) don't pollute the map."""
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {
            "name": "Alex",
            "channels": {"whatsapp": "14125550001", "telegram": "", "email": None},
        },
    ])
    m = build_map(users_yaml)
    assert m == {"whatsapp:14125550001": "person:alex"}


def test_build_map_skips_nameless_user(tmp_path):
    """A user with no name can't be canonicalized — skip entirely."""
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {"name": "", "channels": {"whatsapp": "111"}},
        {"name": "Alex", "channels": {"whatsapp": "222"}},
    ])
    m = build_map(users_yaml)
    assert m == {"whatsapp:222": "person:alex"}


def test_build_map_missing_file_returns_empty(tmp_path):
    m = build_map(tmp_path / "nope.yaml")
    assert m == {}


def test_build_map_malformed_yaml_returns_empty(tmp_path):
    bad = tmp_path / "users.yaml"
    bad.write_text("not: valid: yaml: [")
    m = build_map(bad)
    assert m == {}


def test_build_map_no_users_key(tmp_path):
    """A YAML file without a `users` key → empty map, not a crash."""
    path = tmp_path / "users.yaml"
    path.write_text("something: else\n")
    assert build_map(path) == {}


def test_build_map_lowercases_keys(tmp_path):
    """channel and identifier are lowercased so lookups in nanobot can
    normalize both sides the same way."""
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {"name": "Alex", "channels": {"Email": "Alex@Example.COM"}},
    ])
    m = build_map(users_yaml)
    assert "email:alex@example.com" in m
    assert m["email:alex@example.com"] == "person:alex"


def test_write_map_atomic_and_sorted(tmp_path):
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {
            "name": "Alex",
            "channels": {"whatsapp": "14125550001", "telegram": "5550000001"},
        },
    ])
    out = tmp_path / "identity_map.json"
    result_path, count = write_map(output_path=out, users_yaml_path=users_yaml)
    assert result_path == out
    assert count == 2
    assert out.exists()
    # Temp file should be cleaned up after atomic replace.
    assert not (tmp_path / "identity_map.json.tmp").exists()

    data = json.loads(out.read_text())
    # sort_keys=True → deterministic ordering.
    assert list(data.keys()) == sorted(data.keys())


def test_write_map_empty_users_still_writes(tmp_path):
    """No users = empty JSON object on disk — easier for nanobot to handle
    uniformly than "file missing vs. empty map"."""
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [])
    out = tmp_path / "identity_map.json"
    _, count = write_map(output_path=out, users_yaml_path=users_yaml)
    assert count == 0
    assert json.loads(out.read_text()) == {}


def test_write_map_creates_parent_dir(tmp_path):
    """output_path.parent is created if it doesn't exist — so the first
    boot writes the map cleanly without build_context needing to mkdir first."""
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {"name": "Alex", "channels": {"whatsapp": "111"}},
    ])
    out = tmp_path / "deep" / "nested" / "identity_map.json"
    assert not out.parent.exists()
    write_map(output_path=out, users_yaml_path=users_yaml)
    assert out.exists()


def test_build_map_ignores_non_dict_user(tmp_path):
    """A malformed users.yaml with a list entry that isn't a dict shouldn't crash."""
    path = tmp_path / "users.yaml"
    path.write_text(yaml.safe_dump({"users": ["not a dict", {"name": "Alex", "channels": {"whatsapp": "111"}}]}))
    assert build_map(path) == {"whatsapp:111": "person:alex"}


def test_build_map_ignores_non_dict_channels(tmp_path):
    """A user whose channels field is a list instead of a dict is skipped."""
    users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
        {"name": "Alex", "channels": ["telegram", "whatsapp"]},
        {"name": "Sam", "channels": {"whatsapp": "111"}},
    ])
    assert build_map(users_yaml) == {"whatsapp:111": "person:sam"}


# ── WhatsApp LID expansion ───────────────────────────────────────────────


class TestExpandWhatsappForms:
    def test_lid_form_expands_to_lid_bare_and_phone(self):
        lid_to_phone = {"246157477413033": "15551234567"}
        forms = _expand_whatsapp_forms("246157477413033@lid", lid_to_phone)
        assert set(forms) == {
            "246157477413033@lid",
            "246157477413033",
            "15551234567",
        }

    def test_lid_form_without_lid_map_still_expands_to_bare(self):
        """Even without a phone mapping we emit both `@lid` and the bare
        prefix — the runtime may hand us either depending on context."""
        forms = _expand_whatsapp_forms("246157477413033@lid", {})
        assert set(forms) == {"246157477413033@lid", "246157477413033"}

    def test_raw_phone_expands_to_lid_forms_via_reverse_lookup(self):
        lid_to_phone = {"246157477413033": "15551234567"}
        forms = _expand_whatsapp_forms("15551234567", lid_to_phone)
        assert set(forms) == {
            "15551234567",
            "246157477413033",
            "246157477413033@lid",
        }

    def test_raw_phone_without_reverse_mapping_returns_self_only(self):
        forms = _expand_whatsapp_forms("15551234567", {})
        assert forms == ["15551234567"]


class TestDefaultLidMapPath:
    """`NANOBOT_PERSISTENT_DATA_DIR` must reroute the default lid_map path
    to the persistent volume, matching what nanobot's bridge writes to."""

    def test_no_env_falls_back_to_home(self, monkeypatch):
        from tools.build_identity_map import _default_lid_map_path
        monkeypatch.delenv("NANOBOT_PERSISTENT_DATA_DIR", raising=False)
        assert _default_lid_map_path() == Path.home() / ".nanobot" / "lid_map.json"

    def test_env_override_redirects_path(self, monkeypatch, tmp_path):
        from tools.build_identity_map import _default_lid_map_path
        monkeypatch.setenv("NANOBOT_PERSISTENT_DATA_DIR", str(tmp_path / "nanobot"))
        assert _default_lid_map_path() == tmp_path / "nanobot" / "lid_map.json"

    def test_env_override_expands_tilde(self, monkeypatch):
        from tools.build_identity_map import _default_lid_map_path
        monkeypatch.setenv("NANOBOT_PERSISTENT_DATA_DIR", "~/nanobot-persistent")
        assert _default_lid_map_path() == Path.home() / "nanobot-persistent" / "lid_map.json"


class TestBuildMapWhatsapp:
    def test_lid_form_in_users_yaml_produces_phone_entry(self, tmp_path):
        """End-to-end: users.yaml has the LID form; identity_map must
        include the raw phone so nanobot's hook hits on either."""
        users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
            {"name": "Alex Johnson", "channels": {"whatsapp": "246157477413033@lid"}},
        ])
        lid_map = _write_lid_map(tmp_path / "lid_map.json", {
            "246157477413033": "15551234567",
        })
        m = build_map(users_yaml, lid_map_path=lid_map)
        assert m == {
            "whatsapp:246157477413033@lid": "person:alex_johnson",
            "whatsapp:246157477413033": "person:alex_johnson",
            "whatsapp:15551234567": "person:alex_johnson",
        }

    def test_phone_form_in_users_yaml_produces_lid_entries(self, tmp_path):
        users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
            {"name": "Alex", "channels": {"whatsapp": "15551234567"}},
        ])
        lid_map = _write_lid_map(tmp_path / "lid_map.json", {
            "246157477413033": "15551234567",
        })
        m = build_map(users_yaml, lid_map_path=lid_map)
        assert m == {
            "whatsapp:15551234567": "person:alex",
            "whatsapp:246157477413033@lid": "person:alex",
            "whatsapp:246157477413033": "person:alex",
        }

    def test_missing_lid_map_still_works(self, tmp_path):
        """If lid_map.json doesn't exist, emit only the form given in users.yaml —
        no crash, no empty expansions."""
        users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
            {"name": "Alex", "channels": {"whatsapp": "15551234567"}},
        ])
        m = build_map(users_yaml, lid_map_path=tmp_path / "absent.json")
        assert m == {"whatsapp:15551234567": "person:alex"}

    def test_malformed_lid_map_still_works(self, tmp_path):
        """Garbage in lid_map.json is ignored; we fall back to the users.yaml form."""
        users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
            {"name": "Alex", "channels": {"whatsapp": "246157477413033@lid"}},
        ])
        bad = tmp_path / "lid_map.json"
        bad.write_text("not: json: [")
        m = build_map(users_yaml, lid_map_path=bad)
        # No phone emitted (lid_map unreadable); bare form still emitted.
        assert m == {
            "whatsapp:246157477413033@lid": "person:alex",
            "whatsapp:246157477413033": "person:alex",
        }

    def test_telegram_channel_not_affected_by_expansion(self, tmp_path):
        users_yaml = _write_users_yaml(tmp_path / "users.yaml", [
            {"name": "Alex", "channels": {
                "telegram": "5550000001",
                "whatsapp": "246157477413033@lid",
            }},
        ])
        lid_map = _write_lid_map(tmp_path / "lid_map.json", {
            "246157477413033": "15551234567",
        })
        m = build_map(users_yaml, lid_map_path=lid_map)
        # Telegram is a single entry; whatsapp expands to three.
        assert m["telegram:5550000001"] == "person:alex"
        assert sum(1 for k in m if k.startswith("telegram:")) == 1
        assert sum(1 for k in m if k.startswith("whatsapp:")) == 3

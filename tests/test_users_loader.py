"""Tests for tools/users_loader.py — the canonical users.yaml reader/writer."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.users_loader import (
    ADMIN_SYMBOL,
    CURRENT_SCHEMA_VERSION,
    as_legacy_list,
    derive_symbol,
    find_by_channel_handle,
    find_by_display_name,
    iter_users,
    load_users,
    normalize,
    resolve_handle,
    save_users,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _v1_doc(*users: dict) -> dict:
    return {"users": list(users)}


def _v1_alice_bob() -> dict:
    return _v1_doc(
        {"name": "Alice", "role": "admin", "channels": {"whatsapp": "wa-alice"}},
        {"name": "Bob", "role": "member", "channels": {"telegram": "tg-bob"}},
    )


def _v2_alice_bob() -> dict:
    return {
        "schema_version": 2,
        "users": {
            "primary": {
                "display_name": "Alice",
                "role": "admin",
                "channels": {"whatsapp": "wa-alice"},
            },
            "bob": {
                "display_name": "Bob",
                "role": "member",
                "channels": {"telegram": "tg-bob"},
            },
        },
    }


@pytest.fixture
def v1_file(tmp_path: Path) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump(_v1_alice_bob(), sort_keys=False))
    return p


@pytest.fixture
def v2_file(tmp_path: Path) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump(_v2_alice_bob(), sort_keys=False))
    return p


# ── derive_symbol ────────────────────────────────────────────────────────────


class TestDeriveSymbol:
    def test_admin_always_primary(self):
        assert derive_symbol("Anyone", "admin", set()) == "primary"
        assert derive_symbol("Whoever", "admin", {"primary", "alice"}) == "primary"

    def test_member_first_name_lower(self):
        assert derive_symbol("Seun Anjorin", "member", set()) == "seun"
        assert derive_symbol("ALEX-PETROV", "member", set()) == "alexpetrov"

    def test_collision_appends_suffix(self):
        assert derive_symbol("Alex", "member", {"alex"}) == "alex_2"
        assert derive_symbol("Alex", "member", {"alex", "alex_2"}) == "alex_3"

    def test_empty_name_falls_back(self):
        assert derive_symbol("", "member", set()) == "user"

    def test_unicode_first_name_stripped(self):
        # Diacritics outside [a-z0-9_] get stripped — match build_identity_map's slug.
        assert derive_symbol("Émile", "member", set()) == "mile"

    def test_member_named_primary_disambiguates(self):
        # A member named "Primary" must not steal the admin slot.
        assert derive_symbol("Primary", "member", set()) == "primary_user"


# ── normalize / load_users ───────────────────────────────────────────────────


class TestNormalize:
    def test_v1_converts_to_v2(self):
        out = normalize(_v1_alice_bob())
        assert out["schema_version"] == CURRENT_SCHEMA_VERSION
        assert set(out["users"].keys()) == {"primary", "bob"}
        assert out["users"]["primary"]["display_name"] == "Alice"
        assert out["users"]["primary"]["role"] == "admin"
        assert out["users"]["bob"]["display_name"] == "Bob"

    def test_v2_passes_through(self):
        out = normalize(_v2_alice_bob())
        assert out["users"]["primary"]["display_name"] == "Alice"
        assert "name" not in out["users"]["primary"]

    def test_v1_drops_name_promotes_display_name(self):
        out = normalize(_v1_alice_bob())
        assert "name" not in out["users"]["primary"]
        assert out["users"]["primary"]["display_name"] == "Alice"

    def test_v1_preserves_briefing_style(self):
        v1 = _v1_doc({"name": "Alice", "role": "admin", "briefing_style": "dry"})
        out = normalize(v1)
        assert out["users"]["primary"]["briefing_style"] == "dry"

    def test_v1_preserves_unknown_keys(self):
        # Hand-edited fields shouldn't silently disappear.
        v1 = _v1_doc({"name": "Alice", "role": "admin", "pronouns": "she/her"})
        out = normalize(v1)
        assert out["users"]["primary"]["pronouns"] == "she/her"

    def test_v1_skips_non_dict_entries(self):
        v1 = _v1_doc({"name": "Alice", "role": "admin"})
        v1["users"].insert(0, "not a record")
        out = normalize(v1)
        assert set(out["users"].keys()) == {"primary"}

    def test_v1_skips_empty_name(self):
        v1 = _v1_doc({"name": "", "role": "member"}, {"name": "Bob", "role": "member"})
        out = normalize(v1)
        assert set(out["users"].keys()) == {"bob"}

    def test_v1_tolerates_non_dict_channels(self):
        # Hand-edited file with channels as a list shouldn't crash the loader.
        # The bad channels value is dropped entirely (key omitted) rather than
        # preserved as an empty dict.
        v1 = _v1_doc({"name": "Alice", "role": "admin", "channels": ["a", "b"]})
        out = normalize(v1)
        assert "channels" not in out["users"]["primary"]

    def test_empty_input_returns_empty_v2(self):
        out = normalize({})
        assert out["schema_version"] == CURRENT_SCHEMA_VERSION
        assert out["users"] == {}

    def test_v1_admin_always_takes_primary_even_if_listed_second(self):
        v1 = _v1_doc(
            {"name": "Bob", "role": "member"},
            {"name": "Alice", "role": "admin"},
        )
        out = normalize(v1)
        assert out["users"]["primary"]["display_name"] == "Alice"
        assert "bob" in out["users"]


class TestLoadUsers:
    def test_missing_file_returns_empty(self, tmp_path):
        out = load_users(tmp_path / "does_not_exist.yaml")
        assert out == {"schema_version": CURRENT_SCHEMA_VERSION, "users": {}}

    def test_loads_v1_file_as_v2(self, v1_file):
        out = load_users(v1_file)
        assert out["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "primary" in out["users"]
        assert "bob" in out["users"]

    def test_loads_v2_file(self, v2_file):
        out = load_users(v2_file)
        assert out["users"]["primary"]["display_name"] == "Alice"

    def test_invalid_top_level_raises(self, tmp_path):
        p = tmp_path / "users.yaml"
        p.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="not a mapping"):
            load_users(p)

    def test_env_override(self, tmp_path, monkeypatch):
        p = tmp_path / "elsewhere.yaml"
        p.write_text(yaml.safe_dump(_v2_alice_bob(), sort_keys=False))
        monkeypatch.setenv("HOMER_USERS_YAML", str(p))
        out = load_users()  # no explicit path
        assert "primary" in out["users"]


# ── save_users round-trip ────────────────────────────────────────────────────


class TestSaveUsers:
    def test_v1_on_disk_writes_v2_back(self, v1_file):
        data = load_users(v1_file)
        save_users(data, v1_file)
        raw = yaml.safe_load(v1_file.read_text())
        assert raw["schema_version"] == CURRENT_SCHEMA_VERSION
        assert isinstance(raw["users"], dict)
        assert "primary" in raw["users"]

    def test_save_create_parent_dirs(self, tmp_path):
        nested = tmp_path / "a" / "b" / "users.yaml"
        save_users(_v2_alice_bob(), nested)
        assert nested.exists()

    def test_save_then_load_roundtrip(self, tmp_path):
        p = tmp_path / "users.yaml"
        save_users(_v2_alice_bob(), p)
        out = load_users(p)
        assert out["users"]["primary"]["display_name"] == "Alice"
        assert out["users"]["bob"]["display_name"] == "Bob"


# ── Lookup helpers ───────────────────────────────────────────────────────────


class TestIterUsers:
    def test_admin_first(self):
        data = _v2_alice_bob()
        symbols = [s for s, _ in iter_users(data)]
        assert symbols[0] == "primary"

    def test_empty(self):
        assert list(iter_users({})) == []


class TestFindByDisplayName:
    def test_case_insensitive(self):
        data = _v2_alice_bob()
        sym, rec = find_by_display_name(data, "alice")
        assert sym == "primary" and rec["role"] == "admin"

    def test_unknown_returns_none(self):
        sym, rec = find_by_display_name(_v2_alice_bob(), "Nobody")
        assert (sym, rec) == (None, None)


class TestFindByChannelHandle:
    def test_finds_by_whatsapp(self):
        sym, rec = find_by_channel_handle(_v2_alice_bob(), "whatsapp", "wa-alice")
        assert sym == "primary"
        assert rec["display_name"] == "Alice"

    def test_no_match_returns_none(self):
        sym, _ = find_by_channel_handle(_v2_alice_bob(), "whatsapp", "wa-nobody")
        assert sym is None

    def test_wrong_channel_returns_none(self):
        # Alice's WA handle wouldn't match against the telegram channel.
        sym, _ = find_by_channel_handle(_v2_alice_bob(), "telegram", "wa-alice")
        assert sym is None


# ── resolve_handle ───────────────────────────────────────────────────────────


class TestResolveHandle:
    def test_resolves_v1_file(self, v1_file):
        assert resolve_handle("primary", "whatsapp", v1_file) == "wa-alice"

    def test_resolves_v2_file(self, v2_file):
        assert resolve_handle("bob", "telegram", v2_file) == "tg-bob"

    def test_unknown_symbol_raises(self, v2_file):
        with pytest.raises(KeyError, match="unknown symbol"):
            resolve_handle("nobody", "whatsapp", v2_file)

    def test_unknown_channel_raises(self, v2_file):
        with pytest.raises(KeyError, match="no 'email' channel"):
            resolve_handle("primary", "email", v2_file)

    def test_empty_handle_raises(self, tmp_path):
        # A user with a channel key present but empty value still fails —
        # silent fallbacks are exactly the failure mode this resolver fixes.
        p = tmp_path / "users.yaml"
        save_users({
            "schema_version": 2,
            "users": {"primary": {
                "display_name": "Alice",
                "role": "admin",
                "channels": {"whatsapp": ""},
            }},
        }, p)
        with pytest.raises(KeyError, match="empty"):
            resolve_handle("primary", "whatsapp", p)


# ── as_legacy_list (back-compat shim) ────────────────────────────────────────


class TestAsLegacyList:
    def test_emits_name_field(self):
        out = as_legacy_list(_v2_alice_bob())
        names = [u["name"] for u in out]
        assert names == ["Alice", "Bob"]  # admin first

    def test_preserves_channels(self):
        out = as_legacy_list(_v2_alice_bob())
        assert out[0]["channels"]["whatsapp"] == "wa-alice"

    def test_empty_input(self):
        assert as_legacy_list({}) == []

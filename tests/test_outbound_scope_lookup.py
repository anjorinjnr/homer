"""Tests for tools/outbound_scope_lookup.py — homer's plug-in for nanobot's scope_guard."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
import yaml

import tools.scope_store as ss


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def lookup(tmp_path, monkeypatch):
    """Provide an isolated outbound_scope_lookup module with a fresh users.yaml.

    Each test gets its own DB + users.yaml so the mtime cache, scope store,
    and member set start clean. The module is reloaded so its module-level
    cache (``_cached_mtime``, ``_cached_members``) is reset between tests.
    """
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text(yaml.safe_dump({
        "users": [
            {
                "name": "Alex",
                "role": "admin",
                "channels": {
                    "telegram": "5550000001",
                    "whatsapp": "14125550001",
                    "email": "alex@example.com",
                },
            },
            {
                "name": "Sam",
                "role": "member",
                "channels": {"whatsapp": "+1 (412) 555-1234"},
            },
        ]
    }))

    # Isolated scope DB.
    db_path = tmp_path / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db_path))
    monkeypatch.setenv("HOMER_USERS_YAML", str(users_yaml))
    # Force a fresh import so the module-level mtime cache is empty.
    if "tools.outbound_scope_lookup" in importlib.sys.modules:
        del importlib.sys.modules["tools.outbound_scope_lookup"]
    if "outbound_scope_lookup" in importlib.sys.modules:
        del importlib.sys.modules["outbound_scope_lookup"]
    mod = importlib.import_module("tools.outbound_scope_lookup")
    # Reset scope_store schema cache so the new DB gets initialized.
    ss._SCHEMA_INITIALISED.clear()
    return mod, users_yaml


def _make_interaction(
    *, name: str, participant_id: str, channel: str = "whatsapp",
    email: str | None = None, purpose: str = "fix the leak", mode: str = "two_way",
) -> str:
    """Build + persist an interaction scope. Returns scope_id."""
    scope_id = f"int_{name.lower().replace(' ', '_')}"
    env = ss.make_interaction_envelope(
        scope_id=scope_id,
        name=name,
        participant_id=participant_id,
        channel=channel,
        email=email,
        purpose=purpose,
        mode=mode,
    )
    ss.create_scope(env)
    return scope_id


# ── Household-member bypass ──────────────────────────────────────────────────


class TestHouseholdMember:
    def test_whatsapp_jid_matches_member(self, lookup):
        mod, _ = lookup
        result = mod.resolve("whatsapp", "14125550001@s.whatsapp.net")
        assert result.authorized is True
        assert result.reason == "household_member"

    def test_whatsapp_bare_digits_matches_member(self, lookup):
        mod, _ = lookup
        # An agent that emits the bare phone (uncommon but possible) should
        # still be recognized as a member.
        result = mod.resolve("whatsapp", "14125550001")
        assert result.authorized is True

    def test_telegram_member_with_tg_prefix(self, lookup):
        mod, _ = lookup
        result = mod.resolve("telegram", "tg:5550000001")
        assert result.authorized is True
        assert result.reason == "household_member"

    def test_telegram_member_bare_id(self, lookup):
        mod, _ = lookup
        result = mod.resolve("telegram", "5550000001")
        assert result.authorized is True

    def test_email_member(self, lookup):
        mod, _ = lookup
        # Mixed-case + dot variants normalize the same way (Gmail rules) —
        # but example.com isn't gmail so casing alone changes.
        result = mod.resolve("email", "ALEX@EXAMPLE.COM")
        assert result.authorized is True
        assert result.reason == "household_member"

    def test_member_phone_with_punctuation(self, lookup):
        """users.yaml may store '+1 (412) 555-1234' style numbers; lookups
        should still match on digit content."""
        mod, _ = lookup
        result = mod.resolve("whatsapp", "14125551234@s.whatsapp.net")
        assert result.authorized is True


# ── Active interaction scope ─────────────────────────────────────────────────


class TestActiveInteractionScope:
    def test_with_purpose_allowed(self, lookup):
        mod, _ = lookup
        scope_id = _make_interaction(
            name="Bob", participant_id="15551239999@s.whatsapp.net",
            purpose="Quote for exterior painting",
        )
        result = mod.resolve("whatsapp", "15551239999@s.whatsapp.net")
        assert result.authorized is True
        assert result.reason == "active_scope"
        assert scope_id in result.scope_ids

    def test_terminated_scope_rejected(self, lookup):
        mod, _ = lookup
        scope_id = _make_interaction(
            name="Bob", participant_id="15551239999@s.whatsapp.net",
            purpose="Stale conversation",
        )
        ss.terminate_scope(scope_id)
        result = mod.resolve("whatsapp", "15551239999@s.whatsapp.net")
        assert result.authorized is False
        assert result.reason == "no_scope"

    def test_email_scope_match_by_address(self, lookup):
        mod, _ = lookup
        _make_interaction(
            name="Acme Plumbing",
            participant_id="info@acmeplumbing.com",
            channel="email",
            purpose="Water heater replacement",
        )
        result = mod.resolve("email", "info@acmeplumbing.com")
        assert result.authorized is True
        assert result.reason == "active_scope"

    def test_email_scope_normalizes_case(self, lookup):
        mod, _ = lookup
        _make_interaction(
            name="Acme Plumbing",
            participant_id="info@acmeplumbing.com",
            channel="email",
            purpose="Water heater replacement",
        )
        result = mod.resolve("email", "Info@AcmePlumbing.com")
        assert result.authorized is True

    def test_no_scope_returns_remediation(self, lookup):
        mod, _ = lookup
        result = mod.resolve("whatsapp", "+15551234567")
        assert result.authorized is False
        assert result.reason == "no_scope"
        assert "manage_interaction" in (result.remediation or "")
        assert "--phone" in (result.remediation or "")

    def test_scope_with_empty_purpose_refused_with_context_reason(self, lookup):
        """A scope that exists but carries no purpose / event ref is the exact
        "ACL-only entry" failure mode this guard exists to refuse — it must
        come back as `scope_no_context`, not `no_scope`."""
        mod, _ = lookup
        # Build the envelope by hand so we can leave context_layers.injected empty.
        scope_id = "int_silent"
        env = ss.make_interaction_envelope(
            scope_id=scope_id, name="Silent",
            participant_id="15551291234@s.whatsapp.net",
            channel="whatsapp", purpose="",
        )
        ss.create_scope(env)
        result = mod.resolve("whatsapp", "15551291234@s.whatsapp.net")
        assert result.authorized is False
        assert result.reason == "scope_no_context"
        assert scope_id in result.scope_ids

    def test_family_history_scope_authorized_without_injected_purpose(self, lookup):
        """family_history scopes encode context via scope_type (the contributor's
        role IS the purpose). Treating them as scope_no_context would strand
        legitimate inbound contributor sends — this is a regression guard for
        the 2026-05-07 audit on Alex's tenant that surfaced exactly this case.
        """
        mod, _ = lookup
        scope_id = "family_history_contributor"
        env = {
            "scope_id": scope_id,
            "scope_type": "family_history",
            "participants": [{
                "party_id": "14126364194@s.whatsapp.net",
                "name": "Contributor",
                "channel": "whatsapp",
            }],
            "context_layers": {"injected": [], "accumulated": []},
            "authorization": {"granted_capabilities": ["message"]},
            "lifecycle": {"last_active": None},
        }
        ss.create_scope(env)
        result = mod.resolve("whatsapp", "14126364194@s.whatsapp.net")
        assert result.authorized is True
        assert result.reason == "active_scope"
        assert scope_id in result.scope_ids

    def test_event_guest_scope_without_context_source_still_authorized(self, lookup):
        """An event-guest envelope without an explicit context_source still
        authorizes — the scope_type ('relationship' or any non-interaction
        type) is itself the purpose."""
        mod, _ = lookup
        env = ss.make_minimal_envelope(
            scope_id="rel_event_guest",
            name="Adam",
            participant_id="15551294567@s.whatsapp.net",
            event_id="some_event",
        )
        # make_minimal_envelope only sets context_source when explicitly passed
        # — leave it absent to exercise the "scope_type alone" path.
        assert "context_source" not in env
        ss.create_scope(env)
        result = mod.resolve("whatsapp", "15551294567@s.whatsapp.net")
        assert result.authorized is True
        assert result.reason == "active_scope"

    def test_remediation_mentions_group_flag_for_group_jids(self, lookup):
        mod, _ = lookup
        result = mod.resolve("whatsapp", "120363042000000000@g.us")
        assert result.authorized is False
        assert "--whatsapp-group" in (result.remediation or "")

    def test_remediation_mentions_email_flag_for_email(self, lookup):
        mod, _ = lookup
        result = mod.resolve("email", "stranger@example.com")
        assert "--channel email" in (result.remediation or "")


# ── No-reply mode ────────────────────────────────────────────────────────────


class TestNoReplyMode:
    def test_outbound_allowed(self, lookup):
        mod, _ = lookup
        scope_id = _make_interaction(
            name="Vendor", participant_id="15551240000@s.whatsapp.net",
            purpose="One-way nudge — no reply expected", mode="no_reply",
        )
        result = mod.resolve("whatsapp", "15551240000@s.whatsapp.net")
        assert result.authorized is True
        assert result.reason == "no_reply_scope"
        assert result.suppress_inbound is True
        assert scope_id in result.scope_ids

    def test_inbound_suppressed_via_check_inbound_suppressed(self, lookup):
        mod, _ = lookup
        _make_interaction(
            name="Vendor", participant_id="15551240000@s.whatsapp.net",
            purpose="One-way nudge", mode="no_reply",
        )
        # The nanobot side calls scope_guard.check_inbound_suppressed which
        # delegates to whatever lookup the host installed. Wire ours and
        # confirm the suppression bit is set.
        from nanobot.channels import scope_guard
        scope_guard.set_scope_lookup(mod.resolve)
        try:
            assert scope_guard.check_inbound_suppressed(
                "whatsapp", "15551240000@s.whatsapp.net"
            ) is True
        finally:
            scope_guard.set_scope_lookup(None)

    def test_two_way_scope_does_not_suppress_inbound(self, lookup):
        mod, _ = lookup
        _make_interaction(
            name="Bob", participant_id="15551239999@s.whatsapp.net",
            purpose="Quote",
        )
        from nanobot.channels import scope_guard
        scope_guard.set_scope_lookup(mod.resolve)
        try:
            assert scope_guard.check_inbound_suppressed(
                "whatsapp", "15551239999@s.whatsapp.net"
            ) is False
        finally:
            scope_guard.set_scope_lookup(None)


# ── users.yaml hot reload ────────────────────────────────────────────────────


class TestUsersYamlHotReload:
    def test_new_member_picked_up_without_restart(self, lookup):
        mod, users_yaml = lookup
        # Initially, this number has no scope and isn't a member.
        result = mod.resolve("whatsapp", "15559990000@s.whatsapp.net")
        assert result.authorized is False

        # Add the number to users.yaml — bumping mtime invalidates the cache.
        data = yaml.safe_load(users_yaml.read_text())
        data["users"].append({
            "name": "Cousin",
            "role": "member",
            "channels": {"whatsapp": "15559990000"},
        })
        users_yaml.write_text(yaml.safe_dump(data))
        # Force mtime to actually differ — write_text may collide with the
        # original mtime on coarse-grained filesystems. os.utime nudges it.
        st = users_yaml.stat()
        os.utime(users_yaml, (st.st_atime, st.st_mtime + 1))

        result = mod.resolve("whatsapp", "15559990000@s.whatsapp.net")
        assert result.authorized is True
        assert result.reason == "household_member"

    def test_unchanged_yaml_uses_cached_members(self, lookup):
        mod, users_yaml = lookup
        # First call populates the cache.
        mod.resolve("whatsapp", "14125550001@s.whatsapp.net")
        cached = mod._cached_members
        # Second call shouldn't rebuild — same dict identity.
        mod.resolve("whatsapp", "14125550001@s.whatsapp.net")
        assert mod._cached_members is cached

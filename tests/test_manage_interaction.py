"""Tests for manage_interaction.py — ad-hoc interaction scope management."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tools.manage_interaction as mi
import tools.scope_store as ss

# Use the same manage_guest module that manage_interaction imported
mg = mi.mg


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Set up isolated environment for interaction tests."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Guest nanobot config
    guest_config_path = tmp_path / "guest_config.json"
    guest_config_path.write_text(json.dumps({
        "channels": {
            "whatsapp": {"allow_from": []},
            "telegram": {"allowFrom": []},
            "email": {"allowFrom": []},
        }
    }))
    # Main nanobot config (unchanged by interaction ops)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "channels": {
            "whatsapp": {"allow_from": ["owner@s.whatsapp.net"]},
            "telegram": {"allowFrom": []},
        }
    }))

    monkeypatch.setattr(mg, "ACL_FILE", events_dir / "guest_agent_acl.json")
    monkeypatch.setattr(mg, "NANOBOT_CONFIG_PATH", config_path)
    monkeypatch.setattr(mg, "GUEST_NANOBOT_CONFIG_PATH", guest_config_path)
    monkeypatch.setattr(mg, "rebuild_context", lambda: None)
    monkeypatch.setattr(mg, "restart_service", lambda service="homer-guest": True)

    # Isolated scope DB
    (tmp_path / "context").mkdir(exist_ok=True)
    db_path = tmp_path / "context" / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db_path))

    return tmp_path


# ── TestCreateInteraction ─────────────────────────────────────────────────────

class TestCreateInteraction:
    def test_creates_scope_in_db(self, env):
        result = mi.create_interaction(
            name="Bob the Painter", phone="+15551234567",
            purpose="Quote for exterior painting",
        )
        assert result["status"] == "created"
        assert result["scope_created"] is True
        scope = ss.get_scope(result["scope_id"])
        assert scope is not None
        assert scope["scope_type"] == "interaction"

    def test_scope_id_format(self, env):
        result = mi.create_interaction(
            name="Bob the Painter", phone="+15551234567",
        )
        assert result["scope_id"].startswith("int_")
        assert "bob" in result["scope_id"]

    def test_default_30d_expiry(self, env):
        result = mi.create_interaction(
            name="Bob", phone="+15551234567",
        )
        expected = (datetime.now(ZoneInfo("America/New_York"))
                    + timedelta(days=30)).strftime("%Y-%m-%d")
        assert result["expires"] == expected

    def test_custom_expiry(self, env):
        result = mi.create_interaction(
            name="Bob", phone="+15551234567",
            expires="2026-12-31",
        )
        assert result["expires"] == "2026-12-31"

    def test_adds_to_acl(self, env):
        result = mi.create_interaction(
            name="Bob", phone="+15551234567",
        )
        acl = mg.load_acl()
        assert "15551234567@s.whatsapp.net" in acl
        entry = acl["15551234567@s.whatsapp.net"]
        assert entry["name"] == "Bob"
        assert entry["channel"] == "whatsapp"
        assert entry["interaction_id"] == result["scope_id"]

    def test_purpose_in_injected_context(self, env):
        result = mi.create_interaction(
            name="Bob", phone="+15551234567",
            purpose="Lawn maintenance",
        )
        scope = ss.get_scope(result["scope_id"])
        injected = scope["context_layers"]["injected"]
        assert len(injected) == 1
        assert injected[0]["content"] == "Lawn maintenance"

    def test_idempotent_on_duplicate(self, env):
        r1 = mi.create_interaction(name="Bob", phone="+15551234567", purpose="First")
        r2 = mi.create_interaction(name="Bob", phone="+15551234567", purpose="Second")
        assert r2["status"] == "exists"
        assert r2["scope_id"] == r1["scope_id"]
        assert r2["scope_created"] is False

    def test_email_in_scope_email_index(self, env):
        mi.create_interaction(
            name="Acme", channel="email", email="info@acme.com",
        )
        scopes = ss.get_scopes_for_email("info@acme.com")
        assert len(scopes) == 1
        assert scopes[0]["scope_type"] == "interaction"

    def test_scope_id_collision_appends_suffix(self, env):
        r1 = mi.create_interaction(name="Bob", phone="+15551234567")
        # Create another "Bob" with a different number
        r2 = mi.create_interaction(name="Bob", phone="+15559999999")
        assert r2["scope_id"] != r1["scope_id"]
        assert r2["scope_id"].startswith("int_bob")


# ── TestCreateInteractionChannels ─────────────────────────────────────────────

class TestCreateInteractionChannels:
    def test_whatsapp(self, env):
        result = mi.create_interaction(
            name="Bob", channel="whatsapp", phone="+15551234567",
        )
        assert result["contact"] == "15551234567@s.whatsapp.net"
        scope = ss.get_scope(result["scope_id"])
        assert scope["participants"][0]["channel"] == "whatsapp"

    def test_telegram(self, env):
        result = mi.create_interaction(
            name="Jake", channel="telegram", telegram_id="123456",
        )
        assert result["contact"] == "tg:123456"
        scope = ss.get_scope(result["scope_id"])
        assert scope["participants"][0]["channel"] == "telegram"

    def test_email(self, env):
        result = mi.create_interaction(
            name="Acme", channel="email", email="info@acme.com",
        )
        assert result["contact"] == "info@acme.com"
        scope = ss.get_scope(result["scope_id"])
        p = scope["participants"][0]
        assert p["channel"] == "email"
        assert p["email"] == "info@acme.com"

    def test_whatsapp_with_email(self, env):
        result = mi.create_interaction(
            name="Bob", phone="+15551234567", email="bob@painters.co",
        )
        scope = ss.get_scope(result["scope_id"])
        assert scope["participants"][0]["email"] == "bob@painters.co"
        # Both participant_id and email should be in ACL
        acl = mg.load_acl()
        assert "15551234567@s.whatsapp.net" in acl
        assert "bob@painters.co" in acl

    def test_whatsapp_missing_phone_raises(self, env):
        with pytest.raises(ValueError, match="--phone"):
            mi.create_interaction(name="Bob", channel="whatsapp")

    def test_telegram_missing_id_raises(self, env):
        with pytest.raises(ValueError, match="--telegram-id"):
            mi.create_interaction(name="Jake", channel="telegram")

    def test_email_missing_email_raises(self, env):
        with pytest.raises(ValueError, match="--email"):
            mi.create_interaction(name="Acme", channel="email")

    def test_idempotent_by_email(self, env):
        """If email already has an interaction scope, return it."""
        r1 = mi.create_interaction(
            name="Acme", channel="email", email="info@acme.com",
        )
        r2 = mi.create_interaction(
            name="Acme Plumbing", channel="email", email="info@acme.com",
        )
        assert r2["status"] == "exists"
        assert r2["scope_id"] == r1["scope_id"]


# ── TestListInteractions ──────────────────────────────────────────────────────

class TestListInteractions:
    def test_lists_interaction_scopes(self, env):
        mi.create_interaction(name="Bob", phone="+15551234567", purpose="Painting")
        mi.create_interaction(name="Acme", channel="email", email="a@b.com", purpose="Plumbing")
        interactions = mi.list_interactions()
        assert len(interactions) == 2
        names = {i["name"] for i in interactions}
        assert names == {"Bob", "Acme"}

    def test_excludes_event_scopes(self, env):
        # Create an event-type scope directly
        event_env = ss.make_minimal_envelope(
            scope_id="mtb_colorado", name="Jake",
            participant_id="15559999999@s.whatsapp.net",
            event_id="mtb_colorado",
        )
        ss.create_scope(event_env)
        # Create an interaction scope
        mi.create_interaction(name="Bob", phone="+15551234567")
        interactions = mi.list_interactions()
        assert len(interactions) == 1
        assert interactions[0]["name"] == "Bob"

    def test_excludes_terminated(self, env):
        result = mi.create_interaction(name="Bob", phone="+15551234567")
        ss.terminate_scope(result["scope_id"])
        interactions = mi.list_interactions()
        assert len(interactions) == 0

    def test_includes_purpose(self, env):
        mi.create_interaction(
            name="Bob", phone="+15551234567", purpose="Quote for painting",
        )
        interactions = mi.list_interactions()
        assert interactions[0]["purpose"] == "Quote for painting"


# ── TestCloseInteraction ──────────────────────────────────────────────────────

class TestCloseInteraction:
    def test_terminates_scope(self, env):
        result = mi.create_interaction(name="Bob", phone="+15551234567")
        scope_id = result["scope_id"]
        mi.close_interaction(scope_id)
        scope = ss.get_scope(scope_id)
        assert scope["_status"] == "terminated"

    def test_removes_from_acl(self, env):
        result = mi.create_interaction(name="Bob", phone="+15551234567")
        mi.close_interaction(result["scope_id"])
        acl = mg.load_acl()
        assert "15551234567@s.whatsapp.net" not in acl

    def test_removes_email_from_acl(self, env):
        result = mi.create_interaction(
            name="Bob", phone="+15551234567", email="bob@painters.co",
        )
        mi.close_interaction(result["scope_id"])
        acl = mg.load_acl()
        assert "bob@painters.co" not in acl

    def test_nonexistent_scope_raises(self, env):
        with pytest.raises(ValueError, match="not found"):
            mi.close_interaction("int_nonexistent")

    def test_non_interaction_scope_raises(self, env):
        event_env = ss.make_minimal_envelope(
            scope_id="mtb_colorado", name="Jake",
            participant_id="15559999999@s.whatsapp.net",
            event_id="mtb_colorado",
        )
        ss.create_scope(event_env)
        with pytest.raises(ValueError, match="not an interaction"):
            mi.close_interaction("mtb_colorado")

"""Tests for accumulate_context.py — accumulated context persistence and rendering."""

import json
import sqlite3
from pathlib import Path
from datetime import datetime

import pytest

import tools.accumulate_context as ac
import tools.scope_store as ss


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Provide a fresh scope DB."""
    db_path = tmp_path / "scopes.db"
    # Initialise tables via scope_store
    conn = ss.get_conn(db_path)
    conn.close()
    return db_path


def _make_envelope(scope_id="rel_123_denver_mtb", event_id="denver_mtb"):
    env = ss.make_minimal_envelope(
        scope_id=scope_id,
        name="Ugo",
        participant_id="14125550001@s.whatsapp.net",
        event_id=event_id,
    )
    # Ensure scope_id matches what we passed (make_minimal_envelope uses it directly)
    env["scope_id"] = scope_id
    return env


# ── Core accumulation ────────────────────────────────────────────────────────


class TestAccumulate:
    def test_accumulates_fragment(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)

        result = ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="Ugo confirmed he can make April 24-26",
            source_interaction="msg_abc123",
            db_path=db,
            rebuild=False,
        )

        assert result["ok"] is True
        assert result["fragment_id"].startswith("acc_")
        assert result["accumulated_count"] == 1

    def test_fragment_appears_in_envelope(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)

        ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="Ugo prefers morning rides",
            source_interaction="msg_def456",
            db_path=db,
            rebuild=False,
        )

        stored = ss.get_scope("rel_123_denver_mtb", db)
        accumulated = stored["context_layers"]["accumulated"]
        assert len(accumulated) == 1
        assert accumulated[0]["content"] == "Ugo prefers morning rides"
        assert accumulated[0]["source_interaction_id"] == "msg_def456"
        assert accumulated[0]["prunable"] is True
        assert accumulated[0]["timestamp"]  # non-empty

    def test_guest_attribution_stored(self, db):
        """The guest field is persisted in the fragment."""
        env = _make_envelope()
        ss.create_scope(env, db)

        ac.accumulate(
            scope_id="rel_123_denver_mtb",
            guest="Ugo",
            content="Prefers drivable trips over flights",
            db_path=db,
            rebuild=False,
        )

        stored = ss.get_scope("rel_123_denver_mtb", db)
        frag = stored["context_layers"]["accumulated"][0]
        assert frag["guest"] == "Ugo"
        assert frag["content"] == "Prefers drivable trips over flights"

    def test_guest_defaults_to_empty(self, db):
        """Omitting guest stores an empty string (backward-compatible)."""
        env = _make_envelope()
        ss.create_scope(env, db)

        ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="General observation",
            db_path=db,
            rebuild=False,
        )

        stored = ss.get_scope("rel_123_denver_mtb", db)
        frag = stored["context_layers"]["accumulated"][0]
        assert frag["guest"] == ""

    def test_multiple_fragments_accumulate(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)

        ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="Ugo confirmed April 24-26",
            db_path=db,
            rebuild=False,
        )
        ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="Ugo asked about e-bike rental options",
            db_path=db,
            rebuild=False,
        )
        ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="Ugo has a dietary restriction: no shellfish",
            db_path=db,
            rebuild=False,
        )

        stored = ss.get_scope("rel_123_denver_mtb", db)
        accumulated = stored["context_layers"]["accumulated"]
        assert len(accumulated) == 3
        contents = [f["content"] for f in accumulated]
        assert "Ugo confirmed April 24-26" in contents
        assert "Ugo asked about e-bike rental options" in contents
        assert "Ugo has a dietary restriction: no shellfish" in contents

    def test_fragment_ids_are_unique(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)

        r1 = ac.accumulate(
            scope_id="rel_123_denver_mtb", content="Fact 1",
            db_path=db, rebuild=False,
        )
        r2 = ac.accumulate(
            scope_id="rel_123_denver_mtb", content="Fact 2",
            db_path=db, rebuild=False,
        )

        assert r1["fragment_id"] != r2["fragment_id"]

    def test_scope_not_found(self, db):
        result = ac.accumulate(
            scope_id="nonexistent",
            content="test",
            db_path=db,
            rebuild=False,
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_terminated_scope_rejected(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.terminate_scope("rel_123_denver_mtb", db)

        result = ac.accumulate(
            scope_id="rel_123_denver_mtb",
            content="Should not work",
            db_path=db,
            rebuild=False,
        )
        assert result["ok"] is False
        assert "terminated" in result["error"]


# ── Rendering in USER.md ─────────────────────────────────────────────────────


class TestAccumulatedRendering:
    def test_accumulated_context_renders_in_user_md(self, db, tmp_path, monkeypatch):
        """Accumulated fragments appear in the guest USER.md Conversation History."""
        env = _make_envelope()
        env["context_layers"]["accumulated"] = [
            {
                "fragment_id": "acc_001",
                "content": "Ugo confirmed he can make April 24-26",
                "source_interaction_id": "msg_1",
                "timestamp": "2026-03-28T21:00:00Z",
                "prunable": True,
            },
            {
                "fragment_id": "acc_002",
                "content": "Ugo asked about e-bike rental options",
                "source_interaction_id": "msg_2",
                "timestamp": "2026-03-27T15:00:00Z",
                "prunable": True,
            },
        ]
        ss.create_scope(env, db)

        # Use env var so both tools.scope_store and scope_store resolve to test DB
        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "### Conversation History" in user_md
        assert "[2026-03-28] Ugo confirmed he can make April 24-26" in user_md
        assert "[2026-03-27] Ugo asked about e-bike rental options" in user_md

    def test_accumulated_context_renders_guest_attribution(self, db, tmp_path, monkeypatch):
        """Accumulated fragments with guest field render with attribution."""
        env = _make_envelope()
        env["context_layers"]["accumulated"] = [
            {
                "fragment_id": "acc_010",
                "guest": "Ugo",
                "content": "Prefers drivable trips over flights",
                "source_interaction_id": "",
                "timestamp": "2026-04-05T17:05:00Z",
                "prunable": True,
            },
            {
                "fragment_id": "acc_011",
                "content": "Unattributed legacy fragment",
                "source_interaction_id": "",
                "timestamp": "2026-04-01T10:00:00Z",
                "prunable": True,
            },
        ]
        ss.create_scope(env, db)
        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "[2026-04-05] Ugo: Prefers drivable trips over flights" in user_md
        # Unattributed fragments render without prefix
        assert "[2026-04-01] Unattributed legacy fragment" in user_md
        assert "None:" not in user_md

    def test_no_accumulated_section_when_empty(self, db, monkeypatch):
        """No Conversation History section when accumulated is empty."""
        env = _make_envelope()
        ss.create_scope(env, db)

        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "### Conversation History" not in user_md


# ── Authorization tier rendering ──────────────────────────────────────────────


class TestAuthorizationRendering:
    def test_task_context_disclosure_rules(self, db, monkeypatch):
        env = _make_envelope()
        env["authorization"]["max_disclosure_tier"] = "task_context"
        ss.create_scope(env, db)
        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "**Disclosure rules**" in user_md
        assert "may share details about this task" in user_md

    def test_identity_only_disclosure_rules(self, db, monkeypatch):
        env = _make_envelope()
        env["authorization"]["max_disclosure_tier"] = "identity_only"
        ss.create_scope(env, db)
        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "Only confirm that you are Homer" in user_md

    def test_broad_context_disclosure_rules(self, db, monkeypatch):
        env = _make_envelope()
        env["authorization"]["max_disclosure_tier"] = "broad_context"
        ss.create_scope(env, db)
        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "broader context relevant to this relationship" in user_md

    def test_authorization_line_in_scope_header(self, db, monkeypatch):
        env = _make_envelope()
        ss.create_scope(env, db)
        monkeypatch.setenv("HOMER_SCOPE_DB", str(db))

        user_md = ss.render_scope_section(ss.get_scope("rel_123_denver_mtb", db))

        assert "Authorization: task_context | capabilities: message" in user_md

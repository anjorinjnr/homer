"""Tests for tools/scope_store.py — SQLite-backed scope store."""

import json
import tempfile
from pathlib import Path

import pytest

import tools.scope_store as ss


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Provide a fresh, isolated scope DB for each test."""
    return tmp_path / "test_scopes.db"


def _make_envelope(
    scope_id: str = "rel_15551234567_mtb_colorado",
    participant_id: str = "15551234567@s.whatsapp.net",
    name: str = "Jake",
    event_id: str = "mtb_colorado",
) -> dict:
    return ss.make_minimal_envelope(
        scope_id=scope_id,
        name=name,
        participant_id=participant_id,
        event_id=event_id,
    )


# ---------------------------------------------------------------------------
# init_db / table creation
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_tables(self, db):
        conn = ss.get_conn(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "scopes" in tables
        assert "scope_participants" in tables
        assert "escalations" in tables

    def test_wal_mode(self, db):
        conn = ss.get_conn(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_idempotent(self, db):
        """Calling get_conn twice should not raise."""
        ss.get_conn(db)
        ss.get_conn(db)


# ---------------------------------------------------------------------------
# create_scope / get_scope
# ---------------------------------------------------------------------------

class TestCreateAndGetScope:
    def test_create_returns_scope_id(self, db):
        env = _make_envelope()
        sid = ss.create_scope(env, db)
        assert sid == "rel_15551234567_mtb_colorado"

    def test_get_returns_envelope(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        result = ss.get_scope("rel_15551234567_mtb_colorado", db)
        assert result is not None
        assert result["scope_id"] == "rel_15551234567_mtb_colorado"
        assert result["participants"][0]["name"] == "Jake"

    def test_get_missing_returns_none(self, db):
        assert ss.get_scope("does_not_exist", db) is None

    def test_duplicate_raises(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        with pytest.raises(Exception):
            ss.create_scope(env, db)

    def test_participant_row_created(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        conn = ss.get_conn(db)
        rows = conn.execute(
            "SELECT participant_id FROM scope_participants WHERE scope_id = ?",
            ("rel_15551234567_mtb_colorado",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["participant_id"] == "15551234567@s.whatsapp.net"


# ---------------------------------------------------------------------------
# update_scope
# ---------------------------------------------------------------------------

class TestUpdateScope:
    def test_update_changes_envelope(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        env["authorization"]["max_disclosure_tier"] = "broad_context"
        ss.update_scope("rel_15551234567_mtb_colorado", env, db)
        result = ss.get_scope("rel_15551234567_mtb_colorado", db)
        assert result["authorization"]["max_disclosure_tier"] == "broad_context"

    def test_update_syncs_participants_add(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        env["participants"].append({
            "party_id": "19995554444@s.whatsapp.net",
            "name": "Wale",
            "handle": "+19995554444",
            "relationship_type": "personal",
            "channel": "whatsapp",
        })
        ss.update_scope("rel_15551234567_mtb_colorado", env, db)
        ids = ss.get_all_active_participant_ids(db)
        assert "19995554444@s.whatsapp.net" in ids

    def test_update_syncs_participants_remove(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.add_participant(
            "rel_15551234567_mtb_colorado", "19995554444@s.whatsapp.net", db
        )
        env["participants"] = []
        ss.update_scope("rel_15551234567_mtb_colorado", env, db)
        ids = ss.get_all_active_participant_ids(db)
        assert "19995554444@s.whatsapp.net" not in ids


# ---------------------------------------------------------------------------
# terminate_scope
# ---------------------------------------------------------------------------

class TestTerminateScope:
    def test_terminated_not_in_active_list(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.terminate_scope("rel_15551234567_mtb_colorado", db)
        scopes = ss.list_active_scopes(db)
        assert all(s["scope_id"] != "rel_15551234567_mtb_colorado" for s in scopes)

    def test_terminated_not_in_participant_lookup(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.terminate_scope("rel_15551234567_mtb_colorado", db)
        result = ss.get_scopes_for_participant("15551234567@s.whatsapp.net", db)
        assert result == []

    def test_get_scope_shows_status(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.terminate_scope("rel_15551234567_mtb_colorado", db)
        result = ss.get_scope("rel_15551234567_mtb_colorado", db)
        assert result["_status"] == "terminated"


# ---------------------------------------------------------------------------
# get_scopes_for_participant
# ---------------------------------------------------------------------------

class TestGetScopesForParticipant:
    def test_returns_scopes_for_participant(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        result = ss.get_scopes_for_participant("15551234567@s.whatsapp.net", db)
        assert len(result) == 1
        assert result[0]["scope_id"] == "rel_15551234567_mtb_colorado"

    def test_multi_scope_actor(self, db):
        """Participant in two scopes → both returned."""
        env1 = _make_envelope("scope_a", "15551234567@s.whatsapp.net", "Jake", "event_a")
        env2 = _make_envelope("scope_b", "15551234567@s.whatsapp.net", "Jake", "event_b")
        ss.create_scope(env1, db)
        ss.create_scope(env2, db)
        result = ss.get_scopes_for_participant("15551234567@s.whatsapp.net", db)
        assert len(result) == 2

    def test_unknown_participant_returns_empty(self, db):
        result = ss.get_scopes_for_participant("unknown@s.whatsapp.net", db)
        assert result == []

    def test_terminated_scope_excluded(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.terminate_scope("rel_15551234567_mtb_colorado", db)
        result = ss.get_scopes_for_participant("15551234567@s.whatsapp.net", db)
        assert result == []


# ---------------------------------------------------------------------------
# get_all_active_participant_ids
# ---------------------------------------------------------------------------

class TestGetAllActiveParticipantIds:
    def test_returns_all_ids(self, db):
        e1 = _make_envelope("s1", "111@s.whatsapp.net", "Alice", "e1")
        e2 = _make_envelope("s2", "222@s.whatsapp.net", "Bob", "e2")
        ss.create_scope(e1, db)
        ss.create_scope(e2, db)
        ids = ss.get_all_active_participant_ids(db)
        assert "111@s.whatsapp.net" in ids
        assert "222@s.whatsapp.net" in ids

    def test_terminated_excluded(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.terminate_scope("rel_15551234567_mtb_colorado", db)
        ids = ss.get_all_active_participant_ids(db)
        assert "15551234567@s.whatsapp.net" not in ids

    def test_empty_db_returns_empty_list(self, db):
        assert ss.get_all_active_participant_ids(db) == []


# ---------------------------------------------------------------------------
# add_participant / remove_participant
# ---------------------------------------------------------------------------

class TestParticipantManagement:
    def test_add_participant(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.add_participant("rel_15551234567_mtb_colorado", "999@s.whatsapp.net", db)
        ids = ss.get_all_active_participant_ids(db)
        assert "999@s.whatsapp.net" in ids

    def test_add_duplicate_participant_is_noop(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.add_participant(
            "rel_15551234567_mtb_colorado", "15551234567@s.whatsapp.net", db
        )
        ids = ss.get_all_active_participant_ids(db)
        assert ids.count("15551234567@s.whatsapp.net") == 1

    def test_remove_participant(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        ss.remove_participant(
            "rel_15551234567_mtb_colorado", "15551234567@s.whatsapp.net", db
        )
        ids = ss.get_all_active_participant_ids(db)
        assert "15551234567@s.whatsapp.net" not in ids


# ---------------------------------------------------------------------------
# find_scope_for_participant_and_event
# ---------------------------------------------------------------------------

class TestFindScopeForParticipantAndEvent:
    def test_finds_correct_scope(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        result = ss.find_scope_for_participant_and_event(
            "15551234567@s.whatsapp.net", "mtb_colorado", db
        )
        assert result == "rel_15551234567_mtb_colorado"

    def test_returns_none_for_wrong_event(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        result = ss.find_scope_for_participant_and_event(
            "15551234567@s.whatsapp.net", "other_event", db
        )
        assert result is None

    def test_returns_none_for_unknown_participant(self, db):
        result = ss.find_scope_for_participant_and_event(
            "unknown@s.whatsapp.net", "mtb_colorado", db
        )
        assert result is None


# ---------------------------------------------------------------------------
# migrate_from_acl
# ---------------------------------------------------------------------------

class TestMigrateFromAcl:
    def _write_acl(self, path: Path, acl: dict) -> None:
        path.write_text(json.dumps(acl), encoding="utf-8")

    def test_migrates_whatsapp_guest(self, db, tmp_path):
        acl_path = tmp_path / "guest_agent_acl.json"
        self._write_acl(acl_path, {
            "15551234567@s.whatsapp.net": {
                "name": "Jake",
                "event_id": "mtb_colorado",
                "channel": "whatsapp",
                "phone": "+15551234567",
                "added": "2026-03-01",
                "expires": "",
            }
        })
        count = ss.migrate_from_acl(acl_path, db)
        assert count == 1
        scope = ss.get_scope("rel_15551234567_mtb_colorado", db)
        assert scope is not None
        assert scope["participants"][0]["name"] == "Jake"

    def test_migrates_multiple_guests(self, db, tmp_path):
        acl_path = tmp_path / "guest_agent_acl.json"
        self._write_acl(acl_path, {
            "111@s.whatsapp.net": {
                "name": "Alice", "event_id": "e1", "channel": "whatsapp",
                "phone": "+1111111111", "added": "2026-01-01", "expires": "",
            },
            "222@s.whatsapp.net": {
                "name": "Bob", "event_id": "e2", "channel": "whatsapp",
                "phone": "+2222222222", "added": "2026-01-01", "expires": "",
            },
        })
        count = ss.migrate_from_acl(acl_path, db)
        assert count == 2
        assert len(ss.list_active_scopes(db)) == 2

    def test_skips_existing_scopes(self, db, tmp_path):
        acl_path = tmp_path / "guest_agent_acl.json"
        self._write_acl(acl_path, {
            "15551234567@s.whatsapp.net": {
                "name": "Jake", "event_id": "mtb_colorado", "channel": "whatsapp",
                "phone": "+15551234567", "added": "2026-01-01", "expires": "",
            }
        })
        ss.migrate_from_acl(acl_path, db)
        count = ss.migrate_from_acl(acl_path, db)
        assert count == 0  # already exists

    def test_missing_acl_file_returns_zero(self, db, tmp_path):
        count = ss.migrate_from_acl(tmp_path / "nonexistent.json", db)
        assert count == 0

    def test_empty_acl_returns_zero(self, db, tmp_path):
        acl_path = tmp_path / "guest_agent_acl.json"
        self._write_acl(acl_path, {})
        count = ss.migrate_from_acl(acl_path, db)
        assert count == 0

    def test_preserves_expiry(self, db, tmp_path):
        acl_path = tmp_path / "guest_agent_acl.json"
        self._write_acl(acl_path, {
            "15551234567@s.whatsapp.net": {
                "name": "Jake", "event_id": "mtb_colorado", "channel": "whatsapp",
                "phone": "+15551234567", "added": "2026-01-01", "expires": "2026-08-01",
            }
        })
        ss.migrate_from_acl(acl_path, db)
        scope = ss.get_scope("rel_15551234567_mtb_colorado", db)
        assert scope["authorization"]["expires_at"] == "2026-08-01"


# ---------------------------------------------------------------------------
# get_scope_summary
# ---------------------------------------------------------------------------

class TestGetScopeSummary:
    def test_empty_returns_none_message(self, db):
        result = ss.get_scope_summary(db)
        assert "none" in result.lower()

    def test_shows_participant_and_task(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        result = ss.get_scope_summary(db)
        assert "Jake" in result
        assert "mtb_colorado" in result.lower() or "Mtb Colorado" in result


# ---------------------------------------------------------------------------
# Email normalization
# ---------------------------------------------------------------------------

class TestNormalizeEmail:
    def test_lowercase(self):
        assert ss.normalize_email("Jake@Example.COM") == "jake@example.com"

    def test_gmail_strips_dots(self):
        assert ss.normalize_email("j.doe@gmail.com") == "jdoe@gmail.com"

    def test_gmail_strips_plus_suffix(self):
        assert ss.normalize_email("jdoe+tag@gmail.com") == "jdoe@gmail.com"

    def test_gmail_strips_dots_and_plus(self):
        assert ss.normalize_email("j.doe+test@gmail.com") == "jdoe@gmail.com"

    def test_googlemail_treated_as_gmail(self):
        assert ss.normalize_email("j.doe@googlemail.com") == "jdoe@googlemail.com"

    def test_non_gmail_preserves_dots(self):
        assert ss.normalize_email("j.doe@company.com") == "j.doe@company.com"

    def test_non_gmail_preserves_plus(self):
        assert ss.normalize_email("user+tag@company.com") == "user+tag@company.com"

    def test_empty_string(self):
        assert ss.normalize_email("") == ""

    def test_whitespace_stripped(self):
        assert ss.normalize_email("  user@example.com  ") == "user@example.com"


# ---------------------------------------------------------------------------
# Email index (scope_email_index table)
# ---------------------------------------------------------------------------

class TestEmailIndex:
    def test_create_scope_with_email_populates_index(self, db):
        env = ss.make_minimal_envelope(
            scope_id="test_scope",
            name="Jake",
            participant_id="15551234567@s.whatsapp.net",
            event_id="mtb_colorado",
            email="jake@example.com",
        )
        ss.create_scope(env, db)
        scopes = ss.get_scopes_for_email("jake@example.com", db)
        assert len(scopes) == 1
        assert scopes[0]["scope_id"] == "test_scope"

    def test_create_scope_without_email_no_index(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        scopes = ss.get_scopes_for_email("nobody@example.com", db)
        assert scopes == []

    def test_email_lookup_case_insensitive(self, db):
        env = ss.make_minimal_envelope(
            scope_id="test_scope",
            name="Jake",
            participant_id="15551234567@s.whatsapp.net",
            event_id="mtb_colorado",
            email="Jake@Example.COM",
        )
        ss.create_scope(env, db)
        scopes = ss.get_scopes_for_email("jake@example.com", db)
        assert len(scopes) == 1

    def test_gmail_normalization_in_lookup(self, db):
        env = ss.make_minimal_envelope(
            scope_id="test_scope",
            name="Jake",
            participant_id="15551234567@s.whatsapp.net",
            event_id="mtb_colorado",
            email="j.doe@gmail.com",
        )
        ss.create_scope(env, db)
        # Lookup with different format should match via normalization
        scopes = ss.get_scopes_for_email("jdoe@gmail.com", db)
        assert len(scopes) == 1

    def test_update_scope_adds_email_to_index(self, db):
        env = _make_envelope()
        ss.create_scope(env, db)
        # Now update to add email
        env["participants"][0]["email"] = "jake@example.com"
        ss.update_scope(env["scope_id"], env, db)
        scopes = ss.get_scopes_for_email("jake@example.com", db)
        assert len(scopes) == 1

    def test_update_scope_removes_email_from_index(self, db):
        env = ss.make_minimal_envelope(
            scope_id="test_scope",
            name="Jake",
            participant_id="15551234567@s.whatsapp.net",
            event_id="mtb_colorado",
            email="jake@example.com",
        )
        ss.create_scope(env, db)
        # Remove email
        del env["participants"][0]["email"]
        ss.update_scope("test_scope", env, db)
        scopes = ss.get_scopes_for_email("jake@example.com", db)
        assert scopes == []

    def test_multi_scope_same_email(self, db):
        for i, event in enumerate(["event_a", "event_b"]):
            env = ss.make_minimal_envelope(
                scope_id=event,
                name="Jake",
                participant_id="15551234567@s.whatsapp.net",
                event_id=event,
                email="jake@example.com",
            )
            ss.create_scope(env, db)
        scopes = ss.get_scopes_for_email("jake@example.com", db)
        assert len(scopes) == 2

    def test_terminated_scope_excluded_from_email_lookup(self, db):
        env = ss.make_minimal_envelope(
            scope_id="test_scope",
            name="Jake",
            participant_id="15551234567@s.whatsapp.net",
            event_id="mtb_colorado",
            email="jake@example.com",
        )
        ss.create_scope(env, db)
        ss.terminate_scope("test_scope", db)
        scopes = ss.get_scopes_for_email("jake@example.com", db)
        assert scopes == []

    def test_get_all_active_email_addresses(self, db):
        env1 = ss.make_minimal_envelope(
            scope_id="s1", name="Jake",
            participant_id="111@s.whatsapp.net", event_id="e1",
            email="jake@example.com",
        )
        env2 = ss.make_minimal_envelope(
            scope_id="s2", name="Bob",
            participant_id="222@s.whatsapp.net", event_id="e2",
            email="bob@example.com",
        )
        ss.create_scope(env1, db)
        ss.create_scope(env2, db)
        emails = ss.get_all_active_email_addresses(db)
        assert set(emails) == {"jake@example.com", "bob@example.com"}

    def test_get_all_active_email_addresses_excludes_terminated(self, db):
        env = ss.make_minimal_envelope(
            scope_id="s1", name="Jake",
            participant_id="111@s.whatsapp.net", event_id="e1",
            email="jake@example.com",
        )
        ss.create_scope(env, db)
        ss.terminate_scope("s1", db)
        emails = ss.get_all_active_email_addresses(db)
        assert emails == []

    def test_email_index_table_created(self, db):
        conn = ss.get_conn(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "scope_email_index" in tables


# ---------------------------------------------------------------------------
# make_interaction_envelope
# ---------------------------------------------------------------------------

class TestMakeInteractionEnvelope:
    def test_scope_type_is_interaction(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
        )
        assert env["scope_type"] == "interaction"

    def test_whatsapp_channel(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
            channel="whatsapp",
        )
        p = env["participants"][0]
        assert p["channel"] == "whatsapp"
        assert p["party_id"] == "15551234567@s.whatsapp.net"

    def test_telegram_channel(self):
        env = ss.make_interaction_envelope(
            scope_id="int_jake", name="Jake",
            participant_id="tg:123456",
            channel="telegram",
        )
        p = env["participants"][0]
        assert p["channel"] == "telegram"
        assert p["party_id"] == "tg:123456"

    def test_email_channel(self):
        env = ss.make_interaction_envelope(
            scope_id="int_acme", name="Acme",
            participant_id="info@acme.com",
            channel="email",
        )
        p = env["participants"][0]
        assert p["channel"] == "email"
        assert p["party_id"] == "info@acme.com"
        assert p["email"] == "info@acme.com"

    def test_email_auto_set_for_email_channel(self):
        """Email channel should set participant.email even if not passed explicitly."""
        env = ss.make_interaction_envelope(
            scope_id="int_test", name="Test",
            participant_id="test@example.com",
            channel="email",
        )
        assert env["participants"][0]["email"] == "test@example.com"

    def test_explicit_email_for_whatsapp(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
            channel="whatsapp",
            email="bob@painters.co",
        )
        assert env["participants"][0]["email"] == "bob@painters.co"

    def test_purpose_in_injected_context(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
            purpose="Quote for exterior painting",
        )
        injected = env["context_layers"]["injected"]
        assert len(injected) == 1
        assert injected[0]["content"] == "Quote for exterior painting"
        assert injected[0]["fragment_id"] == "init_int_bob"

    def test_no_purpose_means_empty_injected(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
        )
        assert env["context_layers"]["injected"] == []

    def test_expires_at(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
            expires="2026-05-16",
        )
        assert env["authorization"]["expires_at"] == "2026-05-16"

    def test_no_expires_means_none(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
        )
        assert env["authorization"]["expires_at"] is None

    def test_task_tags_use_purpose(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
            purpose="Lawn maintenance",
        )
        assert env["task_tags"][0]["task_id"] == "task_int_bob"
        assert env["task_tags"][0]["description"] == "Lawn maintenance"

    def test_relationship_type_is_service(self):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="15551234567@s.whatsapp.net",
        )
        assert env["participants"][0]["relationship_type"] == "service"

    def test_creates_and_retrieves_in_db(self, db):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="bob@example.com",
            channel="email",
            purpose="Test",
            expires="2026-06-01",
        )
        ss.create_scope(env, db)
        stored = ss.get_scope("int_bob", db)
        assert stored["scope_type"] == "interaction"
        assert stored["participants"][0]["email"] == "bob@example.com"

    def test_email_index_populated(self, db):
        env = ss.make_interaction_envelope(
            scope_id="int_bob", name="Bob",
            participant_id="bob@example.com",
            channel="email",
        )
        ss.create_scope(env, db)
        scopes = ss.get_scopes_for_email("bob@example.com", db)
        assert len(scopes) == 1
        assert scopes[0]["scope_id"] == "int_bob"


# ---------------------------------------------------------------------------
# render_scope_section (section-level renderer)
# ---------------------------------------------------------------------------

class TestRenderScopeSection:
    def test_renders_minimum_fields(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_adam_mtb",
            name="Adam",
            participant_id="16072348189@s.whatsapp.net",
            event_id="mtb",
        )
        out = ss.render_scope_section(env)
        assert "## Scope: rel_adam_mtb" in out
        assert "Type: relationship" in out
        assert "Adam (16072348189@s.whatsapp.net)" in out
        assert "Authorization: task_context | capabilities: message" in out
        assert "**Disclosure rules**" in out
        assert "Tasks:" in out
        assert "  - Mtb [active]" in out

    def test_omits_context_when_no_injected(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_adam_mtb", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="mtb",
        )
        out = ss.render_scope_section(env)
        assert "### Context" not in out

    def test_renders_injected_context(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_adam_mtb", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="mtb",
        )
        env["context_layers"]["injected"] = [
            {"fragment_id": "evt_mtb_status", "content": "# MTB Trip\nDates: April 24-26"},
        ]
        out = ss.render_scope_section(env)
        assert "### Context" in out
        assert "# MTB Trip" in out
        assert "Dates: April 24-26" in out

    def test_renders_accumulated_conversation_history(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_adam_mtb", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="mtb",
        )
        env["context_layers"]["accumulated"] = [
            {"timestamp": "2026-04-16T13:37:59Z", "guest": "Adam",
             "content": "Can do Aug 20-23"},
        ]
        out = ss.render_scope_section(env)
        assert "### Conversation History" in out
        assert "[2026-04-16] Adam: Can do Aug 20-23" in out

    def test_pending_follow_ups_optional_default_none(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_adam_mtb", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="mtb",
        )
        out = ss.render_scope_section(env)
        assert "### Pending Follow-ups" not in out

    def test_pending_follow_ups_rendered_when_provided(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_adam_mtb", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="mtb",
        )
        pending = [{
            "id": "abc-123", "from": "adam", "topic": "dates",
            "notify_channel": "whatsapp", "notify_recipient": "1234@s.whatsapp.net",
        }]
        out = ss.render_scope_section(env, pending)
        assert "### Pending Follow-ups" in out
        assert "**adam** re: dates" in out
        assert "id: abc-123" in out
        assert "notify via whatsapp → 1234@s.whatsapp.net" in out

    def test_interaction_scope_type_rendered(self):
        env = ss.make_interaction_envelope(
            scope_id="int_ben", name="Ben", participant_id="ben@example.com",
            channel="email", purpose="Vendor quote",
        )
        out = ss.render_scope_section(env)
        assert "Type: interaction" in out
        assert "### Context" in out
        assert "Vendor quote" in out

    def test_broad_context_tier_renders_broader_rules(self):
        env = ss.make_minimal_envelope(
            scope_id="rel_x", name="X",
            participant_id="x@s.whatsapp.net", event_id="e",
        )
        env["authorization"]["max_disclosure_tier"] = "broad_context"
        out = ss.render_scope_section(env)
        assert "may share broader context" in out


# ---------------------------------------------------------------------------
# render_scope_context_for_sender (per-turn injection entry point)
# ---------------------------------------------------------------------------

class TestRenderScopeContextForSender:
    def _setup_two_scopes(self, db):
        """Create two disjoint scopes: mtb (Adam, Emeka) + bday (Alex, Sam)."""
        mtb = ss.make_minimal_envelope(
            scope_id="rel_mtb", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="mtb",
        )
        mtb["participants"].append({
            "party_id": "14129739891@s.whatsapp.net", "name": "Emeka",
            "handle": "14129739891@s.whatsapp.net",
            "relationship_type": "personal", "channel": "whatsapp",
        })
        mtb["context_layers"]["injected"] = [
            {"fragment_id": "evt_mtb_status",
             "content": "# MTB Trip\nDates: April 24-26"},
        ]
        ss.create_scope(mtb, db)

        bday = ss.make_minimal_envelope(
            scope_id="rel_bday", name="Alex",
            participant_id="4126920720@s.whatsapp.net", event_id="bday",
        )
        bday["participants"].append({
            "party_id": "sam@example.com", "name": "Sam",
            "handle": "sam@example.com", "email": "sam@example.com",
            "relationship_type": "personal", "channel": "email",
        })
        bday["context_layers"]["injected"] = [
            {"fragment_id": "evt_bday_status",
             "content": "# Birthday\nDates: May 2"},
        ]
        ss.create_scope(bday, db)
        # Sync email index
        ss.update_scope("rel_bday", ss.get_scope("rel_bday", db), db)

    def test_empty_sender_returns_empty(self, db):
        assert ss.render_scope_context_for_sender("", db) == ""

    def test_unknown_sender_returns_empty(self, db):
        self._setup_two_scopes(db)
        assert ss.render_scope_context_for_sender("99999@s.whatsapp.net", db) == ""

    def test_single_scope_for_whatsapp_sender(self, db):
        self._setup_two_scopes(db)
        out = ss.render_scope_context_for_sender("16072348189@s.whatsapp.net", db)
        assert "# Scope Context" in out
        assert "## Scope: rel_mtb" in out
        assert "## Scope: rel_bday" not in out
        # Injected content present
        assert "Dates: April 24-26" in out
        # Cross-scope content absent
        assert "Dates: May 2" not in out

    def test_scope_isolation_sender_b_does_not_see_sender_a_scope(self, db):
        self._setup_two_scopes(db)
        out = ss.render_scope_context_for_sender("4126920720@s.whatsapp.net", db)
        assert "## Scope: rel_bday" in out
        assert "## Scope: rel_mtb" not in out
        assert "April 24-26" not in out  # no leakage
        assert "Adam" not in out         # other scope participant invisible

    def test_email_sender_routes_to_scope(self, db):
        self._setup_two_scopes(db)
        out = ss.render_scope_context_for_sender("sam@example.com", db)
        assert "## Scope: rel_bday" in out
        assert "rel_mtb" not in out

    def test_multi_scope_participant_gets_both(self, db):
        """A participant in two scopes sees both — still isolated from others."""
        self._setup_two_scopes(db)
        # Add Emeka to the bday scope too (he's already in mtb)
        bday = ss.get_scope("rel_bday", db)
        bday["participants"].append({
            "party_id": "14129739891@s.whatsapp.net", "name": "Emeka",
            "handle": "14129739891@s.whatsapp.net",
            "relationship_type": "personal", "channel": "whatsapp",
        })
        ss.update_scope("rel_bday", bday, db)

        out = ss.render_scope_context_for_sender("14129739891@s.whatsapp.net", db)
        assert "## Scope: rel_mtb" in out
        assert "## Scope: rel_bday" in out

    def test_deduplicates_if_participant_and_email_match_same_scope(self, db):
        """Ensure participant-ID and email lookups don't double-render a scope."""
        env = ss.make_interaction_envelope(
            scope_id="int_vendor", name="Vendor",
            participant_id="vendor@example.com", channel="email",
        )
        ss.create_scope(env, db)
        out = ss.render_scope_context_for_sender("vendor@example.com", db)
        assert out.count("## Scope: int_vendor") == 1

    def test_pending_follow_ups_matched_by_participant_name(self, db, tmp_path):
        self._setup_two_scopes(db)
        pending_file = tmp_path / "pending_replies.json"
        pending_file.write_text(json.dumps([
            {"id": "p1", "from": "adam", "topic": "dates",
             "notify_channel": "whatsapp", "notify_recipient": "xxx"},
            {"id": "p2", "from": "sam", "topic": "venue",
             "notify_channel": "whatsapp", "notify_recipient": "yyy"},
        ]))
        out = ss.render_scope_context_for_sender(
            "16072348189@s.whatsapp.net", db, pending_replies_path=pending_file,
        )
        # Adam's pending entry attached to mtb scope
        assert "**adam** re: dates" in out
        # Sam's pending entry belongs to bday scope — must not appear
        assert "sam" not in out.lower() or "**sam**" not in out

    def test_no_pending_follow_ups_section_when_none_match(self, db, tmp_path):
        self._setup_two_scopes(db)
        pending_file = tmp_path / "pending_replies.json"
        pending_file.write_text(json.dumps([
            {"id": "p1", "from": "nobody", "topic": "x",
             "notify_channel": "whatsapp", "notify_recipient": "xxx"},
        ]))
        out = ss.render_scope_context_for_sender(
            "16072348189@s.whatsapp.net", db, pending_replies_path=pending_file,
        )
        assert "### Pending Follow-ups" not in out

    def test_missing_pending_replies_file_is_fine(self, db, tmp_path):
        self._setup_two_scopes(db)
        out = ss.render_scope_context_for_sender(
            "16072348189@s.whatsapp.net", db,
            pending_replies_path=tmp_path / "does_not_exist.json",
        )
        assert "## Scope: rel_mtb" in out
        assert "### Pending Follow-ups" not in out

    def test_pending_follow_up_party_id_resolves_collision(self, db, tmp_path):
        """When a pending entry carries party_id, it only renders into the scope
        whose participant has that exact party_id — even if another scope has a
        participant with the same display name.
        """
        sA = ss.make_minimal_envelope(
            scope_id="rel_A", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="A",
        )
        sA["participants"].append({
            "party_id": "18005551111@s.whatsapp.net", "name": "Alex",
            "handle": "18005551111@s.whatsapp.net",
            "relationship_type": "personal", "channel": "whatsapp",
        })
        ss.create_scope(sA, db)

        sB = ss.make_minimal_envelope(
            scope_id="rel_B", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="B",
        )
        sB["participants"].append({
            "party_id": "18005552222@s.whatsapp.net", "name": "Alex",
            "handle": "18005552222@s.whatsapp.net",
            "relationship_type": "personal", "channel": "whatsapp",
        })
        ss.create_scope(sB, db)

        pending_file = tmp_path / "pending.json"
        pending_file.write_text(json.dumps([{
            "id": "p1", "from": "alex",
            "party_id": "18005551111@s.whatsapp.net",  # only in scope A
            "topic": "logistics",
            "notify_channel": "whatsapp", "notify_recipient": "xxx",
        }]))

        out = ss.render_scope_context_for_sender(
            "16072348189", db, pending_replies_path=pending_file,
        )
        # Scope A renders the pending (party_id matches a participant there);
        # scope B does NOT, even though it has a participant named "Alex".
        assert out.count("**alex** re: logistics") == 1
        # Confirm which scope owns it — find "rel_A" before the pending text
        a_section = out[out.index("## Scope: rel_A"):out.index("## Scope: rel_B")]
        assert "**alex** re: logistics" in a_section

    def test_pending_follow_up_name_collision_shows_in_both_matching_scopes(
        self, db, tmp_path,
    ):
        """Documented semantic: when a sender belongs to multiple scopes that
        each contain a participant whose name matches a pending entry's ``from``,
        the entry is rendered into every matching scope. pending_replies.json
        currently stores only a free-text name — no party_id — so disambiguation
        is not possible at render time. A follow-up can tighten this by adding
        party_id to pending entries; until then, this test locks down the
        inherited (pre-per-scope-filter) behavior.
        """
        # Both scopes contain a participant named "Alex" — shared sender Adam
        # is in both so will see both scopes.
        sA = ss.make_minimal_envelope(
            scope_id="rel_A", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="A",
        )
        sA["participants"].append({
            "party_id": "18005551111@s.whatsapp.net", "name": "Alex",
            "handle": "18005551111@s.whatsapp.net",
            "relationship_type": "personal", "channel": "whatsapp",
        })
        ss.create_scope(sA, db)

        sB = ss.make_minimal_envelope(
            scope_id="rel_B", name="Adam",
            participant_id="16072348189@s.whatsapp.net", event_id="B",
        )
        sB["participants"].append({
            "party_id": "18005552222@s.whatsapp.net", "name": "Alex",
            "handle": "18005552222@s.whatsapp.net",
            "relationship_type": "personal", "channel": "whatsapp",
        })
        ss.create_scope(sB, db)

        pending_file = tmp_path / "pending.json"
        pending_file.write_text(json.dumps([
            {"id": "p1", "from": "alex", "topic": "logistics",
             "notify_channel": "whatsapp", "notify_recipient": "xxx"},
        ]))

        out = ss.render_scope_context_for_sender(
            "16072348189", db, pending_replies_path=pending_file,
        )
        # Both scopes visible (Adam participates in both)
        assert "## Scope: rel_A" in out
        assert "## Scope: rel_B" in out
        # Pending entry appears in BOTH scope sections — documented collision
        assert out.count("**alex** re: logistics") == 2

    def test_whatsapp_phone_digits_resolve_to_full_jid_scope(self, db):
        """Inbound sender_id is phone-digits only (WhatsApp bridge), but scope
        participants are stored as full JIDs. The renderer must bridge."""
        self._setup_two_scopes(db)
        out = ss.render_scope_context_for_sender("16072348189", db)
        assert "## Scope: rel_mtb" in out
        assert "## Scope: rel_bday" not in out

    def test_telegram_id_variant_matches_tg_prefixed_participant(self, db):
        env = ss.make_minimal_envelope(
            scope_id="rel_tg",
            name="TGUser",
            participant_id="tg:987654321",
            event_id="tg_event",
        )
        ss.create_scope(env, db)
        out = ss.render_scope_context_for_sender("987654321", db)
        assert "## Scope: rel_tg" in out


class TestSenderIdVariants:
    def test_digits_only_expands_to_whatsapp_lid_telegram(self):
        assert ss._sender_id_variants("14129739891") == [
            "14129739891",
            "14129739891@s.whatsapp.net",
            "14129739891@lid",
            "tg:14129739891",
        ]

    def test_full_jid_is_single_variant(self):
        assert ss._sender_id_variants("14129739891@s.whatsapp.net") == [
            "14129739891@s.whatsapp.net",
        ]

    def test_tg_prefixed_is_single_variant(self):
        assert ss._sender_id_variants("tg:987654321") == ["tg:987654321"]

    def test_email_not_expanded_into_phone_variants(self):
        assert ss._sender_id_variants("user@example.com") == ["user@example.com"]

    def test_empty_returns_empty(self):
        assert ss._sender_id_variants("") == []

    def test_lid_suffix_resolves_via_lid_map(self, tmp_path, monkeypatch):
        """Adam regression: inbound LID wasn't matching his phone-form party_id."""
        monkeypatch.setenv("NANOBOT_PERSISTENT_DATA_DIR", str(tmp_path))
        (tmp_path / "lid_map.json").write_text(
            '{"38457841848414": {"phone": "16072348189"}}'
        )
        variants = ss._sender_id_variants("38457841848414@lid")
        assert "16072348189@s.whatsapp.net" in variants

    def test_full_lid_jid_suffix_resolves_via_lid_map(self, tmp_path, monkeypatch):
        """2026-05-07 regression: outbound to a `<lid>@lid.whatsapp.net` JID
        wasn't being recognized as a LID, so the lookup never reached the
        underlying phone scope. Surfaced when scope_guard refused 5 outbound
        replies to a Denver MTB guest who messaged in via their LID."""
        monkeypatch.setenv("NANOBOT_PERSISTENT_DATA_DIR", str(tmp_path))
        (tmp_path / "lid_map.json").write_text(
            '{"38457841848414": {"phone": "16072348189"}}'
        )
        variants = ss._sender_id_variants("38457841848414@lid.whatsapp.net")
        assert "16072348189@s.whatsapp.net" in variants
        assert "16072348189" in variants
        assert "38457841848414" in variants

    def test_bare_digits_consults_lid_map(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NANOBOT_PERSISTENT_DATA_DIR", str(tmp_path))
        (tmp_path / "lid_map.json").write_text(
            '{"38457841848414": {"phone": "16072348189"}}'
        )
        variants = ss._sender_id_variants("38457841848414")
        assert "16072348189@s.whatsapp.net" in variants
        assert "16072348189" in variants

    def test_lid_map_missing_is_safe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NANOBOT_PERSISTENT_DATA_DIR", str(tmp_path))
        # No lid_map.json at all — must not raise, returns usual variants only.
        variants = ss._sender_id_variants("38457841848414")
        assert "38457841848414@s.whatsapp.net" in variants

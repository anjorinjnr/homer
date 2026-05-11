"""Tests for context_inject.py — scope context injection from living documents."""

import json
import sqlite3
from pathlib import Path

import pytest

import tools.context_inject as ci


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_STATUS = """\
# Denver MTB Trip
Status: Confirmed
Dates: 2026-07-15 to 2026-07-20
Created: 2026-03-20

## Guests
| Name | Phone | JID | Status | Added |
|------|-------|-----|--------|-------|
| Ugo | +14125550001 | 14125550001@s.whatsapp.net | Pending | 2026-03-26 |
| Emeka | +16072348189 | 16072348189@s.whatsapp.net | Pending | 2026-03-28 |

## Open Items
- [ ] Rent bikes
- [x] Book Airbnb
- [ ] Buy trail passes (@alex)
- [ ] Confirm shuttle

## Confirmed Details
- **Location**: Crested Butte, CO
- **Lodging**: Airbnb confirmed, 4BR house

## Notes
- 2026-03-15 14:30: Trail conditions look good for July
- 2026-03-20 10:00: Ugo confirmed dates work

## Budget
Sheet: https://docs.google.com/spreadsheets/d/abc123
Sheet-ID: abc123

## Activity Log
| Date | What |
|------|------|
| 2026-03-20 | Created event |
| 2026-03-26 | Added guest: Ugo |
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Set up isolated environment with event files and scope DB."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    edir = events_dir / "denver_mtb"
    edir.mkdir()
    (edir / "status.md").write_text(SAMPLE_STATUS)

    monkeypatch.setattr(ci, "EVENTS_DIR", events_dir)

    # Create scope DB with one event scope
    db_path = tmp_path / "scopes.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE scopes (
        scope_id TEXT PRIMARY KEY, envelope TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', last_active TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )""")
    conn.execute("""CREATE TABLE scope_participants (
        participant_id TEXT NOT NULL, scope_id TEXT NOT NULL,
        PRIMARY KEY (participant_id, scope_id)
    )""")
    conn.execute("""CREATE TABLE escalations (
        escalation_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL,
        trigger_type TEXT NOT NULL, triggering_message TEXT,
        guest_assessment TEXT, urgency TEXT DEFAULT 'async',
        status TEXT DEFAULT 'pending', resolution TEXT,
        outbound_sent INTEGER DEFAULT 0, outbound_sent_at TEXT,
        created_at TEXT, resolved_at TEXT
    )""")
    conn.commit()
    conn.close()

    return tmp_path, db_path


def _insert_scope(db_path, scope_id, envelope):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO scopes (scope_id, envelope) VALUES (?, ?)",
        (scope_id, json.dumps(envelope)),
    )
    for p in envelope.get("participants", []):
        conn.execute(
            "INSERT INTO scope_participants (participant_id, scope_id) VALUES (?, ?)",
            (p["party_id"], scope_id),
        )
    conn.commit()
    conn.close()


def _make_event_envelope(scope_id="rel_123_denver_mtb", event_id="denver_mtb"):
    return {
        "scope_id": scope_id,
        "scope_type": "relationship",
        "principal": "alex",
        "guest_identity": "I am Homer",
        "context_source": {"type": "event", "ref": event_id},
        "participants": [{"party_id": "123@s.whatsapp.net", "name": "Test", "handle": "123", "relationship_type": "personal", "channel": "whatsapp"}],
        "authorization": {"granted_capabilities": ["message"], "max_disclosure_tier": "task_context"},
        "context_layers": {"injected": [], "accumulated": []},
        "task_tags": [{"task_id": f"task_{event_id}", "description": event_id.replace("_", " ").title(), "status": "active"}],
        "lifecycle": {"last_active": None, "pruning_policy": "retain_all", "review_trigger": "30d"},
        "escalation_log": [],
    }


def _make_static_envelope(scope_id="rel_456"):
    return {
        "scope_id": scope_id,
        "scope_type": "relationship",
        "principal": "alex",
        "guest_identity": "I am Homer",
        "participants": [{"party_id": "456@s.whatsapp.net", "name": "Vendor", "handle": "456", "relationship_type": "personal", "channel": "whatsapp"}],
        "authorization": {"granted_capabilities": ["message"], "max_disclosure_tier": "task_context"},
        "context_layers": {"injected": [{"fragment_id": "init_rel_456", "content": "Remind about invoice #1234"}], "accumulated": []},
        "task_tags": [{"task_id": "task_vendor", "description": "Vendor Reminder", "status": "active"}],
        "lifecycle": {"last_active": None, "pruning_policy": "retain_all", "review_trigger": "30d"},
        "escalation_log": [],
    }


# ── Scrubbing ────────────────────────────────────────────────────────────────

class TestScrubEventStatus:
    def test_keeps_title(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "# Denver MTB Trip" in result

    def test_keeps_status_and_dates(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "Status: Confirmed" in result
        assert "Dates: 2026-07-15 to 2026-07-20" in result

    def test_keeps_confirmed_details(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "**Location**: Crested Butte, CO" in result
        assert "**Lodging**: Airbnb confirmed" in result

    def test_keeps_notes(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "Trail conditions look good for July" in result

    def test_keeps_open_items(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "Rent bikes" in result
        assert "Book Airbnb" in result

    def test_keeps_guests_table(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "## Guests" in result
        assert "Ugo" in result

    def test_keeps_budget(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "## Budget" in result

    def test_strips_activity_log(self):
        result = ci._scrub_event_status(SAMPLE_STATUS)
        assert "Activity Log" not in result
        assert "Created event" not in result


# ── Event provider ───────────────────────────────────────────────────────────

class TestEventProvider:
    def test_returns_fragment(self, env):
        tmp_path, db_path = env
        envelope = _make_event_envelope()
        fragments = ci._provide_event_context(envelope)
        assert len(fragments) == 1
        assert fragments[0]["fragment_id"] == "evt_denver_mtb_status"
        assert "Crested Butte" in fragments[0]["content"]

    def test_missing_event_returns_empty(self, env):
        envelope = _make_event_envelope(event_id="nonexistent")
        fragments = ci._provide_event_context(envelope)
        assert fragments == []

    def test_fallback_to_task_tags(self, env):
        tmp_path, db_path = env
        envelope = _make_event_envelope()
        del envelope["context_source"]
        fragments = ci._provide_event_context(envelope)
        assert len(fragments) == 1
        assert "Denver MTB Trip" in fragments[0]["content"]


# ── inject_all ───────────────────────────────────────────────────────────────

class TestInjectAll:
    def test_updates_event_scope(self, env):
        tmp_path, db_path = env
        envelope = _make_event_envelope()
        _insert_scope(db_path, envelope["scope_id"], envelope)

        updated = ci.inject_all(db_path)
        assert updated == 1

        # Verify the scope was updated in DB
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT envelope FROM scopes WHERE scope_id = ?",
                           (envelope["scope_id"],)).fetchone()
        stored = json.loads(row["envelope"])
        injected = stored["context_layers"]["injected"]
        assert len(injected) == 1
        assert "Crested Butte" in injected[0]["content"]

    def test_skips_static_scope(self, env):
        tmp_path, db_path = env
        static = _make_static_envelope()
        _insert_scope(db_path, static["scope_id"], static)

        updated = ci.inject_all(db_path)
        assert updated == 0

        # Static content preserved
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT envelope FROM scopes WHERE scope_id = ?",
                           (static["scope_id"],)).fetchone()
        stored = json.loads(row["envelope"])
        assert stored["context_layers"]["injected"][0]["content"] == "Remind about invoice #1234"

    def test_idempotent(self, env):
        tmp_path, db_path = env
        envelope = _make_event_envelope()
        _insert_scope(db_path, envelope["scope_id"], envelope)

        ci.inject_all(db_path)
        updated = ci.inject_all(db_path)
        assert updated == 0  # no change on second run

    def test_mixed_scopes(self, env):
        tmp_path, db_path = env
        event_env = _make_event_envelope()
        static_env = _make_static_envelope()
        _insert_scope(db_path, event_env["scope_id"], event_env)
        _insert_scope(db_path, static_env["scope_id"], static_env)

        updated = ci.inject_all(db_path)
        assert updated == 1  # only event scope updated

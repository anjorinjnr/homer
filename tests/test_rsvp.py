"""Tests for RSVP feature — event_store extensions, rsvp_invite tool, and RSVP router."""

import json
from pathlib import Path

import pytest

import tools.event_store as event_store
import tools.rsvp_invite as rsvp_invite


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated events.db for testing."""
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("HOMER_EVENTS_DB", str(db_path))
    # Clear schema cache so each test gets a fresh init
    event_store._schema_initialized.discard(str(db_path))
    return db_path


@pytest.fixture()
def event_with_guests(db):
    """Create a test event with 3 guests."""
    eid = "camping_trip"
    event_store.add_guest(eid, "15551111111@s.whatsapp.net", "Alice", phone="+15551111111", channel="whatsapp", db_path=db)
    event_store.add_guest(eid, "15552222222@s.whatsapp.net", "Bob", phone="+15552222222", channel="whatsapp", db_path=db)
    event_store.add_guest(eid, "tg:333333", "Carol", channel="telegram", db_path=db)
    return eid


HID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _stub_household_id(monkeypatch):
    """Every rsvp_invite call needs HOMER_HOUSEHOLD_ID (URLs are tenant-scoped)."""
    monkeypatch.setenv("HOMER_HOUSEHOLD_ID", HID)


@pytest.fixture(autouse=True)
def _stub_short_links(monkeypatch):
    """Stub the Supabase-backed shortener so RSVP tests stay hermetic.

    Returns a deterministic in-memory map: same target_url + household_id
    yields the same code, mirroring the real `shorten`'s idempotency.

    Patches via `rsvp_invite.short_links` because rsvp_invite imports the
    module via the bare `import short_links` form (with tools/ on sys.path),
    which gives a different module object than `tools.short_links`.
    """
    cache: dict[tuple[str, str], str] = {}

    def fake_shorten_or_none(target_url, *, household_id, kind=None):
        key = (household_id, target_url)
        if key not in cache:
            cache[key] = f"code{len(cache):04d}"
        return f"https://example.com/s/{cache[key]}"

    monkeypatch.setattr(rsvp_invite.short_links, "shorten_or_none", fake_shorten_or_none)
    return cache


@pytest.fixture()
def event_dir(tmp_path):
    """Create a test event directory with status.md."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    edir = events_dir / "camping_trip"
    edir.mkdir()
    (edir / "status.md").write_text("""\
# Weekend Camping Trip
Status: Coordinating
Dates: May 22-24, 2026
Created: 2026-04-15

## Guests (3)

## Open Items

## Confirmed Details
- **Location**: Red Top Mountain

## Notes

## Budget

## Activity Log
| Date | What |
|------|------|
""")
    return events_dir


# ── event_store schema tests ─────────────────────────────────────────────────

class TestEventStoreSchema:
    """Test new columns and tables in event_store."""

    def test_event_meta_table_created(self, db):
        """event_meta table exists after get_conn."""
        conn = event_store.get_conn(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "event_meta" in tables
        conn.close()

    def test_rsvp_token_column_exists(self, db):
        """event_guests has rsvp_token column."""
        conn = event_store.get_conn(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(event_guests)").fetchall()}
        assert "rsvp_token" in cols
        assert "rsvp_fields_response" in cols
        conn.close()

    def test_migration_adds_columns_to_old_db(self, db):
        """Migration adds new columns to a DB created without them."""
        # Create a DB with the old schema (no rsvp_token, no rsvp_fields_response)
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE event_guests (
                event_id TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT,
                channel TEXT NOT NULL,
                added_at TEXT NOT NULL,
                rsvp_status TEXT NOT NULL DEFAULT 'enrolled',
                headcount INTEGER,
                responded_at TEXT,
                rsvp_note TEXT,
                invited_at TEXT,
                PRIMARY KEY (event_id, participant_id)
            )
        """)
        conn.commit()
        conn.close()

        # Clear schema cache so get_conn re-runs create+migrate
        event_store._schema_initialized.discard(str(db))
        conn = event_store.get_conn(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(event_guests)").fetchall()}
        assert "rsvp_token" in cols
        assert "rsvp_fields_response" in cols
        conn.close()


# ── Token tests ───────────────────────────────────────────────────────────────

class TestRsvpTokens:
    """Test RSVP token generation and lookup."""

    def test_generate_token(self, db, event_with_guests):
        token = event_store.generate_rsvp_token(event_with_guests, "15551111111@s.whatsapp.net", db)
        assert token
        assert len(token) > 20

    def test_generate_token_idempotent(self, db, event_with_guests):
        t1 = event_store.generate_rsvp_token(event_with_guests, "15551111111@s.whatsapp.net", db)
        t2 = event_store.generate_rsvp_token(event_with_guests, "15551111111@s.whatsapp.net", db)
        assert t1 == t2

    def test_generate_token_unique_per_guest(self, db, event_with_guests):
        t1 = event_store.generate_rsvp_token(event_with_guests, "15551111111@s.whatsapp.net", db)
        t2 = event_store.generate_rsvp_token(event_with_guests, "15552222222@s.whatsapp.net", db)
        assert t1 != t2

    def test_generate_token_invalid_guest(self, db, event_with_guests):
        with pytest.raises(ValueError, match="not found"):
            event_store.generate_rsvp_token(event_with_guests, "nonexistent@s.whatsapp.net", db)

    def test_get_guest_by_token(self, db, event_with_guests):
        token = event_store.generate_rsvp_token(event_with_guests, "15551111111@s.whatsapp.net", db)
        guest = event_store.get_guest_by_token(event_with_guests, token, db)
        assert guest is not None
        assert guest["name"] == "Alice"
        assert guest["rsvp_token"] == token

    def test_get_guest_by_invalid_token(self, db, event_with_guests):
        guest = event_store.get_guest_by_token(event_with_guests, "bogus-token", db)
        assert guest is None


# ── RSVP with fields tests ───────────────────────────────────────────────────

class TestRsvpWithFields:
    """Test update_rsvp with custom field responses."""

    def test_update_with_fields(self, db, event_with_guests):
        fields = {"dietary": "vegetarian", "camping_gear": True}
        ok = event_store.update_rsvp(
            event_with_guests, "15551111111@s.whatsapp.net",
            status="confirmed", headcount=2, note="Excited!", fields_response=fields, db_path=db,
        )
        assert ok
        guest = event_store.get_guest(event_with_guests, "15551111111@s.whatsapp.net", db)
        assert guest["rsvp_status"] == "confirmed"
        assert guest["headcount"] == 2
        assert guest["rsvp_note"] == "Excited!"
        assert json.loads(guest["rsvp_fields_response"]) == fields

    def test_update_without_fields(self, db, event_with_guests):
        ok = event_store.update_rsvp(
            event_with_guests, "15551111111@s.whatsapp.net",
            status="declined", note="Too far", db_path=db,
        )
        assert ok
        guest = event_store.get_guest(event_with_guests, "15551111111@s.whatsapp.net", db)
        assert guest["rsvp_status"] == "declined"
        assert guest["rsvp_fields_response"] is None

    def test_update_nonexistent_guest(self, db, event_with_guests):
        ok = event_store.update_rsvp(
            event_with_guests, "nobody@s.whatsapp.net",
            status="confirmed", db_path=db,
        )
        assert not ok


# ── Event meta tests ──────────────────────────────────────────────────────────

class TestEventMeta:
    """Test event_meta CRUD."""

    def test_set_and_get_meta(self, db):
        fields = [
            {"id": "dietary", "type": "select", "label": "Dietary restrictions", "options": ["None", "Vegetarian"]},
            {"id": "gear", "type": "checkbox", "label": "Need camping gear?"},
        ]
        event_store.set_event_meta("camping_trip", rsvp_fields=fields, rsvp_deadline="2026-05-15", db_path=db)
        meta = event_store.get_event_meta("camping_trip", db)
        assert meta is not None
        assert meta["rsvp_fields"] == fields
        assert meta["rsvp_deadline"] == "2026-05-15"

    def test_upsert_meta(self, db):
        event_store.set_event_meta("camping_trip", rsvp_deadline="2026-05-15", db_path=db)
        event_store.set_event_meta("camping_trip", event_description="A fun trip!", db_path=db)
        meta = event_store.get_event_meta("camping_trip", db)
        assert meta["rsvp_deadline"] == "2026-05-15"
        assert meta["event_description"] == "A fun trip!"

    def test_get_meta_nonexistent(self, db):
        meta = event_store.get_event_meta("nonexistent", db)
        assert meta is None


# ── rsvp_invite tool tests ────────────────────────────────────────────────────

class TestRsvpInvite:
    """Test rsvp_invite.py tool functions."""

    def test_generate_invite_single(self, db, event_with_guests):
        result = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://portal.example.com", HID)
        assert "error" not in result
        assert result["guest"] == "Alice"
        assert result["url"].startswith(f"https://portal.example.com/rsvp/{HID}/camping_trip/")
        assert result["token"]

    def test_generate_invite_marks_invited(self, db, event_with_guests):
        rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        guest = event_store.get_guest(event_with_guests, "15551111111@s.whatsapp.net", db)
        assert guest["rsvp_status"] == "invited"

    def test_generate_invite_case_insensitive(self, db, event_with_guests):
        result = rsvp_invite.generate_invite(event_with_guests, "alice", "https://example.com", HID)
        assert "error" not in result
        assert result["guest"] == "Alice"

    def test_generate_invite_unknown_guest(self, db, event_with_guests):
        result = rsvp_invite.generate_invite(event_with_guests, "Nobody", "https://example.com", HID)
        assert "error" in result

    def test_generate_all_invites(self, db, event_with_guests):
        results = rsvp_invite.generate_all_invites(event_with_guests, "https://example.com", HID)
        assert len(results) == 3
        names = {r["guest"] for r in results}
        assert names == {"Alice", "Bob", "Carol"}
        urls = {r["url"] for r in results}
        assert len(urls) == 3  # unique URLs
        # Every URL embeds the household id so the portal can route it.
        for url in urls:
            assert f"/rsvp/{HID}/camping_trip/" in url

    def test_generate_invite_idempotent_token(self, db, event_with_guests):
        r1 = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        r2 = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        assert r1["token"] == r2["token"]

    def test_generate_invite_includes_short_url(self, db, event_with_guests):
        result = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        assert "short_url" in result
        assert result["short_url"].startswith("https://example.com/s/")

    def test_generate_invite_short_url_idempotent(self, db, event_with_guests):
        r1 = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        r2 = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        assert r1["short_url"] == r2["short_url"]

    def test_generate_invite_degrades_when_shortener_fails(
        self, db, event_with_guests, monkeypatch
    ):
        """Supabase outage must not block invite generation; long URL still works."""
        monkeypatch.setattr(
            rsvp_invite.short_links, "shorten_or_none", lambda *a, **k: None
        )
        result = rsvp_invite.generate_invite(event_with_guests, "Alice", "https://example.com", HID)
        assert "error" not in result
        assert result["url"]
        assert "short_url" not in result

    def test_main_refuses_without_household_id(self, db, event_with_guests, monkeypatch, capsys):
        """Missing HOMER_HOUSEHOLD_ID must fail loudly at CLI invocation —
        silent blank URLs on the portal are much harder to diagnose."""
        monkeypatch.delenv("HOMER_HOUSEHOLD_ID", raising=False)
        monkeypatch.setattr(
            "sys.argv",
            ["rsvp_invite.py", "--event-id", event_with_guests, "--guest", "Alice"],
        )
        with pytest.raises(SystemExit) as exc:
            rsvp_invite.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "HOMER_HOUSEHOLD_ID" in out


# ── Public RSVP tests ─────────────────────────────────────────────────────────

class TestPublicToken:
    """Test public token generation and lookup."""

    def test_generate_public_token(self, db):
        token = event_store.generate_public_token("camping_trip", db)
        assert token
        assert len(token) > 10

    def test_generate_public_token_idempotent(self, db):
        t1 = event_store.generate_public_token("camping_trip", db)
        t2 = event_store.generate_public_token("camping_trip", db)
        assert t1 == t2

    def test_get_event_by_public_token(self, db):
        token = event_store.generate_public_token("camping_trip", db)
        meta = event_store.get_event_by_public_token("camping_trip", token, db)
        assert meta is not None

    def test_get_event_by_invalid_public_token(self, db):
        event_store.generate_public_token("camping_trip", db)
        meta = event_store.get_event_by_public_token("camping_trip", "bogus", db)
        assert meta is None


class TestAddWebGuest:
    """Test self-service web guest creation."""

    def test_add_web_guest(self, db):
        guest = event_store.add_web_guest("camping_trip", "Jake Smith", db)
        assert guest["name"] == "Jake Smith"
        assert guest["participant_id"] == "web:jake_smith"
        assert guest["channel"] == "web"

    def test_add_web_guest_idempotent(self, db):
        g1 = event_store.add_web_guest("camping_trip", "Jake Smith", db)
        g2 = event_store.add_web_guest("camping_trip", "Jake Smith", db)
        assert g1["participant_id"] == g2["participant_id"]

    def test_add_web_guest_different_names(self, db):
        g1 = event_store.add_web_guest("camping_trip", "Jake", db)
        g2 = event_store.add_web_guest("camping_trip", "Mike", db)
        assert g1["participant_id"] != g2["participant_id"]


class TestPublicInvite:
    """Test rsvp_invite.py --public functionality."""

    def test_generate_public_link(self, db):
        result = rsvp_invite.generate_public_link("camping_trip", "https://example.com", HID)
        assert result["event_id"] == "camping_trip"
        assert result["url"].startswith(
            f"https://example.com/rsvp/{HID}/camping_trip/open/"
        )
        assert result["public_token"]

    def test_generate_public_link_idempotent(self, db):
        r1 = rsvp_invite.generate_public_link("camping_trip", "https://example.com", HID)
        r2 = rsvp_invite.generate_public_link("camping_trip", "https://example.com", HID)
        assert r1["public_token"] == r2["public_token"]

    def test_generate_public_link_includes_short_url(self, db):
        result = rsvp_invite.generate_public_link("camping_trip", "https://example.com", HID)
        assert "short_url" in result
        assert result["short_url"].startswith("https://example.com/s/")

    def test_generate_public_link_short_url_idempotent(self, db):
        r1 = rsvp_invite.generate_public_link("camping_trip", "https://example.com", HID)
        r2 = rsvp_invite.generate_public_link("camping_trip", "https://example.com", HID)
        assert r1["short_url"] == r2["short_url"]



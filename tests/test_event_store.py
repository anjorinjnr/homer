"""Tests for event_store.py — SQLite-backed guest roster and RSVP tracking."""

import json
import sqlite3
from pathlib import Path

import pytest

import tools.event_store as es


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point event_store at a temp DB for every test."""
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("HOMER_EVENTS_DB", str(db_path))
    return db_path


# ── Guest CRUD ───────────────────────────────────────────────────────────────

class TestGuestCrud:
    def test_add_guest(self):
        es.add_guest("trip", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        guests = es.list_guests("trip")
        assert len(guests) == 1
        assert guests[0]["name"] == "Jake"
        assert guests[0]["participant_id"] == "j@s.whatsapp.net"
        assert guests[0]["channel"] == "whatsapp"
        assert guests[0]["rsvp_status"] == "enrolled"

    def test_add_telegram_guest(self):
        es.add_guest("trip", "tg:123", "Sam", channel="telegram")
        guests = es.list_guests("trip")
        assert len(guests) == 1
        assert guests[0]["channel"] == "telegram"
        assert guests[0]["phone"] is None

    def test_add_duplicate_raises(self):
        es.add_guest("trip", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        with pytest.raises(sqlite3.IntegrityError):
            es.add_guest("trip", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")

    def test_same_guest_different_events(self):
        es.add_guest("trip_a", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        es.add_guest("trip_b", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        assert es.guest_count("trip_a") == 1
        assert es.guest_count("trip_b") == 1

    def test_remove_guest(self):
        es.add_guest("trip", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        assert es.remove_guest("trip", "j@s.whatsapp.net") is True
        assert es.list_guests("trip") == []

    def test_remove_nonexistent_returns_false(self):
        assert es.remove_guest("trip", "nobody@s.whatsapp.net") is False

    def test_guest_count(self):
        assert es.guest_count("trip") == 0
        es.add_guest("trip", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        es.add_guest("trip", "m@s.whatsapp.net", "Mike", "+1666", "whatsapp")
        assert es.guest_count("trip") == 2

    def test_get_guest(self):
        es.add_guest("trip", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        g = es.get_guest("trip", "j@s.whatsapp.net")
        assert g is not None
        assert g["name"] == "Jake"

    def test_get_guest_not_found(self):
        assert es.get_guest("trip", "nobody@s.whatsapp.net") is None

    def test_list_guests_ordered_by_added_at(self):
        es.add_guest("trip", "a@s", "Alice", added_at="2026-01-01")
        es.add_guest("trip", "b@s", "Bob", added_at="2026-01-02")
        guests = es.list_guests("trip")
        assert guests[0]["name"] == "Alice"
        assert guests[1]["name"] == "Bob"

    def test_list_guests_different_events_isolated(self):
        es.add_guest("trip_a", "j@s", "Jake")
        es.add_guest("trip_b", "m@s", "Mike")
        assert len(es.list_guests("trip_a")) == 1
        assert len(es.list_guests("trip_b")) == 1
        assert es.list_guests("trip_a")[0]["name"] == "Jake"


# ── RSVP operations ─────────────────────────────────────────────────────────

class TestRsvp:
    def test_update_rsvp(self):
        es.add_guest("trip", "j@s", "Jake")
        assert es.update_rsvp("trip", "j@s", "confirmed", 3, "bringing kids") is True
        g = es.get_guest("trip", "j@s")
        assert g["rsvp_status"] == "confirmed"
        assert g["headcount"] == 3
        assert g["rsvp_note"] == "bringing kids"
        assert g["responded_at"] is not None

    def test_update_rsvp_preserves_headcount_when_none(self):
        es.add_guest("trip", "j@s", "Jake")
        es.update_rsvp("trip", "j@s", "confirmed", 3, "bringing kids")
        # Update status only — headcount and note should be preserved
        es.update_rsvp("trip", "j@s", "maybe")
        g = es.get_guest("trip", "j@s")
        assert g["rsvp_status"] == "maybe"
        assert g["headcount"] == 3
        assert g["rsvp_note"] == "bringing kids"

    def test_update_rsvp_nonexistent_returns_false(self):
        assert es.update_rsvp("trip", "nobody@s", "confirmed") is False

    def test_mark_invited(self):
        es.add_guest("trip", "j@s", "Jake")
        assert es.mark_invited("trip", "j@s") is True
        g = es.get_guest("trip", "j@s")
        assert g["rsvp_status"] == "invited"
        assert g["invited_at"] is not None

    def test_mark_invited_only_from_enrolled(self):
        es.add_guest("trip", "j@s", "Jake")
        es.update_rsvp("trip", "j@s", "confirmed")
        # Already confirmed, should not revert to invited
        assert es.mark_invited("trip", "j@s") is False

    def test_rsvp_summary(self):
        es.add_guest("trip", "j@s", "Jake")
        es.add_guest("trip", "m@s", "Mike")
        es.add_guest("trip", "s@s", "Sam")
        es.update_rsvp("trip", "j@s", "confirmed", 3)
        es.update_rsvp("trip", "m@s", "confirmed", 2)
        es.update_rsvp("trip", "s@s", "maybe", 1)

        summary = es.rsvp_summary("trip")
        assert summary["confirmed"]["count"] == 2
        assert summary["confirmed"]["headcount"] == 5
        assert summary["maybe"]["count"] == 1

    def test_rsvp_summary_empty_event(self):
        summary = es.rsvp_summary("nonexistent")
        assert summary == {}

    def test_rsvp_pending(self):
        es.add_guest("trip", "j@s", "Jake")
        es.add_guest("trip", "m@s", "Mike")
        es.add_guest("trip", "s@s", "Sam")
        es.update_rsvp("trip", "j@s", "confirmed")

        pending = es.rsvp_pending("trip")
        assert len(pending) == 2
        names = {g["name"] for g in pending}
        assert names == {"Mike", "Sam"}

    def test_rsvp_pending_includes_invited(self):
        es.add_guest("trip", "j@s", "Jake")
        es.mark_invited("trip", "j@s")
        pending = es.rsvp_pending("trip")
        assert len(pending) == 1
        assert pending[0]["rsvp_status"] == "invited"


# ── Guest summary rendering ─────────────────────────────────────────────────

class TestRenderGuestSummary:
    def test_empty_event(self):
        assert es.render_guest_summary("empty") == "## Guests (0)"

    def test_all_enrolled(self):
        es.add_guest("trip", "j@s", "Jake")
        es.add_guest("trip", "m@s", "Mike")
        summary = es.render_guest_summary("trip")
        assert "## Guests (2)" in summary
        assert "pending" in summary

    def test_mixed_statuses(self):
        es.add_guest("trip", "j@s", "Jake")
        es.add_guest("trip", "m@s", "Mike")
        es.add_guest("trip", "s@s", "Sam")
        es.update_rsvp("trip", "j@s", "confirmed", 3)
        es.update_rsvp("trip", "m@s", "maybe", 1)

        summary = es.render_guest_summary("trip")
        assert "## Guests (3)" in summary
        assert "1 confirmed (3 ppl)" in summary
        assert "1 maybe" in summary
        assert "1 pending" in summary

    def test_invited_and_enrolled_combined_as_pending(self):
        es.add_guest("trip", "j@s", "Jake")
        es.add_guest("trip", "m@s", "Mike")
        es.add_guest("trip", "s@s", "Sam")
        es.mark_invited("trip", "j@s")
        # Jake is invited, Mike and Sam are enrolled — all three are "pending"
        summary = es.render_guest_summary("trip")
        assert "## Guests (3)" in summary
        assert "3 pending" in summary
        # Should NOT have duplicate "pending" entries
        assert summary.count("pending") == 1

    def test_all_confirmed(self):
        es.add_guest("trip", "j@s", "Jake")
        es.update_rsvp("trip", "j@s", "confirmed", 2)
        summary = es.render_guest_summary("trip")
        assert "## Guests (1)" in summary
        assert "1 confirmed (2 ppl)" in summary

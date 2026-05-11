"""Tests for email_action_items.py — action item tracking for morning briefing."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import email_action_items as eai


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    store = tmp_path / "email_actions.json"
    monkeypatch.setattr(eai, "ACTIONS_FILE", store)
    monkeypatch.setattr(eai, "STATE_DIR", tmp_path)
    return store


class TestAdd:
    def test_creates_entry(self):
        eai.cmd_add("Invoice due", "billing@vendor.com", "Pay invoice", "today")
        entries = eai._load()
        assert len(entries) == 1
        assert entries[0]["subject"] == "Invoice due"
        assert entries[0]["sender"] == "billing@vendor.com"
        assert entries[0]["action"] == "Pay invoice"
        assert entries[0]["urgency"] == "today"
        assert "id" in entries[0]
        assert "created_at" in entries[0]

    def test_multiple_entries(self):
        eai.cmd_add("Invoice", "a@b.com", "Pay", "today")
        eai.cmd_add("Meeting", "c@d.com", "RSVP", "this_week")
        entries = eai._load()
        assert len(entries) == 2

    def test_optional_email_id(self):
        eai.cmd_add("Test", "a@b.com", "Do it", "low", email_id="msg123")
        entries = eai._load()
        assert entries[0]["email_id"] == "msg123"

    def test_default_empty_email_id(self):
        eai.cmd_add("Test", "a@b.com", "Do it", "low")
        entries = eai._load()
        assert entries[0]["email_id"] == ""


class TestDedupe:
    def test_same_email_id_does_not_duplicate(self, capsys):
        eai.cmd_add("Receipt", "noreply@x.com", "Review", "today",
                    email_id="msg123")
        capsys.readouterr()
        eai.cmd_add("Receipt", "noreply@x.com", "Review", "today",
                    email_id="msg123")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        assert len(eai._load()) == 1

    def test_same_subject_and_sender_no_email_id_does_not_duplicate(self, capsys):
        eai.cmd_add("Google Play receipt", "googleplay@google.com",
                    "Review", "low")
        capsys.readouterr()
        eai.cmd_add("Google Play receipt", "googleplay@google.com",
                    "Review", "low")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        assert len(eai._load()) == 1

    def test_dedupe_returns_existing_id(self, capsys):
        eai.cmd_add("X", "y@z.com", "do", "today", email_id="abc")
        first = json.loads(capsys.readouterr().out)["id"]
        eai.cmd_add("X", "y@z.com", "do", "today", email_id="abc")
        second = json.loads(capsys.readouterr().out)
        assert second["id"] == first

    def test_different_subjects_not_deduped(self):
        eai.cmd_add("Invoice A", "billing@x.com", "Pay", "today")
        eai.cmd_add("Invoice B", "billing@x.com", "Pay", "today")
        assert len(eai._load()) == 2

    def test_loop_storm_collapses_to_one(self, capsys):
        for _ in range(9):
            eai.cmd_add("Your Google Play order receipt from April 27, 2026",
                        "Google Play googleplay-noreply@google.com",
                        "Review the order details for any discrepancies.",
                        "low")
            capsys.readouterr()
        assert len(eai._load()) == 1


class TestList:
    def test_empty(self, capsys):
        eai.cmd_list()
        out = json.loads(capsys.readouterr().out)
        assert out == []

    def test_returns_all(self, capsys):
        eai.cmd_add("A", "a@b.com", "Do A", "today")
        eai.cmd_add("B", "c@d.com", "Do B", "low")
        capsys.readouterr()
        eai.cmd_list()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2


class TestComplete:
    def test_complete_by_id(self):
        eai.cmd_add("Test", "a@b.com", "Do it", "today")
        entry_id = eai._load()[0]["id"]
        eai.cmd_complete(entry_id=entry_id, subject_keyword=None)
        assert eai._load() == []

    def test_complete_by_subject(self):
        eai.cmd_add("Dentist appointment", "dr@clinic.com", "Confirm", "today")
        eai.cmd_add("Car insurance", "ins@co.com", "Renew", "this_week")
        eai.cmd_complete(entry_id=None, subject_keyword="dentist")
        entries = eai._load()
        assert len(entries) == 1
        assert entries[0]["subject"] == "Car insurance"

    def test_complete_not_found_by_id(self):
        with pytest.raises(SystemExit):
            eai.cmd_complete(entry_id="nonexistent", subject_keyword=None)

    def test_complete_not_found_by_subject(self):
        eai.cmd_add("Test", "a@b.com", "Do it", "today")
        with pytest.raises(SystemExit):
            eai.cmd_complete(entry_id=None, subject_keyword="nomatch")

    def test_subject_match_is_case_insensitive(self):
        eai.cmd_add("URGENT Invoice", "a@b.com", "Pay", "today")
        eai.cmd_complete(entry_id=None, subject_keyword="urgent")
        assert eai._load() == []


class TestCLI:
    def test_add_via_main(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "email_action_items.py", "--add",
            "--subject", "Test email",
            "--sender", "test@example.com",
            "--action", "Reply to sender",
            "--urgency", "today",
        ])
        eai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"

    def test_list_via_main(self, monkeypatch, capsys):
        eai.cmd_add("Test", "a@b.com", "Do it", "today")
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["email_action_items.py", "--list"])
        eai.main()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1

    def test_complete_via_main(self, monkeypatch, capsys):
        eai.cmd_add("Test", "a@b.com", "Do it", "today")
        entry_id = eai._load()[0]["id"]
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", [
            "email_action_items.py", "--complete", "--id", entry_id,
        ])
        eai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "completed"

    def test_clear_via_main(self, monkeypatch, capsys):
        eai.cmd_add("A", "a@b.com", "Do A", "today")
        eai.cmd_add("B", "c@d.com", "Do B", "low")
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["email_action_items.py", "--clear"])
        eai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "cleared"
        assert out["removed"] == 2
        assert eai._load() == []

    def test_clear_empty_store(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["email_action_items.py", "--clear"])
        eai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["removed"] == 0

    def test_add_missing_fields(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "email_action_items.py", "--add", "--subject", "Test",
        ])
        with pytest.raises(SystemExit):
            eai.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

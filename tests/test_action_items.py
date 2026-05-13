"""Tests for action_items.py — generic action-item tracking."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import action_items as ai


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    store = tmp_path / "action_items.json"
    monkeypatch.setattr(ai, "ITEMS_FILE", store)
    monkeypatch.setattr(ai, "STATE_DIR", tmp_path)
    return store


def _email_ref(**overrides):
    base = {
        "subject": "Invoice due",
        "sender": "billing@x.com",
        "account": "primary",
    }
    base.update(overrides)
    return base


class TestAdd:
    def test_creates_entry_with_required_fields(self, capsys):
        ai.cmd_add("email", "Pay invoice", _email_ref(),
                   "this_week", "")
        capsys.readouterr()
        entries = ai._load()
        assert len(entries) == 1
        e = entries[0]
        assert e["description"] == "Pay invoice"
        assert e["source"] == "email"
        assert e["source_ref"] == _email_ref()
        assert e["urgency"] == "this_week"
        assert e["due_at"] == ""
        assert e["status"] == "open"
        assert e["snoozed_until"] is None
        assert e["completed_at"] is None
        assert e["id"].startswith("ai_")
        assert len(e["id"]) == 11  # ai_ + 8 hex chars

    def test_due_at_is_persisted(self):
        ai.cmd_add("email", "Pay", _email_ref(), "today", "2026-05-15")
        assert ai._load()[0]["due_at"] == "2026-05-15"

    def test_supports_all_source_types(self):
        for src in ["email", "calendar", "scope", "chat", "manual", "inference"]:
            ai.cmd_add(src, f"do {src}", {"k": "v"}, "low", "")
        assert {e["source"] for e in ai._load()} == ai.VALID_SOURCES


class TestEmailDedupe:
    def test_same_message_id_dedupes(self, capsys):
        ai.cmd_add("email", "Receipt", _email_ref(message_id="m1"),
                   "low", "")
        capsys.readouterr()
        ai.cmd_add("email", "Receipt", _email_ref(message_id="m1"),
                   "low", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        assert len(ai._load()) == 1

    def test_same_subject_sender_account_dedupes_when_no_message_id(self, capsys):
        ai.cmd_add("email", "Receipt", _email_ref(), "low", "")
        capsys.readouterr()
        ai.cmd_add("email", "Receipt", _email_ref(), "low", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        assert len(ai._load()) == 1

    def test_different_account_is_not_a_duplicate(self, capsys):
        ai.cmd_add("email", "Receipt", _email_ref(account="primary"),
                   "low", "")
        capsys.readouterr()
        ai.cmd_add("email", "Receipt", _email_ref(account="personal"),
                   "low", "")
        assert len(ai._load()) == 2

    def test_completed_item_does_not_block_new_add(self, capsys):
        ai.cmd_add("email", "Pay", _email_ref(message_id="m1"),
                   "today", "")
        eid = ai._load()[0]["id"]
        ai.cmd_complete(eid)
        capsys.readouterr()
        ai.cmd_add("email", "Pay again", _email_ref(message_id="m1"),
                   "today", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert len(ai._load()) == 2

    def test_loop_storm_collapses_to_one(self, capsys):
        for _ in range(9):
            ai.cmd_add("email", "Google Play receipt",
                       _email_ref(subject="Google Play receipt",
                                  sender="noreply@google.com"),
                       "low", "")
            capsys.readouterr()
        assert len(ai._load()) == 1

    def test_subject_case_normalized(self, capsys):
        ai.cmd_add("email", "Pay", _email_ref(subject="Invoice due"), "today", "")
        capsys.readouterr()
        ai.cmd_add("email", "Pay", _email_ref(subject="invoice due"), "today", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        assert len(ai._load()) == 1

    def test_reply_forward_prefixes_normalized(self, capsys):
        ai.cmd_add("email", "Review", _email_ref(subject="Receipt"), "low", "")
        capsys.readouterr()
        ai.cmd_add("email", "Review", _email_ref(subject="Re: Receipt"), "low", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        ai.cmd_add("email", "Review", _email_ref(subject="Fwd: Re: Receipt"), "low", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "duplicate"
        assert len(ai._load()) == 1

    def test_non_email_sources_do_not_dedupe(self, capsys):
        ai.cmd_add("manual", "Buy milk", {}, "today", "")
        capsys.readouterr()
        ai.cmd_add("manual", "Buy milk", {}, "today", "")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert len(ai._load()) == 2


class TestList:
    def test_empty(self, capsys):
        ai.cmd_list(None, None, False)
        out = json.loads(capsys.readouterr().out)
        assert out == []

    def test_default_returns_only_open(self, capsys):
        ai.cmd_add("email", "A", _email_ref(message_id="a"), "today", "")
        ai.cmd_add("email", "B", _email_ref(message_id="b"), "today", "")
        ai.cmd_complete(ai._load()[0]["id"])
        capsys.readouterr()
        ai.cmd_list(None, None, False)
        out = json.loads(capsys.readouterr().out)
        assert [e["description"] for e in out] == ["B"]

    def test_filter_by_source(self, capsys):
        ai.cmd_add("email", "Email task", _email_ref(message_id="a"),
                   "today", "")
        ai.cmd_add("manual", "Manual task", {}, "today", "")
        capsys.readouterr()
        ai.cmd_list("manual", None, False)
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["source"] == "manual"

    def test_filter_by_status_snoozed(self, capsys):
        ai.cmd_add("manual", "A", {}, "low", "")
        ai.cmd_add("manual", "B", {}, "low", "")
        b_id = ai._load()[1]["id"]
        ai.cmd_snooze(b_id, "2026-06-01")
        capsys.readouterr()
        ai.cmd_list(None, "snoozed", False)
        out = json.loads(capsys.readouterr().out)
        assert [e["description"] for e in out] == ["B"]

    def test_filter_status_all_returns_everything(self, capsys):
        ai.cmd_add("manual", "A", {}, "low", "")
        ai.cmd_add("manual", "B", {}, "low", "")
        ai.cmd_complete(ai._load()[0]["id"])
        capsys.readouterr()
        ai.cmd_list(None, "all", False)
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2

    def test_due_today_filter(self, capsys, monkeypatch):
        # Freeze "today" to a known date inside the tool
        fixed = datetime(2026, 5, 13, 10, 0, tzinfo=ai.LOCAL_TZ)

        class FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr(ai, "datetime", FrozenDT)
        ai.cmd_add("email", "Today",
                   _email_ref(message_id="t"), "today", "2026-05-13")
        ai.cmd_add("email", "Tomorrow",
                   _email_ref(message_id="tm"), "today", "2026-05-14")
        ai.cmd_add("email", "No date",
                   _email_ref(message_id="nd"), "low", "")
        capsys.readouterr()
        ai.cmd_list(None, None, True)
        out = json.loads(capsys.readouterr().out)
        assert [e["description"] for e in out] == ["Today"]


class TestComplete:
    def test_marks_done_and_stamps_time(self):
        ai.cmd_add("manual", "X", {}, "low", "")
        eid = ai._load()[0]["id"]
        ai.cmd_complete(eid)
        e = ai._load()[0]
        assert e["status"] == "done"
        assert e["completed_at"]

    def test_not_found_exits(self):
        with pytest.raises(SystemExit):
            ai.cmd_complete("ai_deadbeef")

    def test_complete_is_idempotent(self, capsys):
        ai.cmd_add("manual", "X", {}, "low", "")
        eid = ai._load()[0]["id"]
        ai.cmd_complete(eid)
        first_completed_at = ai._load()[0]["completed_at"]
        capsys.readouterr()
        ai.cmd_complete(eid)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "already_done"
        assert ai._load()[0]["completed_at"] == first_completed_at


class TestSnooze:
    def test_sets_status_and_until(self):
        ai.cmd_add("manual", "X", {}, "low", "")
        eid = ai._load()[0]["id"]
        ai.cmd_snooze(eid, "2026-06-01")
        e = ai._load()[0]
        assert e["status"] == "snoozed"
        assert e["snoozed_until"] == "2026-06-01"

    def test_not_found_exits(self):
        with pytest.raises(SystemExit):
            ai.cmd_snooze("ai_deadbeef", "2026-06-01")


class TestRemove:
    def test_drops_entry(self):
        ai.cmd_add("manual", "X", {}, "low", "")
        eid = ai._load()[0]["id"]
        ai.cmd_remove(eid)
        assert ai._load() == []

    def test_not_found_exits(self):
        with pytest.raises(SystemExit):
            ai.cmd_remove("ai_deadbeef")


class TestCLI:
    def _argv(self, *args):
        return ["action_items.py", *args]

    def test_add_via_main(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", self._argv(
            "--add", "--source", "email",
            "--description", "Pay invoice",
            "--source-ref", json.dumps(_email_ref()),
            "--urgency", "this_week", "--due", "2026-05-15",
        ))
        ai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert ai._load()[0]["due_at"] == "2026-05-15"

    def test_add_rejects_unknown_source(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", self._argv(
            "--add", "--source", "telepathy",
            "--description", "x", "--source-ref", "{}",
        ))
        with pytest.raises(SystemExit):
            ai.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_add_rejects_unknown_urgency(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", self._argv(
            "--add", "--source", "manual", "--description", "x",
            "--urgency", "yesterday",
        ))
        with pytest.raises(SystemExit):
            ai.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_add_rejects_invalid_due(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", self._argv(
            "--add", "--source", "manual", "--description", "x",
            "--due", "next Friday",
        ))
        with pytest.raises(SystemExit):
            ai.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_add_rejects_invalid_source_ref(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", self._argv(
            "--add", "--source", "email", "--description", "x",
            "--source-ref", "not json",
        ))
        with pytest.raises(SystemExit):
            ai.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_list_via_main(self, monkeypatch, capsys):
        ai.cmd_add("manual", "A", {}, "low", "")
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", self._argv("--list"))
        ai.main()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1

    def test_complete_via_main(self, monkeypatch, capsys):
        ai.cmd_add("manual", "A", {}, "low", "")
        eid = ai._load()[0]["id"]
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", self._argv(
            "--complete", "--id", eid,
        ))
        ai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "completed"

    def test_snooze_via_main(self, monkeypatch, capsys):
        ai.cmd_add("manual", "A", {}, "low", "")
        eid = ai._load()[0]["id"]
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", self._argv(
            "--snooze", "--id", eid, "--until", "2026-06-01",
        ))
        ai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "snoozed"

    def test_remove_via_main(self, monkeypatch, capsys):
        ai.cmd_add("manual", "A", {}, "low", "")
        eid = ai._load()[0]["id"]
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", self._argv(
            "--remove", "--id", eid,
        ))
        ai.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"

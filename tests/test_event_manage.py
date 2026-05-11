"""Tests for event_manage.py — event lifecycle, items, guest counting, and budget summary."""

import json
import re
from pathlib import Path

import pytest

import tools.event_manage as em
import tools.event_store as es


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def events_dir(tmp_path, monkeypatch):
    edir = tmp_path / "events"
    edir.mkdir()
    monkeypatch.setattr(em, "EVENTS_DIR", edir)
    # Point event_store at a temp DB
    events_db = tmp_path / "state" / "events.db"
    monkeypatch.setenv("HOMER_EVENTS_DB", str(events_db))
    return edir


@pytest.fixture()
def event(events_dir, monkeypatch, capsys):
    """Create a test event and return its event_id."""
    # Stub out budget sheet creation (needs real Google credentials)
    monkeypatch.setattr(em, "create_budget_sheet", lambda name, eid: None)
    em.do_create("MTB Colorado", "mtb_colorado")
    capsys.readouterr()  # clear stdout
    return "mtb_colorado"


# ── do_create ─────────────────────────────────────────────────────────────────

class TestCreate:
    def test_creates_status_file(self, events_dir, monkeypatch, capsys):
        monkeypatch.setattr(em, "create_budget_sheet", lambda n, e: None)
        em.do_create("Beach Trip", "beach_trip")
        sp = events_dir / "beach_trip" / "status.md"
        assert sp.exists()
        content = sp.read_text()
        assert "# Beach Trip" in content
        assert "Status: Coordinating" in content
        assert "Event created: Beach Trip" in content

    def test_create_output_json(self, events_dir, monkeypatch, capsys):
        monkeypatch.setattr(em, "create_budget_sheet", lambda n, e: None)
        em.do_create("Beach Trip", "beach_trip")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "created"
        assert out["event_id"] == "beach_trip"
        assert out["name"] == "Beach Trip"

    def test_create_duplicate_fails(self, event, events_dir, monkeypatch, capsys):
        monkeypatch.setattr(em, "create_budget_sheet", lambda n, e: None)
        with pytest.raises(SystemExit):
            em.do_create("MTB Colorado", "mtb_colorado")

    def test_create_with_budget_sheet(self, events_dir, monkeypatch, capsys):
        sheet_info = {"url": "https://sheets.example.com/abc", "sheet_id": "abc123", "title": "Budget", "sheets": ["Expenses", "Summary"]}
        monkeypatch.setattr(em, "create_budget_sheet", lambda n, e: sheet_info)
        em.do_create("Trip", "trip_x")
        content = (events_dir / "trip_x" / "status.md").read_text()
        assert "Sheet: https://sheets.example.com/abc" in content
        assert "Sheet-ID: abc123" in content
        out = json.loads(capsys.readouterr().out)
        assert out["budget_sheet_url"] == "https://sheets.example.com/abc"


# ── do_status ─────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_shows_defaults(self, event, events_dir, capsys):
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["name"] == "MTB Colorado"
        assert out["status"] == "Coordinating"
        assert out["dates"] == "TBD"
        assert out["guest_count"] == 0
        assert out["open_items"] == 0

    def test_status_nonexistent_event_fails(self, events_dir, capsys):
        with pytest.raises(SystemExit):
            em.do_status("nonexistent")

    def test_status_counts_open_items(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights")
        capsys.readouterr()
        em.do_add_item("mtb_colorado", "Rent bikes")
        capsys.readouterr()
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["open_items"] == 2

    def test_status_counts_checked_items(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights")
        capsys.readouterr()
        em.do_check_item("mtb_colorado", "flights")
        capsys.readouterr()
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["open_items"] == 0
        assert out["completed_items"] == 1

    def test_status_includes_notes_empty(self, event, events_dir, capsys):
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert "notes" in out
        assert out["notes"] == []

    def test_status_includes_notes_after_add_note(self, event, events_dir, capsys):
        em.do_add_note("mtb_colorado", "Jake confirmed: available July 15-20")
        capsys.readouterr()
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert "notes" in out
        assert any("Jake confirmed" in note for note in out["notes"])

    def test_status_includes_multiple_notes(self, event, events_dir, capsys):
        em.do_add_note("mtb_colorado", "First note")
        em.do_add_note("mtb_colorado", "Second note")
        capsys.readouterr()
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert len(out["notes"]) == 2
        assert any("First note" in note for note in out["notes"])
        assert any("Second note" in note for note in out["notes"])

    def test_status_notes_not_mixed_with_activity_log(self, event, events_dir, capsys):
        """Activity log entries must not appear in the notes field."""
        em.do_add_note("mtb_colorado", "Jake confirmed")
        capsys.readouterr()
        em.do_status("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert not any("Event created" in note for note in out["notes"])


# ── do_update ─────────────────────────────────────────────────────────────────

class TestUpdate:
    def test_update_dates(self, event, events_dir, capsys):
        em.do_update("mtb_colorado", "dates", "2026-07-15 to 2026-07-20")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Dates: 2026-07-15 to 2026-07-20" in content
        assert "Dates: TBD" not in content

    def test_update_confirmed_detail_new(self, event, events_dir, capsys):
        em.do_update("mtb_colorado", "Location", "Crested Butte, CO")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "- **Location**: Crested Butte, CO" in content

    def test_update_confirmed_detail_overwrite(self, event, events_dir, capsys):
        em.do_update("mtb_colorado", "Location", "Crested Butte, CO")
        capsys.readouterr()
        em.do_update("mtb_colorado", "Location", "Moab, UT")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "- **Location**: Moab, UT" in content
        # Old value still in activity log (correct), but not as a confirmed detail
        confirmed = content.split("## Confirmed Details")[1].split("## Budget")[0]
        assert "Crested Butte" not in confirmed

    def test_update_logs_activity(self, event, events_dir, capsys):
        em.do_update("mtb_colorado", "Lodging", "Airbnb #123")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Updated Lodging: Airbnb #123" in content


# ── do_add_item / do_check_item / do_remove_item ─────────────────────────────

class TestItems:
    def test_add_item(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights", "@all")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "- [ ] Book flights (@all)" in content

    def test_add_item_no_assignee(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Buy snacks")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "- [ ] Buy snacks\n" in content
        assert "(@" not in content.split("Buy snacks")[1].split("\n")[0]

    def test_check_item(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights")
        capsys.readouterr()
        em.do_check_item("mtb_colorado", "flights")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "checked"
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "- [x] Book flights" in content
        assert "- [ ] Book flights" not in content

    def test_check_item_not_found(self, event, events_dir, capsys):
        with pytest.raises(SystemExit):
            em.do_check_item("mtb_colorado", "nonexistent_item")

    def test_remove_item(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights")
        capsys.readouterr()
        em.do_remove_item("mtb_colorado", "flights")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        # Item removed from Open Items section (still in activity log, which is correct)
        items_section = content.split("## Open Items")[1].split("## Confirmed")[0]
        assert "Book flights" not in items_section

    def test_remove_item_not_found(self, event, events_dir, capsys):
        with pytest.raises(SystemExit):
            em.do_remove_item("mtb_colorado", "nonexistent_item")

    def test_multiple_items(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights")
        em.do_add_item("mtb_colorado", "Rent bikes")
        em.do_add_item("mtb_colorado", "Reserve Airbnb")
        capsys.readouterr()

        em.do_check_item("mtb_colorado", "flights")
        capsys.readouterr()

        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "- [x] Book flights" in content
        assert "- [ ] Rent bikes" in content
        assert "- [ ] Reserve Airbnb" in content


# ── do_set_status ─────────────────────────────────────────────────────────────

class TestSetStatus:
    def test_set_confirmed(self, event, events_dir, capsys):
        em.do_set_status("mtb_colorado", "confirmed")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Status: Confirmed" in content
        assert "Status: Coordinating" not in content

    def test_set_active(self, event, events_dir, capsys):
        em.do_set_status("mtb_colorado", "active")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Status: Active" in content

    def test_archived_lifecycle_rejected(self, event, events_dir, capsys):
        with pytest.raises(SystemExit):
            em.do_set_status("mtb_colorado", "archived")

    def test_custom_lifecycle_accepted(self, event, events_dir, capsys):
        em.do_set_status("mtb_colorado", "deposits due")
        capsys.readouterr()
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Status: Deposits due" in content

    def test_set_status_logs_activity(self, event, events_dir, capsys):
        em.do_set_status("mtb_colorado", "confirmed")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Status changed to: Confirmed" in content


# ── do_close ──────────────────────────────────────────────────────────────────

class TestClose:
    def _stub_subprocess(self, monkeypatch):
        """Stub out subprocess.run so no real guest removal or budget calls happen."""
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import subprocess
            r = subprocess.CompletedProcess(cmd, 0, stdout='{"status":"ok"}', stderr="")
            return r
        monkeypatch.setattr(em.subprocess, "run", fake_run)
        return calls

    def test_close_archives_event(self, event, events_dir, monkeypatch, capsys):
        self._stub_subprocess(monkeypatch)
        em.do_close("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "archived"
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Status: Archived" in content

    def test_close_logs_activity(self, event, events_dir, monkeypatch, capsys):
        self._stub_subprocess(monkeypatch)
        em.do_close("mtb_colorado")
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Event archived" in content

    def test_close_revokes_guests(self, events_dir, monkeypatch, capsys):
        # Create event with guests in event_store
        edir = events_dir / "trip"
        edir.mkdir()
        content = """# Trip
Status: Confirmed
Dates: 2026-07-15
Created: 2026-03-01

## Guests (2)
2 pending

## Open Items

## Activity Log
| Date | What |
|------|------|
"""
        (edir / "status.md").write_text(content)
        es.add_guest("trip", "1555@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        es.add_guest("trip", "1666@s.whatsapp.net", "Mike", "+1666", "whatsapp")
        calls = self._stub_subprocess(monkeypatch)
        em.do_close("trip")
        out = json.loads(capsys.readouterr().out)
        assert set(out["guests_revoked"]) == {"Jake", "Mike"}
        # Verify manage_event_guest.py --remove was called for each guest
        remove_calls = [c for c in calls if "--remove" in c]
        assert len(remove_calls) == 2

    def test_close_no_guests_still_archives(self, event, events_dir, monkeypatch, capsys):
        self._stub_subprocess(monkeypatch)
        em.do_close("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "archived"
        assert out["guests_revoked"] == []

    def test_close_runs_final_budget_when_sheet_exists(self, events_dir, monkeypatch, capsys):
        edir = events_dir / "funded"
        edir.mkdir()
        content = """# Funded Trip
Status: Active
Dates: 2026-07-15
Created: 2026-03-01

## Guests

## Budget
Sheet: https://docs.google.com/spreadsheets/d/ABC
Sheet-ID: ABC

## Activity Log
| Date | What |
|------|------|
"""
        (edir / "status.md").write_text(content)
        budget_response = json.dumps({"event_id": "funded", "total_expenses": 500.0})
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import subprocess
            stdout = budget_response if "--budget-summary" in cmd else '{"status":"ok"}'
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        monkeypatch.setattr(em.subprocess, "run", fake_run)
        em.do_close("funded")
        out = json.loads(capsys.readouterr().out)
        assert out["final_budget"]["total_expenses"] == 500.0
        assert any("--budget-summary" in c for c in calls)


# ── do_list ───────────────────────────────────────────────────────────────────

class TestList:
    def test_list_empty(self, events_dir, capsys):
        em.do_list()
        out = json.loads(capsys.readouterr().out)
        assert out == []

    def test_list_one_event(self, event, events_dir, capsys):
        em.do_list()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["event_id"] == "mtb_colorado"
        assert out[0]["name"] == "MTB Colorado"
        assert out[0]["status"] == "Coordinating"

    def test_list_multiple_events(self, events_dir, monkeypatch, capsys):
        monkeypatch.setattr(em, "create_budget_sheet", lambda n, e: None)
        em.do_create("Trip A", "trip_a")
        capsys.readouterr()
        em.do_create("Trip B", "trip_b")
        capsys.readouterr()
        em.do_list()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2
        names = {e["name"] for e in out}
        assert names == {"Trip A", "Trip B"}

    def test_list_no_events_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(em, "EVENTS_DIR", tmp_path / "nonexistent")
        em.do_list()
        out = json.loads(capsys.readouterr().out)
        assert out == []


# ── Activity log ──────────────────────────────────────────────────────────────

class TestActivityLog:
    def test_activity_log_records_create(self, event, events_dir):
        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        assert "Event created: MTB Colorado" in content

    def test_activity_log_appended_when_section_missing(self, events_dir, monkeypatch, capsys):
        """If status.md was manually edited and Activity Log section removed, it gets recreated."""
        monkeypatch.setattr(em, "create_budget_sheet", lambda n, e: None)
        em.do_create("Trip", "trip_no_log")
        capsys.readouterr()
        # Manually strip the Activity Log section
        sp = events_dir / "trip_no_log" / "status.md"
        content = sp.read_text()
        content = content[:content.index("## Activity Log")]
        sp.write_text(content)
        # Now update — should recreate the section
        em.do_update("trip_no_log", "Location", "Denver")
        result = sp.read_text()
        assert "## Activity Log" in result
        assert "Updated Location: Denver" in result

    def test_activity_log_records_multiple_actions(self, event, events_dir, capsys):
        em.do_add_item("mtb_colorado", "Book flights")
        em.do_update("mtb_colorado", "Location", "Moab")
        em.do_check_item("mtb_colorado", "flights")
        capsys.readouterr()

        content = (events_dir / "mtb_colorado" / "status.md").read_text()
        log_section = content[content.index("## Activity Log"):]
        assert "Event created" in log_section
        assert "Added item: Book flights" in log_section
        assert "Updated Location: Moab" in log_section
        assert "Completed: Book flights" in log_section


# ── Budget summary: roster completeness ───────────────────────────────────────

class TestBudgetSummaryRoster:
    def test_budget_includes_guests_who_havent_paid(self, events_dir):
        """event_store.list_guests is used to include all roster members in per_person output."""
        es.add_guest("test_budget", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        es.add_guest("test_budget", "m@s.whatsapp.net", "Mike", "+1666", "whatsapp")
        guests = es.list_guests("test_budget")
        names = {g["name"] for g in guests}
        # Simulate: only Jake has paid
        totals_paid = {"Jake": 200.0}
        all_names = set(names)
        all_names.update(totals_paid.keys())
        # Mike should still be in the set even though he hasn't paid
        assert "Mike" in all_names
        assert "Jake" in all_names


# ── do_budget_summary ─────────────────────────────────────────────────────────

STATUS_WITH_SHEET = """\
# MTB Colorado
Status: Coordinating
Dates: TBD

## Guests (1)
1 pending

## Budget
Sheet: https://sheets.example.com/abc
Sheet-ID: abc123

## Activity Log
| Date | What |
|------|------|
"""

EXPENSE_ROWS = {
    "values": [
        ["Date", "Item", "Amount", "Paid By", "Split Among", "Notes"],
        ["2026-03-19", "Airbnb deposit", "400", "Jake", "all", ""],
        ["2026-03-20", "Gas", "50", "Mike", "all", ""],
    ]
}


class TestDoBudgetSummary:
    def _make_subprocess_mock(self, monkeypatch, read_response=None, captured_writes=None):
        """Stub subprocess.run: returns read_response for --mode read, captures writes."""
        import subprocess as sp_module
        import tools.event_manage as em_module

        read_resp = read_response or EXPENSE_ROWS

        class FakeResult:
            returncode = 0
            stdout = json.dumps(read_resp)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if captured_writes is not None and "--mode" in cmd:
                idx = cmd.index("--mode")
                mode = cmd[idx + 1]
                if mode == "write":
                    captured_writes.append(cmd)
            return FakeResult()

        monkeypatch.setattr(em_module.subprocess, "run", fake_run)
        return calls

    def test_budget_summary_output(self, events_dir, monkeypatch, capsys):
        (events_dir / "mtb_colorado").mkdir()
        (events_dir / "mtb_colorado" / "status.md").write_text(STATUS_WITH_SHEET)
        self._make_subprocess_mock(monkeypatch)
        em.do_budget_summary("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["total_expenses"] == 450.0
        assert out["expense_count"] == 2
        assert "Jake" in out["per_person"]

    def test_budget_summary_fair_share(self, events_dir, monkeypatch, capsys):
        (events_dir / "mtb_colorado").mkdir()
        (events_dir / "mtb_colorado" / "status.md").write_text(STATUS_WITH_SHEET)
        # Add guest to event_store so budget knows about them
        es.add_guest("mtb_colorado", "j@s.whatsapp.net", "Jake", "+1555", "whatsapp")
        self._make_subprocess_mock(monkeypatch)
        em.do_budget_summary("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        # 1 guest + 1 owner = 2 people; $450 / 2 = $225
        assert out["fair_share_per_person"] == 225.0
        assert out["per_person"]["Jake"]["paid"] == 400.0
        assert out["per_person"]["Jake"]["balance"] == 175.0   # overpaid
        assert out["per_person"]["Mike"]["paid"] == 50.0
        assert out["per_person"]["Mike"]["balance"] == -175.0  # underpaid

    def test_budget_summary_writes_to_summary_tab(self, events_dir, monkeypatch, capsys):
        (events_dir / "mtb_colorado").mkdir()
        (events_dir / "mtb_colorado" / "status.md").write_text(STATUS_WITH_SHEET)
        writes = []
        self._make_subprocess_mock(monkeypatch, captured_writes=writes)
        em.do_budget_summary("mtb_colorado")
        capsys.readouterr()
        assert any("Summary!A1" in " ".join(cmd) for cmd in writes), "Should write to Summary tab"

    def test_budget_summary_no_expenses(self, events_dir, monkeypatch, capsys):
        (events_dir / "mtb_colorado").mkdir()
        (events_dir / "mtb_colorado" / "status.md").write_text(STATUS_WITH_SHEET)
        self._make_subprocess_mock(monkeypatch, read_response={"values": [["Date", "Item", "Amount", "Paid By"]]})
        em.do_budget_summary("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["total_expenses"] == 0
        assert out["message"] == "No expenses logged yet"

    def test_budget_summary_no_sheet_fails(self, events_dir, monkeypatch, capsys):
        (events_dir / "mtb_colorado").mkdir()
        (events_dir / "mtb_colorado" / "status.md").write_text("# MTB Colorado\nStatus: Coordinating\n")
        with pytest.raises(SystemExit):
            em.do_budget_summary("mtb_colorado")

    def test_budget_summary_skips_malformed_rows(self, events_dir, monkeypatch, capsys):
        (events_dir / "mtb_colorado").mkdir()
        (events_dir / "mtb_colorado" / "status.md").write_text(STATUS_WITH_SHEET)
        bad_data = {"values": [
            ["Date", "Item", "Amount", "Paid By"],
            ["2026-03-19", "Gas"],           # too short — skipped
            ["2026-03-20", "Food", "not-a-number", "Jake"],  # bad amount — skipped
            ["2026-03-21", "Hotel", "300", "Jake"],          # valid
        ]}
        self._make_subprocess_mock(monkeypatch, read_response=bad_data)
        em.do_budget_summary("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out["total_expenses"] == 300.0
        assert out["expense_count"] == 1


# ── do_add_note ───────────────────────────────────────────────────────────────

class TestDoAddNote:
    def test_note_appended_to_notes_section(self, event, events_dir, capsys):
        em.do_add_note(event, "Sent brunch options to all guests")
        capsys.readouterr()
        content = (events_dir / event / "status.md").read_text()
        assert "Sent brunch options to all guests" in content
        assert "## Notes" in content

    def test_note_has_timestamp(self, event, events_dir, capsys):
        em.do_add_note(event, "Some note")
        capsys.readouterr()
        content = (events_dir / event / "status.md").read_text()
        import re
        assert re.search(r"- \d{4}-\d{2}-\d{2} \d{2}:\d{2}: Some note", content)

    def test_multiple_notes_accumulate(self, event, events_dir, capsys):
        em.do_add_note(event, "First note")
        em.do_add_note(event, "Second note")
        capsys.readouterr()
        content = (events_dir / event / "status.md").read_text()
        assert "First note" in content
        assert "Second note" in content

    def test_notes_section_created_when_missing(self, events_dir, capsys, monkeypatch):
        edir = events_dir / "nonotes"
        edir.mkdir()
        content = "# No Notes Event\nStatus: Confirmed\n\n## Activity Log\n| Date | What |\n|------|------|\n"
        (edir / "status.md").write_text(content)
        monkeypatch.setattr(em, "EVENTS_DIR", events_dir)
        em.do_add_note("nonotes", "A note")
        capsys.readouterr()
        result = (edir / "status.md").read_text()
        assert "## Notes" in result
        assert "A note" in result

    def test_output_is_ok_json(self, event, events_dir, capsys):
        em.do_add_note(event, "test note")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["note"] == "test note"

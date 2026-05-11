"""
Tests for calendar_add.py — gogcli wrapper for Google Calendar writes.

Mocks subprocess.run + load_google_credentials so no real binary or token
is required.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import calendar_add as ca
import gogcli


def _args(**kwargs):
    """Build a minimal args namespace covering all argparse flags."""
    defaults = {
        "title": "Test Event",
        "date": "2026-03-22",
        "time": None,
        "end_date": None,
        "end_time": None,
        "duration": None,
        "location": None,
        "description": None,
        "calendar": "primary",
        "account": "primary",
        "edit": False,
        "event_id": None,
        "search": False,
        "recur": None,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return type("Args", (), defaults)()


# ── build_event_body() — preserves canonical Google Calendar event shape ──────


class TestAllDayEvent:
    def test_single_day_start_equals_date(self):
        body = ca.build_event_body(_args(date="2026-03-22"))
        assert body["start"] == {"date": "2026-03-22"}

    def test_single_day_end_is_next_day(self):
        """Google Calendar all-day end is exclusive — must be day+1."""
        body = ca.build_event_body(_args(date="2026-03-22"))
        assert body["end"] == {"date": "2026-03-23"}

    def test_multiday_end_date_inclusive(self):
        body = ca.build_event_body(_args(date="2026-03-22", end_date="2026-03-28"))
        assert body["end"] == {"date": "2026-03-29"}

    def test_multiday_spans_month_boundary(self):
        body = ca.build_event_body(_args(date="2026-03-30", end_date="2026-04-02"))
        assert body["end"] == {"date": "2026-04-03"}

    def test_end_date_same_as_start_treated_as_single_day(self):
        body = ca.build_event_body(_args(date="2026-03-22", end_date="2026-03-22"))
        assert body["end"] == {"date": "2026-03-23"}

    def test_no_datetime_keys_in_allday(self):
        body = ca.build_event_body(_args(date="2026-03-22", end_date="2026-03-25"))
        assert "dateTime" not in body["start"]
        assert "dateTime" not in body["end"]


class TestRecurringEvent:
    def test_daily_shorthand(self):
        body = ca.build_event_body(_args(date="2026-03-22", recur="daily"))
        assert body["recurrence"] == ["RRULE:FREQ=DAILY"]

    def test_weekly_shorthand(self):
        body = ca.build_event_body(_args(date="2026-03-22", recur="weekly"))
        assert body["recurrence"] == ["RRULE:FREQ=WEEKLY"]

    def test_monthly_shorthand(self):
        body = ca.build_event_body(_args(date="2026-03-22", recur="monthly"))
        assert body["recurrence"] == ["RRULE:FREQ=MONTHLY"]

    def test_case_insensitive(self):
        body = ca.build_event_body(_args(date="2026-03-22", recur="WEEKLY"))
        assert body["recurrence"] == ["RRULE:FREQ=WEEKLY"]

    def test_raw_rrule_passthrough(self):
        rrule = "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"
        body = ca.build_event_body(_args(date="2026-03-22", recur=rrule))
        assert body["recurrence"] == [rrule]

    def test_no_recur_omits_key(self):
        body = ca.build_event_body(_args(date="2026-03-22"))
        assert "recurrence" not in body

    def test_invalid_recur_raises_value_error(self):
        with pytest.raises(ValueError, match="--recur"):
            ca.build_event_body(_args(date="2026-03-22", recur="biweekly"))


class TestTimedEvent:
    def test_end_date_ignored_when_time_set(self):
        """--end-date must not affect timed events."""
        body = ca.build_event_body(_args(date="2026-03-22", time="10:00", end_date="2026-03-28"))
        assert "dateTime" in body["start"]
        assert "dateTime" in body["end"]
        start_dt = datetime.fromisoformat(body["start"]["dateTime"])
        end_dt = datetime.fromisoformat(body["end"]["dateTime"])
        assert (end_dt - start_dt).seconds == 3600


# ── build_gog_args() — gogcli flag construction ───────────────────────────────


class TestResolveWhen:
    """Single source of truth for time/date math — both build_event_body and
    build_gog_args delegate here, so these tests guard against drift."""

    def test_timed_default_duration_is_60_min(self):
        start, end, all_day = ca._resolve_when(_args(date="2026-03-22", time="10:00"))
        assert all_day is False
        assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).seconds == 3600

    def test_timed_explicit_duration(self):
        start, end, _ = ca._resolve_when(_args(date="2026-03-22", time="10:00", duration=90))
        assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).seconds == 90 * 60

    def test_timed_explicit_end_time_overrides_duration(self):
        start, end, _ = ca._resolve_when(_args(
            date="2026-03-22", time="10:00", end_time="11:30", duration=120,
        ))
        assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).seconds == 90 * 60

    def test_all_day_single_day_end_is_plus_one(self):
        start, end, all_day = ca._resolve_when(_args(date="2026-03-22"))
        assert all_day is True
        assert start == "2026-03-22"
        assert end == "2026-03-23"

    def test_all_day_multiday_end_is_inclusive_plus_one(self):
        start, end, _ = ca._resolve_when(_args(date="2026-03-22", end_date="2026-03-28"))
        assert start == "2026-03-22"
        assert end == "2026-03-29"

    def test_all_day_end_date_ignored_when_time_set(self):
        """--end-date must not affect timed events."""
        _, _, all_day = ca._resolve_when(_args(date="2026-03-22", time="10:00", end_date="2026-03-28"))
        assert all_day is False


class TestBuildGogArgs:
    def test_timed_event_uses_from_to_isoformat(self):
        flags = ca.build_gog_args(_args(date="2026-03-22", time="10:00", duration=90))
        assert "--summary" in flags and "Test Event" in flags
        i_from = flags.index("--from")
        i_to = flags.index("--to")
        # Parse flag values as datetime, confirm 90-min span
        start = datetime.fromisoformat(flags[i_from + 1])
        end = datetime.fromisoformat(flags[i_to + 1])
        assert (end - start).seconds == 90 * 60
        assert "--all-day" not in flags

    def test_all_day_uses_dates_and_all_day_flag(self):
        flags = ca.build_gog_args(_args(date="2026-03-22"))
        assert "--all-day" in flags
        assert "--from" in flags
        assert "2026-03-22" in flags  # start date
        assert "2026-03-23" in flags  # end is +1

    def test_multiday_all_day_end_is_inclusive_plus_one(self):
        flags = ca.build_gog_args(_args(date="2026-03-22", end_date="2026-03-28"))
        i_to = flags.index("--to")
        assert flags[i_to + 1] == "2026-03-29"

    def test_recurrence_emits_rrule_flag(self):
        flags = ca.build_gog_args(_args(date="2026-03-22", recur="weekly"))
        assert "--rrule" in flags
        assert "RRULE:FREQ=WEEKLY" in flags

    def test_invalid_recur_raises_value_error(self):
        with pytest.raises(ValueError, match="--recur"):
            ca.build_gog_args(_args(date="2026-03-22", recur="biweekly"))

    def test_location_and_description_passed_when_set(self):
        flags = ca.build_gog_args(_args(date="2026-03-22", location="Park", description="Bring snacks"))
        i_loc = flags.index("--location")
        i_desc = flags.index("--description")
        assert flags[i_loc + 1] == "Park"
        assert flags[i_desc + 1] == "Bring snacks"

    def test_location_omitted_when_unset(self):
        flags = ca.build_gog_args(_args(date="2026-03-22"))
        assert "--location" not in flags
        assert "--description" not in flags


# ── format_result() ───────────────────────────────────────────────────────────


def test_format_result_timed_event():
    event = {
        "id": "evt1",
        "htmlLink": "https://link",
        "summary": "Standup",
        "start": {"dateTime": "2026-03-22T15:30:00-04:00"},
    }
    out = ca.format_result(event, "created")
    assert out == {
        "status": "created",
        "event_id": "evt1",
        "link": "https://link",
        "title": "Standup",
        "date": "2026-03-22",
        "time": "3:30 PM",
    }


def test_format_result_all_day_event():
    event = {
        "id": "evt2",
        "htmlLink": "https://link",
        "summary": "Holiday",
        "start": {"date": "2026-03-22"},
    }
    out = ca.format_result(event, "updated")
    assert out["status"] == "updated"
    assert out["date"] == "2026-03-22"
    assert out["time"] == "all-day"


# ── tool-specific mocks ────────────────────────────────────────────────────────


def _mock_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ── get_access_token() ────────────────────────────────────────────────────────


def test_get_access_token_requires_calendar_scope(monkeypatch):
    creds = MagicMock(token="abc", scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(ca, "load_google_credentials", lambda a: creds)
    with pytest.raises(PermissionError, match="calendar"):
        ca.get_access_token("primary")


def test_get_access_token_passes_when_scope_present(monkeypatch):
    creds = MagicMock(token="abc", scopes=[ca.CALENDAR_SCOPE])
    monkeypatch.setattr(ca, "load_google_credentials", lambda a: creds)
    assert ca.get_access_token("primary") == "abc"


# ── search_events() ───────────────────────────────────────────────────────────


def test_search_events_filters_by_substring(monkeypatch):
    monkeypatch.setattr(ca, "list_calendars", lambda token: [{"id": "primary", "summary": "Personal"}])
    monkeypatch.setattr(gogcli, "run",
        lambda token, *args: {"events": [
            {"id": "e1", "summary": "Karate practice",
             "start": {"dateTime": "2026-03-22T10:00:00-04:00"}, "CalendarID": "primary"},
            {"id": "e2", "summary": "Soccer practice",
             "start": {"dateTime": "2026-03-22T14:00:00-04:00"}, "CalendarID": "primary"},
        ]},
    )
    matches = ca.search_events("tok", "Karate", "2026-03-22", "primary")
    assert len(matches) == 1
    assert matches[0]["event_id"] == "e1"
    assert matches[0]["calendar_id"] == "primary"
    assert matches[0]["time"] == "10:00 AM"


def test_search_events_handles_all_day(monkeypatch):
    monkeypatch.setattr(ca, "list_calendars", lambda token: [{"id": "primary", "summary": "P"}])
    monkeypatch.setattr(gogcli, "run",
        lambda token, *args: {"events": [
            {"id": "e1", "summary": "Holiday", "start": {"date": "2026-03-22"}, "CalendarID": "primary"},
        ]},
    )
    matches = ca.search_events("tok", "Holiday", "2026-03-22", "primary")
    assert matches[0]["time"] == "all-day"


def test_search_events_invalid_date(monkeypatch):
    with pytest.raises(RuntimeError, match="Invalid date"):
        ca.search_events("tok", "x", "not-a-date", "primary")


def test_search_events_explicit_calendar_skips_list_calendars(monkeypatch):
    """If --calendar is set to a non-primary value, don't fetch the full calendar list."""
    list_called = []
    monkeypatch.setattr(ca, "list_calendars", lambda token: list_called.append(token) or [])
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: {"events": []})
    ca.search_events("tok", "x", "2026-03-22", "specific@cal.id")
    assert list_called == []


def test_search_events_returns_empty_when_gogcli_returns_no_events(monkeypatch):
    monkeypatch.setattr(ca, "list_calendars", lambda token: [{"id": "primary", "summary": "P"}])
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: {"events": []})
    assert ca.search_events("tok", "Karate", "2026-03-22", "primary") == []


# ── main() ────────────────────────────────────────────────────────────────────


def test_main_dry_run_prints_event_body(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--title", "Test", "--date", "2026-03-22", "--dry-run",
    ])
    ca.main()
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert '"summary": "Test"' in out
    assert '"date": "2026-03-22"' in out


def test_main_dry_run_does_not_call_gogcli(monkeypatch):
    called = []
    monkeypatch.setattr(ca, "get_access_token", lambda a: called.append("token") or "tok")
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: called.append("run_gog") or {})
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--title", "T", "--date", "2026-03-22", "--dry-run",
    ])
    ca.main()
    assert called == []


def test_main_create_happy_path(capsys, monkeypatch):
    monkeypatch.setattr(ca, "get_access_token", lambda a: "tok")

    def fake_run_gog(token, *args):
        assert "create" in args
        return {"event": {
            "id": "new-id", "htmlLink": "https://x", "summary": "T",
            "start": {"dateTime": "2026-03-22T10:00:00-04:00"},
        }}

    monkeypatch.setattr(gogcli, "run", fake_run_gog)
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--title", "T", "--date", "2026-03-22", "--time", "10:00",
    ])
    ca.main()
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "created"
    assert out["event_id"] == "new-id"
    assert out["time"] == "10:00 AM"


def test_main_edit_passes_event_id(monkeypatch):
    captured = {}

    def fake_run_gog(token, *args):
        captured["args"] = args
        return {"event": {"id": "abc", "summary": "x", "start": {"date": "2026-03-22"}}}

    monkeypatch.setattr(ca, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(gogcli, "run", fake_run_gog)
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--edit", "--event-id", "abc",
        "--title", "T", "--date", "2026-03-22",
    ])
    ca.main()
    assert "update" in captured["args"]
    assert "abc" in captured["args"]


def test_main_edit_without_event_id_errors(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--edit", "--title", "T", "--date", "2026-03-22",
    ])
    with pytest.raises(SystemExit):
        ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "event-id" in out["error"]


def test_main_missing_title_errors(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["calendar_add.py", "--date", "2026-03-22"])
    with pytest.raises(SystemExit):
        ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "required" in out["error"]


def test_main_search_returns_matches(capsys, monkeypatch):
    monkeypatch.setattr(ca, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(
        ca, "search_events",
        lambda token, q, d, cal: [{"event_id": "e1", "title": "Karate", "calendar_id": "primary",
                                   "date": d, "time": "10:00 AM", "location": ""}],
    )
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--search", "--title", "Karate", "--date", "2026-03-22",
    ])
    ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "matches" in out
    assert len(out["matches"]) == 1
    assert out["matches"][0]["event_id"] == "e1"


def test_main_search_requires_title_and_date(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["calendar_add.py", "--search", "--title", "X"])
    with pytest.raises(SystemExit):
        ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "--search requires" in out["error"]


def test_main_emits_friendly_error_when_scope_missing(capsys, monkeypatch):
    creds = MagicMock(token="abc", scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(ca, "load_google_credentials", lambda a: creds)
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--title", "T", "--date", "2026-03-22",
    ])
    with pytest.raises(SystemExit):
        ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "calendar" in out["error"]
    assert "re-link" in out["error"].lower()


def test_main_emits_error_when_gogcli_returns_no_event(capsys, monkeypatch):
    monkeypatch.setattr(ca, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: {})  # missing 'event'
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--title", "T", "--date", "2026-03-22",
    ])
    with pytest.raises(SystemExit):
        ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "no event" in out["error"]


def test_main_emits_error_when_binary_missing(capsys, monkeypatch):
    monkeypatch.setattr(ca, "get_access_token", lambda a: "tok")

    def boom(*a, **kw):
        raise RuntimeError(f"gogcli binary '{gogcli.GOG_BIN}' not found. Install: brew install gogcli")

    monkeypatch.setattr(gogcli, "run", boom)
    monkeypatch.setattr(sys, "argv", [
        "calendar_add.py", "--title", "T", "--date", "2026-03-22",
    ])
    with pytest.raises(SystemExit):
        ca.main()
    out = json.loads(capsys.readouterr().out)
    assert "brew install gogcli" in out["error"]

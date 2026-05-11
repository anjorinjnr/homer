"""
Tests for calendar_fetch.py — gogcli wrapper for Google Calendar.

Mocks subprocess.run + load_google_credentials so no real binary or token
is required.
"""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import calendar_fetch as cf
import gogcli


@pytest.fixture(autouse=True)
def _assume_google_connected(monkeypatch):
    """All main()-level tests want the post-token-check path; the SKIP
    gate has its own dedicated test."""
    monkeypatch.setattr(cf, "has_google_token", lambda *a, **kw: True)


# ── normalize_event() ─────────────────────────────────────────────────────────


def test_normalize_timed_event_localized():
    raw = {
        "id": "evt1",
        "summary": "Standup",
        "location": "Zoom",
        "description": "daily sync",
        "start": {"dateTime": "2026-05-04T15:30:00-04:00", "timeZone": "America/New_York"},
        "CalendarID": "cal-id-1",
    }
    out = cf.normalize_event(raw, {"cal-id-1": "Work"})
    assert out["title"] == "Standup"
    assert out["date"] == "2026-05-04"
    assert out["time"] == "3:30 PM"
    assert out["is_all_day"] is False
    assert out["location"] == "Zoom"
    assert out["description"] == "daily sync"
    assert out["calendar"] == "Work"
    assert out["calendar_id"] == "cal-id-1"
    assert out["event_id"] == "evt1"


def test_normalize_all_day_event():
    raw = {
        "id": "evt2",
        "summary": "School holiday",
        "start": {"date": "2026-05-10"},
        "CalendarID": "cal-id-2",
    }
    out = cf.normalize_event(raw, {"cal-id-2": "Family"})
    assert out["date"] == "2026-05-10"
    assert out["time"] == "all-day"
    assert out["is_all_day"] is True


def test_normalize_missing_title_falls_back():
    raw = {"id": "x", "start": {"date": "2026-05-10"}, "CalendarID": "x"}
    out = cf.normalize_event(raw, {})
    assert out["title"] == "(no title)"


def test_normalize_unknown_calendar_id_falls_back_to_id():
    raw = {"summary": "x", "start": {"date": "2026-05-10"}, "CalendarID": "stranger"}
    out = cf.normalize_event(raw, {"known": "Known Cal"})
    assert out["calendar"] == "stranger"


def test_normalize_truncates_long_description():
    raw = {
        "summary": "x",
        "description": "y" * 1000,
        "start": {"date": "2026-05-10"},
        "CalendarID": "c",
    }
    out = cf.normalize_event(raw, {})
    assert len(out["description"]) <= 300


# ── list_calendars() ──────────────────────────────────────────────────────────


def test_list_calendars_skips_noisy(monkeypatch):
    payload = {"calendars": [
        {"id": "work@x", "summary": "Work"},
        {"id": "addressbook#contacts@group.v.calendar.google.com", "summary": "Birthdays"},
        {"id": "en.usa#holiday@group.v.calendar.google.com", "summary": "Holidays in United States"},
        {"id": "family@x", "summary": "Family"},
    ]}
    monkeypatch.setattr(gogcli, "run", lambda token, *args: payload)
    cals = cf.list_calendars("tok")
    summaries = [c["summary"] for c in cals]
    assert summaries == ["Work", "Family"]


def test_list_calendars_falls_back_to_id_when_summary_missing(monkeypatch):
    payload = {"calendars": [{"id": "x@y", "summary": ""}]}
    monkeypatch.setattr(gogcli, "run", lambda token, *args: payload)
    cals = cf.list_calendars("tok")
    assert cals[0]["summary"] == "x@y"


# ── fetch_events() ────────────────────────────────────────────────────────────


def test_fetch_events_argv(monkeypatch):
    captured = {}

    def fake_run_gog(token, *args):
        captured["token"] = token
        captured["args"] = args
        return {"events": []}

    monkeypatch.setattr(gogcli, "run", fake_run_gog)
    cals = [{"id": "a@x", "summary": "A"}, {"id": "b@x", "summary": "B"}]
    cf.fetch_events("tok", days=7, calendars=cals)
    assert captured["token"] == "tok"
    assert "calendar" in captured["args"]
    assert "events" in captured["args"]
    assert "--calendars=a@x,b@x" in captured["args"]
    assert "--days=7" in captured["args"]
    assert "--all-pages" in captured["args"]
    assert "--max=50" in captured["args"]


def test_fetch_events_empty_calendars_skips_subprocess(monkeypatch):
    """No calendars → don't even invoke gogcli."""
    called = []
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: called.append(a) or {})
    assert cf.fetch_events("tok", days=7, calendars=[]) == []
    assert called == []


def test_fetch_events_resolves_calendar_name(monkeypatch):
    payload = {"events": [
        {"summary": "x", "start": {"date": "2026-05-04"}, "CalendarID": "work@x"},
    ]}
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: payload)
    cals = [{"id": "work@x", "summary": "Work"}]
    events = cf.fetch_events("tok", 7, cals)
    assert events[0]["calendar"] == "Work"


# ── tool-specific mocks ────────────────────────────────────────────────────────


def _mock_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ── get_access_token() ────────────────────────────────────────────────────────


def test_get_access_token_requires_calendar_scope(monkeypatch):
    fake_creds = MagicMock(token="abc", scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(cf, "load_google_credentials", lambda a: fake_creds)
    with pytest.raises(PermissionError, match="calendar"):
        cf.get_access_token("primary")


def test_get_access_token_passes_when_scope_present(monkeypatch):
    fake_creds = MagicMock(token="abc", scopes=[cf.CALENDAR_SCOPE])
    monkeypatch.setattr(cf, "load_google_credentials", lambda a: fake_creds)
    assert cf.get_access_token("primary") == "abc"


# ── dedupe_and_sort() ─────────────────────────────────────────────────────────


def test_dedupe_collapses_same_event_across_calendars():
    events = [
        {"title": "Team sync", "date": "2026-05-04", "time": "10:00 AM", "is_all_day": False},
        {"title": "Team sync", "date": "2026-05-04", "time": "10:00 AM", "is_all_day": False},
        {"title": "Other", "date": "2026-05-04", "time": "10:00 AM", "is_all_day": False},
    ]
    out = cf.dedupe_and_sort(events)
    assert len(out) == 2


def test_sort_orders_by_date_then_time():
    events = [
        {"title": "B", "date": "2026-05-05", "time": "9:00 AM", "is_all_day": False},
        {"title": "A", "date": "2026-05-04", "time": "11:00 PM", "is_all_day": False},
        {"title": "C", "date": "2026-05-04", "time": "all-day", "is_all_day": True},
    ]
    out = cf.dedupe_and_sort(events)
    # All-day events sort before timed events on the same day (key uses "00:00")
    assert [e["title"] for e in out] == ["C", "A", "B"]


# ── split_today_vs_week (existing logic preserved) ────────────────────────────


class TestCalendarEventSplitting:
    def _make_event(self, date_str, time_str="all-day", is_all_day=True):
        return {
            "title": "Test Event", "date": date_str, "time": time_str,
            "is_all_day": is_all_day, "location": "", "description": "", "calendar": "test",
        }

    def test_today_classified_correctly(self):
        today = date.today().isoformat()
        ev = [self._make_event(today)]
        t, w = cf.split_today_vs_week(ev, today)
        assert len(t) == 1 and len(w) == 0

    def test_future_event_in_week(self):
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        ev = [self._make_event(tomorrow)]
        t, w = cf.split_today_vs_week(ev, today)
        assert len(t) == 0 and len(w) == 1

    def test_mixed_split(self):
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        ev = [self._make_event(today), self._make_event(tomorrow), self._make_event(tomorrow)]
        t, w = cf.split_today_vs_week(ev, today)
        assert len(t) == 1 and len(w) == 2


# ── main() ────────────────────────────────────────────────────────────────────


def test_main_emits_error_when_scope_missing(capsys, monkeypatch):
    fake_creds = MagicMock(token="abc", scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(cf, "load_google_credentials", lambda a: fake_creds)
    monkeypatch.setattr(sys, "argv", ["calendar_fetch.py"])
    with pytest.raises(SystemExit) as exc:
        cf.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "calendar" in out["error"]
    assert "re-link" in out["error"].lower()


def test_main_emits_error_when_gogcli_fails(capsys, monkeypatch):
    monkeypatch.setattr(cf, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(cf, "list_calendars", lambda *a: [{"id": "x", "summary": "X"}])

    def boom(token, days, calendars):
        raise RuntimeError("gogcli failed (exit 2): boom")

    monkeypatch.setattr(cf, "fetch_events", boom)
    monkeypatch.setattr(sys, "argv", ["calendar_fetch.py"])
    with pytest.raises(SystemExit) as exc:
        cf.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "boom" in out["error"]


def test_main_emits_error_when_binary_missing(capsys, monkeypatch):
    monkeypatch.setattr(cf, "get_access_token", lambda a: "tok")
    def boom(*a, **kw):
        raise RuntimeError(f"gogcli binary '{gogcli.GOG_BIN}' not found. Install: brew install gogcli")
    monkeypatch.setattr(cf, "list_calendars", boom)
    monkeypatch.setattr(sys, "argv", ["calendar_fetch.py"])
    with pytest.raises(SystemExit) as exc:
        cf.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "brew install gogcli" in out["error"]


def test_main_happy_path_json_output(capsys, monkeypatch):
    monkeypatch.setattr(cf, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(cf, "list_calendars", lambda *a: [{"id": "x@y", "summary": "Cal"}])

    def fake_fetch(token, days, calendars):
        return [{
            "title": "Standup", "date": "2099-01-01", "time": "9:00 AM",
            "is_all_day": False, "location": "", "description": "",
            "calendar": "Cal", "event_id": "e1", "calendar_id": "x@y",
        }]

    monkeypatch.setattr(cf, "fetch_events", fake_fetch)
    monkeypatch.setattr(sys, "argv", ["calendar_fetch.py"])
    cf.main()
    out = json.loads(capsys.readouterr().out)
    assert "today" in out
    assert "today_events" in out and "week_events" in out
    # Future-dated event lands in week_events.
    assert len(out["week_events"]) == 1
    assert out["week_events"][0]["title"] == "Standup"


def test_main_skips_when_google_not_connected(capsys, monkeypatch):
    """No token → print SKIP marker and bail before any token / gogcli work.
    Mirrors morning_briefing's early SKIP; same shape as plaid_balance_check."""
    monkeypatch.setattr(cf, "has_google_token", lambda *a, **kw: False)

    def _explode(*a, **kw):
        raise AssertionError("must not be called when Google is not connected")

    monkeypatch.setattr(cf, "get_access_token", _explode)
    monkeypatch.setattr(cf, "list_calendars", _explode)
    monkeypatch.setattr(cf, "fetch_events", _explode)
    monkeypatch.setattr(sys, "argv", ["calendar_fetch.py"])

    cf.main()
    out = capsys.readouterr().out
    assert out.startswith("SKIP:")
    assert "Google not connected" in out

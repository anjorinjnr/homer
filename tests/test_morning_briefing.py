"""
Tests for morning_briefing.py — daily briefing data gathering.

All tests use only pure logic functions — no real API calls, no credentials needed.
"""

import json
import sys
from datetime import date, datetime as _real_datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import morning_briefing as mb


# ── run_tool() ───────────────────────────────────────────────────────────────

class TestRunTool:
    def test_returns_none_on_missing_script(self, monkeypatch):
        monkeypatch.setattr(mb, "TOOLS_DIR", Path("/nonexistent"))
        monkeypatch.setattr(mb, "VENV_PYTHON", Path("/nonexistent/python"))
        result = mb.run_tool("no_such_script.py")
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path, monkeypatch):
        script = tmp_path / "bad.py"
        script.write_text('print("not json")')
        monkeypatch.setattr(mb, "TOOLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "VENV_PYTHON", sys.executable)
        result = mb.run_tool("bad.py")
        assert result is None

    def test_parses_valid_json_dict(self, tmp_path, monkeypatch):
        script = tmp_path / "good.py"
        script.write_text('import json; print(json.dumps({"key": "value"}))')
        monkeypatch.setattr(mb, "TOOLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "VENV_PYTHON", sys.executable)
        result = mb.run_tool("good.py")
        assert result == {"key": "value"}

    def test_parses_valid_json_list(self, tmp_path, monkeypatch):
        script = tmp_path / "list.py"
        script.write_text('import json; print(json.dumps([{"a": 1}]))')
        monkeypatch.setattr(mb, "TOOLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "VENV_PYTHON", sys.executable)
        result = mb.run_tool("list.py")
        assert result == [{"a": 1}]

    def test_passes_extra_args(self, tmp_path, monkeypatch):
        script = tmp_path / "args.py"
        script.write_text('import sys, json; print(json.dumps({"args": sys.argv[1:]}))')
        monkeypatch.setattr(mb, "TOOLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "VENV_PYTHON", sys.executable)
        result = mb.run_tool("args.py", ["--foo", "bar"])
        assert result == {"args": ["--foo", "bar"]}

    def test_returns_none_on_nonzero_exit(self, tmp_path, monkeypatch):
        script = tmp_path / "fail.py"
        script.write_text('import sys; sys.exit(1)')
        monkeypatch.setattr(mb, "TOOLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "VENV_PYTHON", sys.executable)
        result = mb.run_tool("fail.py")
        assert result is None

    def test_returns_none_on_empty_stdout(self, tmp_path, monkeypatch):
        script = tmp_path / "empty.py"
        script.write_text('')
        monkeypatch.setattr(mb, "TOOLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "VENV_PYTHON", sys.executable)
        result = mb.run_tool("empty.py")
        assert result is None


# ── gather_briefing() via capsys ─────────────────────────────────────────────

SAMPLE_CALENDAR = {"today_events": [{"title": "Dentist"}], "week_events": [{"title": "Meeting"}]}
SAMPLE_EMAILS = [
    {
        "id": "ai_abc12345",
        "description": "Pay invoice",
        "source": "email",
        "source_ref": {
            "subject": "Invoice #123",
            "sender": "billing@vendor.com",
            "account": "primary",
        },
        "urgency": "today",
        "due_at": "",
        "status": "open",
        "snoozed_until": None,
        "created_at": "2026-04-09T10:00:00+00:00",
        "completed_at": None,
    },
]
SAMPLE_TASKS = [
    {"description": "Gmail scan", "type": "system", "schedule": "2026-04-10 09:00", "recipients": "primary:whatsapp"},
    {"description": "Generate Kemi's weekly math plan", "type": "agentic", "schedule": "2026-04-10 09:00", "recipients": "primary:whatsapp"},
    {"description": "Call dentist", "type": "", "schedule": "2026-04-10 14:00", "recipients": "primary:whatsapp"},
    {"description": "Pick up groceries", "type": "", "schedule": "2026-04-10", "recipients": "primary:whatsapp,sam:whatsapp"},
]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect user registry + motivation state to tmp so tests don't touch the repo."""
    import manage_users
    users = tmp_path / "users.yaml"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(manage_users, "USERS_FILE", users)
    monkeypatch.setattr(manage_users, "_rebuild_context", lambda: None)
    monkeypatch.setattr(mb, "STATE_DIR", state_dir)
    monkeypatch.setattr(mb, "MOTIVATIONS_FILE", state_dir / "recent_motivations.txt")
    return tmp_path


class TestMainOutput:
    """Test the default (no-arg) CLI path by mocking run_tool + argv."""

    def _run_main(self, monkeypatch, capsys, calendar_data=None,
                  email_data=None, tasks_data=None, google_connected=True):
        # Freeze "today" at 2026-04-09 so SAMPLE_TASKS scheduled 2026-04-10 are
        # exactly 1 day out (within window, after the today-exclusion rule).
        monkeypatch.setattr(mb, "datetime", _FrozenDatetimeApr9)
        # Default: assume Google is connected so existing tests exercise the
        # full briefing path. Tests that exercise the SKIP path override this.
        monkeypatch.setattr(mb, "has_google_token", lambda *a, **kw: google_connected)
        # gather_briefing now fans out across discovered accounts. The default
        # set is the primary account so existing tests keep their single-source
        # shape; multi-account fan-out has its own dedicated tests.
        monkeypatch.setattr(mb.accounts, "list_valid_accounts", lambda: ["primary"])
        def mock_run_tool(script, extra_args=None):
            if "calendar_fetch" in script:
                return calendar_data
            elif "action_items" in script:
                return email_data
            elif "tasks_update" in script:
                return tasks_data
            return None
        monkeypatch.setattr(mb, "run_tool", mock_run_tool)
        monkeypatch.setattr(sys, "argv", ["morning_briefing.py"])
        mb.main()
        out = capsys.readouterr().out
        # SKIP-path: return raw string (caller asserts on the literal text).
        # Briefing-path: parse the JSON object (existing-tests contract).
        if out.startswith("SKIP:"):
            return out
        return json.loads(out)

    def test_output_with_calendar_events(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys, calendar_data=SAMPLE_CALENDAR)
        assert result["type"] == "morning_briefing"
        # Each event carries its source account so the LLM can label per-source
        # in multi-account households. Single-account still gets the label
        # (cheap; downstream may ignore when only one account exists).
        assert result["today_events"] == [{"title": "Dentist", "account": "primary"}]
        assert result["week_events"] == [{"title": "Meeting", "account": "primary"}]
        assert "date" in result
        assert "calendar_error" not in result

    def test_empty_calendar(self, monkeypatch, capsys):
        cal = {"today_events": [], "week_events": []}
        result = self._run_main(monkeypatch, capsys, calendar_data=cal)
        assert result["today_events"] == []
        assert result["week_events"] == []
        assert "calendar_error" not in result

    def test_calendar_failure_shows_error(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys)
        assert result["today_events"] == []
        assert result["week_events"] == []
        assert result["calendar_error"] == "Could not fetch calendar events"

    def test_skip_when_google_not_connected(self, monkeypatch, capsys):
        """Fresh tenant who hasn't linked Google should not get a daily empty
        briefing. The SKIP marker tells the agent to suppress the message —
        same shape as plaid_balance_check / budget_check.

        The SKIP text spells out Gmail AND Calendar explicitly because the
        morning brief pulls both — earlier wording said only "Google not
        connected" and the agent paraphrased that as "Calendar isn't
        connected", losing Gmail from the user-visible explanation."""
        out = self._run_main(monkeypatch, capsys, google_connected=False)
        assert out.startswith("SKIP:")
        assert "Gmail" in out and "Calendar" in out

    def test_skip_path_does_not_spawn_subprocesses(self, monkeypatch, capsys):
        """The whole point of the early SKIP is avoiding the subprocess spawn /
        HTTP retry / log noise. Assert run_tool is never called when Google is
        absent — if someone re-orders the SKIP check below the gather, this
        test catches it."""
        monkeypatch.setattr(mb, "datetime", _FrozenDatetimeApr9)
        monkeypatch.setattr(mb, "has_google_token", lambda *a, **kw: False)
        calls = []
        monkeypatch.setattr(mb, "run_tool",
                            lambda script, extra_args=None: calls.append(script))
        monkeypatch.setattr(sys, "argv", ["morning_briefing.py"])
        mb.main()
        assert calls == []

    def test_output_is_valid_json(self, monkeypatch, capsys):
        cal = {"today_events": [], "week_events": []}
        result = self._run_main(monkeypatch, capsys, calendar_data=cal)
        assert isinstance(result, dict)

    # ── Action items ─────────────────────────────────────────────────────

    def test_action_items_populated(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                email_data=SAMPLE_EMAILS)
        assert len(result["action_items"]) == 1
        assert result["action_items"][0]["subject"] == "Invoice #123"

    def test_action_items_empty_on_failure(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                email_data=None)
        assert result["action_items"] == []

    def test_action_items_empty_on_non_list(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                email_data={"status": "skipped"})
        assert result["action_items"] == []

    # ── Reminders ────────────────────────────────────────────────────────

    def test_reminders_exclude_system_and_agentic(self, monkeypatch, capsys):
        """True user reminders only — system + agentic tasks filtered out."""
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                tasks_data=SAMPLE_TASKS)
        descriptions = [r["description"] for r in result["reminders"]]
        assert descriptions == ["Call dentist", "Pick up groceries"]
        assert "Gmail scan" not in descriptions
        assert "Generate Kemi's weekly math plan" not in descriptions

    def test_reminders_empty_on_failure(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                tasks_data=None)
        assert result["reminders"] == []

    def test_reminders_empty_when_all_system(self, monkeypatch, capsys):
        system_only = [{"description": "Gmail scan", "type": "system", "schedule": "2026-04-09"}]
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                tasks_data=system_only)
        assert result["reminders"] == []

    def test_reminders_empty_when_all_agentic(self, monkeypatch, capsys):
        agentic_only = [{"description": "Weekly math plan", "type": "agentic", "schedule": "2026-04-09"}]
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                tasks_data=agentic_only)
        assert result["reminders"] == []

    # ── Users + briefing_style ───────────────────────────────────────────

    def test_users_absent_when_no_users_file(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys, calendar_data=SAMPLE_CALENDAR)
        assert result["users"] == []

    def test_users_surface_briefing_style(self, isolated_state, monkeypatch, capsys):
        (isolated_state / "users.yaml").write_text(
            "users:\n"
            "  - name: Alex\n"
            "    role: admin\n"
            "    briefing_style: dry, no emoji\n"
            "  - name: Sam\n"
            "    role: member\n"
        )
        result = self._run_main(monkeypatch, capsys, calendar_data=SAMPLE_CALENDAR)
        assert result["users"] == [
            {"name": "Alex", "briefing_style": "dry, no emoji"},
            {"name": "Sam"},
        ]

    # ── Recent motivations ───────────────────────────────────────────────

    def test_recent_motivations_empty_by_default(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys, calendar_data=SAMPLE_CALENDAR)
        assert result["recent_motivations"] == []

    def test_recent_motivations_reads_state(self, isolated_state, monkeypatch, capsys):
        mb.MOTIVATIONS_FILE.write_text("line one\nline two\nline three\n")
        result = self._run_main(monkeypatch, capsys, calendar_data=SAMPLE_CALENDAR)
        assert result["recent_motivations"] == ["line one", "line two", "line three"]

    def test_recent_motivations_capped_at_keep(self, isolated_state, monkeypatch, capsys):
        mb.MOTIVATIONS_FILE.write_text(
            "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
        )
        result = self._run_main(monkeypatch, capsys, calendar_data=SAMPLE_CALENDAR)
        assert len(result["recent_motivations"]) == mb.MOTIVATIONS_KEEP
        assert result["recent_motivations"][0] == "line 4"
        assert result["recent_motivations"][-1] == "line 10"

    # ── Full briefing ────────────────────────────────────────────────────

    def test_full_briefing(self, monkeypatch, capsys):
        result = self._run_main(monkeypatch, capsys,
                                calendar_data=SAMPLE_CALENDAR,
                                email_data=SAMPLE_EMAILS,
                                tasks_data=SAMPLE_TASKS)
        assert result["type"] == "morning_briefing"
        assert len(result["today_events"]) == 1
        assert len(result["action_items"]) == 1
        assert len(result["reminders"]) == 2
        assert result["users"] == []
        assert result["recent_motivations"] == []
        assert "calendar_error" not in result


# ── Reminder filtering — regression data from 2026-04-20 bug report ──────────
#
# These tasks were incorrectly surfaced in the morning briefing before the fix.
# The shapes below are faithful to what tasks_update.py --list returned on the
# VPS that day.

class _FrozenDatetime(_real_datetime):
    """Substitute that freezes datetime.now() at 2026-04-20 07:00 ET."""
    @classmethod
    def now(cls, tz=None):
        base = _real_datetime(2026, 4, 20, 7, 0, 0)
        return base.replace(tzinfo=tz) if tz is not None else base


class _FrozenDatetimeApr9(_real_datetime):
    """Substitute that freezes datetime.now() at 2026-04-09 07:00 ET."""
    @classmethod
    def now(cls, tz=None):
        base = _real_datetime(2026, 4, 9, 7, 0, 0)
        return base.replace(tzinfo=tz) if tz is not None else base


# Tasks that should be EXCLUDED from the morning briefing
_AGENTIC_NO_TYPE = {
    "description": "Update Kemi's daily math reminder",
    "type": "",  # missing Type: agentic — the actual bug
    "schedule": "2026-04-20 09:00",
    "goal": "Update the recurring daily math reminder for Kemi based on the latest plan.",
    "recipients": "primary:whatsapp",
}
_AGENTIC_TYPED = {
    "description": "Generate Kemi's weekly math plan",
    "type": "agentic",
    "schedule": "2026-04-20 09:00",
    "goal": "Generate a weekly math practice plan for Kemi.",
    "recipients": "primary:whatsapp",
}
_AGENTIC_MONTHLY_REPORT = {
    "description": "Kemi's monthly math report",
    "type": "agentic",
    "schedule": "2026-04-20 09:00",
    "goal": "Compile and send Kemi's monthly math progress report.",
    "recipients": "primary:whatsapp",
}
_FAR_FUTURE_9_DAYS = {
    "description": "Upload Chase CSV to family docs",
    "type": "",
    "schedule": "2026-04-29",      # 9 days out
    "recipients": "primary:whatsapp",
}
_FAR_FUTURE_MONTHS = {
    "description": "Replace upstairs HVAC filter",
    "type": "",
    "schedule": "2026-11-01",      # ~6 months out
    "recipients": "primary:whatsapp",
}

# Tasks that should be EXCLUDED — reminder fires at its schedule today, no need
# to surface it in the brief (heartbeat will deliver it as its own message).
_REMINDER_TODAY = {
    "description": "Call dentist to schedule Kemi's checkup",
    "type": "",
    "schedule": "2026-04-20 10:00",
    "recipients": "primary:whatsapp",
}

# Tasks that should be INCLUDED
_REMINDER_4_DAYS = {
    "description": "Pick up dry cleaning",
    "type": "",
    "schedule": "2026-04-24",       # 4 days out — within window
    "recipients": "primary:whatsapp",
}
_REMINDER_5_DAYS = {
    "description": "Return library books",
    "type": "",
    "schedule": "2026-04-25",       # exactly 5 days out — boundary, still included
    "recipients": "primary:whatsapp",
}
_REMINDER_6_DAYS = {
    "description": "Soccer cleats pickup",
    "type": "",
    "schedule": "2026-04-26",       # 6 days out — just outside window, excluded
    "recipients": "primary:whatsapp",
}

REAL_DATA_TASKS = [
    _AGENTIC_NO_TYPE,
    _AGENTIC_TYPED,
    _AGENTIC_MONTHLY_REPORT,
    _FAR_FUTURE_9_DAYS,
    _FAR_FUTURE_MONTHS,
    _REMINDER_TODAY,         # excluded — fires at its own schedule today
    _REMINDER_4_DAYS,        # included — needs early notice
]


class TestReminderFiltering:
    """Regression tests for the 2026-04-20 bug: agentic + far-future tasks leaked
    into the morning briefing. All tests freeze datetime.now() at 2026-04-20."""

    def _run(self, monkeypatch, capsys, tasks):
        monkeypatch.setattr(mb, "datetime", _FrozenDatetime)
        monkeypatch.setattr(mb, "has_google_token", lambda *a, **kw: True)

        def mock_run_tool(script, extra_args=None):
            if "calendar_fetch" in script:
                return {"today_events": [], "week_events": []}
            if "action_items" in script:
                return []
            if "tasks_update" in script:
                return tasks
            return None

        monkeypatch.setattr(mb, "run_tool", mock_run_tool)
        monkeypatch.setattr(sys, "argv", ["morning_briefing.py"])
        mb.main()
        return json.loads(capsys.readouterr().out)

    def test_agentic_with_goal_but_no_type_excluded(self, monkeypatch, capsys):
        """Core regression: goal-field task with type='' must be filtered out."""
        result = self._run(monkeypatch, capsys, [_AGENTIC_NO_TYPE, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Update Kemi's daily math reminder" not in descs
        assert "Pick up dry cleaning" in descs

    def test_typed_agentic_excluded(self, monkeypatch, capsys):
        result = self._run(monkeypatch, capsys, [_AGENTIC_TYPED, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Generate Kemi's weekly math plan" not in descs

    def test_monthly_report_agentic_excluded(self, monkeypatch, capsys):
        result = self._run(monkeypatch, capsys, [_AGENTIC_MONTHLY_REPORT, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Kemi's monthly math report" not in descs

    def test_chase_csv_too_far_out_excluded(self, monkeypatch, capsys):
        """Chase CSV 9 days out must not appear."""
        result = self._run(monkeypatch, capsys, [_FAR_FUTURE_9_DAYS, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Upload Chase CSV to family docs" not in descs

    def test_hvac_filter_months_away_excluded(self, monkeypatch, capsys):
        """HVAC filter months away must not appear."""
        result = self._run(monkeypatch, capsys, [_FAR_FUTURE_MONTHS, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Replace upstairs HVAC filter" not in descs

    def test_reminder_today_excluded(self, monkeypatch, capsys):
        """Reminders scheduled for today must not appear — heartbeat fires them
        at their own schedule, so duplicating in the brief is noise."""
        result = self._run(monkeypatch, capsys, [_REMINDER_TODAY, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Call dentist to schedule Kemi's checkup" not in descs
        assert "Pick up dry cleaning" in descs

    def test_reminder_past_due_excluded(self, monkeypatch, capsys):
        """Past-dated reminders that haven't ticked also fire on next heartbeat;
        suppress them from the brief for the same reason."""
        past = {
            "description": "Past due task",
            "type": "",
            "schedule": "2026-04-15 10:00",  # 5 days before frozen 'today'
            "recipients": "primary:whatsapp",
        }
        result = self._run(monkeypatch, capsys, [past, _REMINDER_4_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Past due task" not in descs

    def test_boundary_5_days_included(self, monkeypatch, capsys):
        """Task exactly 5 days out is still within window and must appear."""
        result = self._run(monkeypatch, capsys, [_REMINDER_5_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Return library books" in descs

    def test_boundary_6_days_excluded(self, monkeypatch, capsys):
        """Task 6 days out is just outside window and must not appear."""
        result = self._run(monkeypatch, capsys, [_REMINDER_6_DAYS])
        descs = [r["description"] for r in result["reminders"]]
        assert "Soccer cleats pickup" not in descs

    def test_no_schedule_field_always_included(self, monkeypatch, capsys):
        """A reminder with no schedule is always relevant — must pass through."""
        no_schedule = {"description": "Buy birthday card", "type": "", "recipients": "primary:whatsapp"}
        result = self._run(monkeypatch, capsys, [no_schedule])
        descs = [r["description"] for r in result["reminders"]]
        assert "Buy birthday card" in descs

    def test_full_real_data_only_correct_reminders_surface(self, monkeypatch, capsys):
        """End-to-end regression: agentic tasks, far-future reminders, and
        today-scheduled reminders are all excluded — only the dry-cleaning
        reminder (4 days out, needs early notice) surfaces."""
        result = self._run(monkeypatch, capsys, REAL_DATA_TASKS)
        descs = [r["description"] for r in result["reminders"]]

        # None of these should appear
        assert "Update Kemi's daily math reminder" not in descs
        assert "Generate Kemi's weekly math plan" not in descs
        assert "Kemi's monthly math report" not in descs
        assert "Upload Chase CSV to family docs" not in descs
        assert "Replace upstairs HVAC filter" not in descs
        # Today-scheduled reminder fires on its own — not in brief
        assert "Call dentist to schedule Kemi's checkup" not in descs

        # Only the multi-day-out reminder surfaces
        assert "Pick up dry cleaning" in descs
        assert len(descs) == 1


# ── Friendly display formatters ──────────────────────────────────────────────

class TestFriendlyTime:
    @pytest.mark.parametrize("raw,expected", [
        ("14:00", "2pm"),
        ("09:00", "9am"),
        ("00:30", "12:30am"),
        ("12:00", "12pm"),
        ("12:30", "12:30pm"),
        ("17:00", "5pm"),
        ("23:45", "11:45pm"),
        ("2:00 PM", "2pm"),
        ("9:30 AM", "9:30am"),
        ("12:00 AM", "12am"),
        ("12:00 PM", "12pm"),
        ("all-day", "all-day"),
        ("", ""),
        ("bogus", "bogus"),
    ])
    def test_formats(self, raw, expected):
        assert mb._friendly_time_from_str(raw) == expected


class TestRelativeDate:
    today = date(2026, 4, 20)  # Monday

    @pytest.mark.parametrize("target,expected", [
        (date(2026, 4, 20), "Today"),
        (date(2026, 4, 21), "Tomorrow"),
        (date(2026, 4, 22), "Wed Apr 22"),
        (date(2026, 4, 23), "Thu Apr 23"),
        (date(2026, 4, 26), "Sun Apr 26"),  # 6 days out — still weekday form
        (date(2026, 4, 27), "Apr 27"),      # 7 days out — plain month+day
        (date(2026, 5, 15), "May 15"),
    ])
    def test_formats(self, target, expected):
        assert mb._relative_date(target, self.today) == expected


class TestEnrichReminder:
    today = date(2026, 4, 20)

    def test_datetime_today(self):
        out = mb._enrich_reminder({"description": "HVAC", "schedule": "2026-04-20 09:00"}, self.today)
        assert out["display_when"] == "9am Today"

    def test_datetime_tomorrow(self):
        out = mb._enrich_reminder({"description": "Call", "schedule": "2026-04-21 15:30"}, self.today)
        assert out["display_when"] == "3:30pm Tomorrow"

    def test_datetime_weekday(self):
        out = mb._enrich_reminder({"description": "Appt", "schedule": "2026-04-24 15:00"}, self.today)
        assert out["display_when"] == "3pm Fri Apr 24"

    def test_date_only(self):
        out = mb._enrich_reminder({"description": "Birthday", "schedule": "2026-05-01"}, self.today)
        assert out["display_when"] == "May 1"

    def test_preserves_other_fields(self):
        out = mb._enrich_reminder(
            {"description": "X", "schedule": "2026-04-20 09:00", "recipients": "a:whatsapp"},
            self.today,
        )
        assert out["description"] == "X"
        assert out["recipients"] == "a:whatsapp"
        assert out["schedule"] == "2026-04-20 09:00"  # raw preserved

    def test_no_display_when_if_schedule_malformed(self):
        out = mb._enrich_reminder({"description": "X", "schedule": "soon"}, self.today)
        assert "display_when" not in out


class TestEnrichEvent:
    today = date(2026, 4, 20)

    def test_today_event(self):
        out = mb._enrich_event({"title": "Swim", "date": "2026-04-20", "time": "2:00 PM"}, self.today)
        assert out["display_date"] == "Today"
        assert out["display_time"] == "2pm"

    def test_week_event(self):
        out = mb._enrich_event({"title": "Visit", "date": "2026-04-22", "time": "10:00 AM"}, self.today)
        assert out["display_date"] == "Wed Apr 22"
        assert out["display_time"] == "10am"

    def test_all_day_omits_display_time(self):
        out = mb._enrich_event({"title": "Holiday", "date": "2026-04-21", "time": "all-day"}, self.today)
        assert out["display_date"] == "Tomorrow"
        assert "display_time" not in out


class TestEnrichActionItem:
    @pytest.mark.parametrize("urgency,expected", [
        ("today", "today"),
        ("this_week", "this week"),
        ("low", "low priority"),
        ("custom", "custom"),  # unknown value passes through
    ])
    def test_display_urgency(self, urgency, expected):
        out = mb._enrich_action_item({"subject": "X", "urgency": urgency})
        assert out["display_urgency"] == expected

    def test_no_urgency_no_display(self):
        out = mb._enrich_action_item({"subject": "X"})
        assert "display_urgency" not in out


# ── --log-motivation ─────────────────────────────────────────────────────────

class TestLogMotivation:
    def test_appends_first_line(self, isolated_state, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["morning_briefing.py", "--log-motivation", "Have a great day!"])
        mb.main()
        out = json.loads(capsys.readouterr().out)
        assert out == {"status": "logged", "kept": 1}
        assert mb.MOTIVATIONS_FILE.read_text().splitlines() == ["Have a great day!"]

    def test_trims_to_last_seven(self, isolated_state, monkeypatch, capsys):
        mb.MOTIVATIONS_FILE.write_text(
            "\n".join(f"line {i}" for i in range(1, 8)) + "\n"
        )
        monkeypatch.setattr(sys, "argv",
                            ["morning_briefing.py", "--log-motivation", "newest"])
        mb.main()
        out = json.loads(capsys.readouterr().out)
        assert out == {"status": "logged", "kept": 7}
        lines = mb.MOTIVATIONS_FILE.read_text().splitlines()
        assert lines[0] == "line 2"
        assert lines[-1] == "newest"

    def test_rejects_empty_line(self, isolated_state, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["morning_briefing.py", "--log-motivation", "   "])
        with pytest.raises(SystemExit):
            mb.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out


# ── Multi-account fan-out ──────────────────────────────────────────────────


class TestMultiAccountFanout:
    """The brief fans out across every linked Google account whose token is
    valid, merges results, and tags each event with its source account.
    Tests stub accounts.list_valid_accounts and the calendar fetch helper
    so discovery + subprocess details stay out of the assertion surface."""

    def _run_main_with_accounts(self, monkeypatch, capsys, *,
                                account_names: list[str],
                                per_account_calendar: dict[str, dict | None],
                                cli_account: str | None = None):
        monkeypatch.setattr(mb, "datetime", _FrozenDatetimeApr9)
        monkeypatch.setattr(mb, "has_google_token", lambda *a, **kw: True)
        monkeypatch.setattr(mb.accounts, "list_valid_accounts", lambda: account_names)
        monkeypatch.setattr(
            mb,
            "_fetch_calendar_for",
            lambda name: per_account_calendar.get(name),
        )
        monkeypatch.setattr(
            mb,
            "run_tool",
            lambda script, extra_args=None: None,
        )
        argv = ["morning_briefing.py"]
        if cli_account:
            argv += ["--account", cli_account]
        monkeypatch.setattr(sys, "argv", argv)
        mb.main()
        return json.loads(capsys.readouterr().out)

    def test_events_from_each_account_merged_with_labels(self, monkeypatch, capsys):
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
            "personal": {"today_events": [{"title": "Dinner"}], "week_events": []},
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar=per_account,
        )
        labels = sorted(e["account"] for e in result["today_events"])
        assert labels == ["personal", "primary"]
        # `accounts_attempted` lists every account we tried. Successful
        # accounts = accounts_attempted - calendar_partial.
        assert result["accounts_attempted"] == ["primary", "personal"]
        assert "calendar_partial" not in result

    def test_single_account_no_attempted_marker(self, monkeypatch, capsys):
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary"],
            per_account_calendar=per_account,
        )
        # Single-account brief omits the multi-account `accounts_attempted`
        # summary (it's only useful when there's more than one).
        assert "accounts_attempted" not in result
        assert "calendar_partial" not in result

    def test_partial_failure_marks_brief_but_keeps_succeeded_accounts(self, monkeypatch, capsys):
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
            "personal": None,
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar=per_account,
        )
        assert result["calendar_partial"] == ["personal"]
        # No `calendar_error` — primary succeeded, so the brief is partial,
        # not failed.
        assert "calendar_error" not in result
        assert [e["account"] for e in result["today_events"]] == ["primary"]

    def test_all_accounts_fail_emits_calendar_error(self, monkeypatch, capsys):
        per_account = {"primary": None, "personal": None}
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar=per_account,
        )
        assert result["calendar_error"] == "Could not fetch calendar events"
        assert set(result["calendar_partial"]) == {"primary", "personal"}
        assert result["today_events"] == []

    def test_explicit_account_flag_overrides_discovery(self, monkeypatch, capsys):
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
            "personal": {"today_events": [{"title": "Dinner"}], "week_events": []},
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],  # discovery sees both
            per_account_calendar=per_account,
            cli_account="personal",                  # but CLI restricts to one
        )
        assert [e["title"] for e in result["today_events"]] == ["Dinner"]
        assert "accounts_attempted" not in result  # single-account run

    def test_explicit_account_unknown_returns_error_not_silent_fanout(self, monkeypatch, capsys):
        """A typo or stale account name on --account should produce a clear
        error message + the list of available accounts, NOT a silent
        fan-out to a nonexistent account that surfaces as a generic
        calendar_error."""
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar={},
            cli_account="bogus",
        )
        assert "error" in result
        assert "bogus" in result["error"]
        assert result["available_accounts"] == ["primary", "personal"]
        # Must NOT have fanned out — none of the regular brief keys present.
        assert "today_events" not in result
        assert "calendar_error" not in result

    def test_events_sorted_chronologically_across_accounts(self, monkeypatch, capsys):
        """Cross-account merge must sort by (date, time) so a 7pm event
        from one account doesn't render before a 9am event from another."""
        per_account = {
            "primary": {
                "today_events": [
                    {"title": "Late Dinner", "date": "2026-04-09", "time": "7:00 PM"},
                ],
                "week_events": [],
            },
            "personal": {
                "today_events": [
                    {"title": "Morning Standup", "date": "2026-04-09", "time": "9:00 AM"},
                    {"title": "Lunch", "date": "2026-04-09", "time": "12:30 PM"},
                ],
                "week_events": [],
            },
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar=per_account,
        )
        titles = [e["title"] for e in result["today_events"]]
        assert titles == ["Morning Standup", "Lunch", "Late Dinner"], (
            f"events out of chronological order: {titles}"
        )

    def test_all_day_events_sort_before_timed_events(self, monkeypatch, capsys):
        per_account = {
            "primary": {
                "today_events": [
                    {"title": "Standup", "date": "2026-04-09", "time": "9:00 AM"},
                ],
                "week_events": [],
            },
            "personal": {
                "today_events": [
                    {"title": "Holiday", "date": "2026-04-09", "time": "all-day"},
                ],
                "week_events": [],
            },
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar=per_account,
        )
        titles = [e["title"] for e in result["today_events"]]
        assert titles == ["Holiday", "Standup"]

    def test_empty_payload_account_counted_as_attempted_not_partial(self, monkeypatch, capsys):
        """An account that returns successfully but has no events today
        is *successful, just empty* — it should appear in
        accounts_attempted but NOT in calendar_partial (which is for
        fetch failures, not empty inboxes / quiet days)."""
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
            "personal": {"today_events": [], "week_events": []},
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary", "personal"],
            per_account_calendar=per_account,
        )
        assert result["accounts_attempted"] == ["primary", "personal"]
        assert "calendar_partial" not in result
        assert "calendar_error" not in result
        # Only primary's event lands; personal contributed nothing but is
        # NOT marked as failed.
        assert [e["title"] for e in result["today_events"]] == ["Standup"]

    def test_invalid_account_silently_excluded_by_discovery(self, monkeypatch, capsys):
        """An account that's linked but whose token is expired-without-refresh
        is rejected by list_valid_accounts and the brief never tries to
        fetch from it. The brief shouldn't claim it failed — it was
        never attempted in the first place."""
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
            # "personal" is in per_account_calendar but NOT in account_names —
            # discovery rejected it, so the brief never asked for it.
            "personal": {"today_events": [{"title": "Should not appear"}], "week_events": []},
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary"],  # discovery filtered "personal" out
            per_account_calendar=per_account,
        )
        # Single-account run, no fan-out summary, no partial marker.
        assert "accounts_attempted" not in result
        assert "calendar_partial" not in result
        assert [e["title"] for e in result["today_events"]] == ["Standup"]


# ── Conflict detection ────────────────────────────────────────────────────


class TestConflictDetection:
    """The brief surfaces overlapping timed events as a `conflicts` list so
    the agent can render a heads-up. Detection is mechanical (interval
    overlap on same date); semantic / delegation reasoning is downstream."""

    def _ev(self, title, start_min, end_min, *, date="2026-04-09",
            account="primary", calendar="primary", is_opaque=False,
            is_all_day=False):
        return {
            "title": title,
            "date": date,
            "time": f"{start_min // 60}:{start_min % 60:02d}",
            "end_time": f"{end_min // 60}:{end_min % 60:02d}",
            "start_minutes": start_min,
            "end_minutes": end_min,
            "is_all_day": is_all_day,
            "account": account,
            "calendar": calendar,
            "is_opaque": is_opaque,
        }

    def test_no_conflicts_when_events_dont_overlap(self):
        events = [
            self._ev("A", 9 * 60, 10 * 60),
            self._ev("B", 10 * 60, 11 * 60),  # back-to-back, no overlap
            self._ev("C", 14 * 60, 15 * 60),
        ]
        assert mb._detect_conflicts(events) == []

    def test_detects_simple_overlap(self):
        events = [
            self._ev("Standup", 9 * 60, 10 * 60),
            self._ev("Dentist", 9 * 60 + 30, 10 * 60 + 30),
        ]
        conflicts = mb._detect_conflicts(events)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["event_a"]["title"] == "Standup"
        assert c["event_b"]["title"] == "Dentist"
        assert c["overlap_start_minutes"] == 9 * 60 + 30
        assert c["overlap_end_minutes"] == 10 * 60

    def test_detects_cross_account_conflict_flag(self):
        events = [
            self._ev("Standup", 9 * 60, 10 * 60, account="work"),
            self._ev("School pickup", 9 * 60 + 30, 10 * 60 + 30, account="personal"),
        ]
        conflicts = mb._detect_conflicts(events)
        assert conflicts[0]["cross_account"] is True

    def test_same_account_conflict_not_cross(self):
        events = [
            self._ev("A", 9 * 60, 10 * 60, account="primary"),
            self._ev("B", 9 * 60 + 30, 10 * 60 + 30, account="primary"),
        ]
        conflicts = mb._detect_conflicts(events)
        assert conflicts
        assert conflicts[0]["cross_account"] is False

    def test_both_opaque_flagged(self):
        """When both sides are free/busy-only blocks, the agent has less to
        say beyond 'you're double-booked' — surface the flag so the
        rendering layer can tone down the message."""
        events = [
            self._ev("Busy", 9 * 60, 10 * 60, is_opaque=True),
            self._ev("Busy", 9 * 60 + 30, 10 * 60 + 30, is_opaque=True),
        ]
        conflicts = mb._detect_conflicts(events)
        assert conflicts[0]["both_opaque"] is True

    def test_one_opaque_one_clear_not_both_opaque(self):
        events = [
            self._ev("Busy", 9 * 60, 10 * 60, is_opaque=True),
            self._ev("Standup", 9 * 60 + 30, 10 * 60 + 30, is_opaque=False),
        ]
        conflicts = mb._detect_conflicts(events)
        assert conflicts[0]["both_opaque"] is False

    def test_all_day_event_does_not_create_conflict(self):
        """An all-day 'Spring Break' does not conflict with a timed standup;
        users routinely have both."""
        events = [
            self._ev("Spring Break", 0, 0, is_all_day=True),
            self._ev("Standup", 9 * 60, 10 * 60),
        ]
        assert mb._detect_conflicts(events) == []

    def test_different_dates_dont_conflict(self):
        events = [
            self._ev("Today event", 9 * 60, 10 * 60, date="2026-04-09"),
            self._ev("Tomorrow event", 9 * 60 + 30, 10 * 60 + 30, date="2026-04-10"),
        ]
        assert mb._detect_conflicts(events) == []

    def test_three_way_pile_up_emits_pairwise(self):
        """Three overlapping events → 3 pairs. Downstream agent decides
        how to present (probably collapses adjacent pairs into one
        'triple-booked' message)."""
        events = [
            self._ev("A", 9 * 60, 11 * 60),
            self._ev("B", 9 * 60 + 30, 10 * 60 + 30),
            self._ev("C", 10 * 60, 10 * 60 + 45),
        ]
        conflicts = mb._detect_conflicts(events)
        pairs = {(c["event_a"]["title"], c["event_b"]["title"]) for c in conflicts}
        assert pairs == {("A", "B"), ("A", "C"), ("B", "C")}

    def test_legacy_event_without_end_minutes_skipped(self):
        """Pre-end_minutes events (legacy payloads or all-day) can't be
        compared for overlap and must be skipped rather than blowing up."""
        events = [
            {"title": "Old", "date": "2026-04-09", "is_all_day": False,
             "start_minutes": 540, "end_minutes": None, "account": "primary"},
            self._ev("New", 9 * 60 + 30, 10 * 60 + 30),
        ]
        assert mb._detect_conflicts(events) == []

    def test_zero_duration_event_skipped(self):
        """An event with end == start (sometimes seen for reminders or
        misconfigured events) cannot create a conflict."""
        events = [
            self._ev("Zero", 9 * 60, 9 * 60),
            self._ev("Standup", 9 * 60, 10 * 60),
        ]
        assert mb._detect_conflicts(events) == []

    def test_brief_output_includes_conflicts_field(self, monkeypatch, capsys):
        """The brief always emits a `conflicts` list, empty by default."""
        monkeypatch.setattr(mb, "datetime", _FrozenDatetimeApr9)
        monkeypatch.setattr(mb, "has_google_token", lambda *a, **kw: True)
        monkeypatch.setattr(mb.accounts, "list_valid_accounts", lambda: ["primary"])
        monkeypatch.setattr(mb, "run_tool",
                            lambda script, extra_args=None:
                            {"today_events": [], "week_events": []} if "calendar_fetch" in script else None)
        monkeypatch.setattr(sys, "argv", ["morning_briefing.py"])
        mb.main()
        result = json.loads(capsys.readouterr().out)
        assert result["conflicts"] == []

    def test_exact_same_time_window_is_a_conflict(self):
        """Two events with identical start AND end times — the maximal
        overlap. Should be flagged."""
        events = [
            self._ev("A", 9 * 60, 10 * 60),
            self._ev("B", 9 * 60, 10 * 60),
        ]
        conflicts = mb._detect_conflicts(events)
        assert len(conflicts) == 1
        assert conflicts[0]["overlap_start_minutes"] == 9 * 60
        assert conflicts[0]["overlap_end_minutes"] == 10 * 60

    def test_full_containment_is_a_conflict(self):
        """B sits entirely inside A — the overlap window is B's full span."""
        events = [
            self._ev("Outer", 9 * 60, 11 * 60),
            self._ev("Inner", 9 * 60 + 30, 10 * 60),
        ]
        conflicts = mb._detect_conflicts(events)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["overlap_start_minutes"] == 9 * 60 + 30
        assert c["overlap_end_minutes"] == 10 * 60

    def test_one_minute_overlap_is_a_conflict(self):
        """Any positive overlap counts. The brief surfaces it; the user
        decides if it's worth caring about."""
        events = [
            self._ev("A", 9 * 60, 10 * 60 + 1),  # 09:00 - 10:01
            self._ev("B", 10 * 60, 11 * 60),     # 10:00 - 11:00
        ]
        conflicts = mb._detect_conflicts(events)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["overlap_end_minutes"] - c["overlap_start_minutes"] == 1

    def test_conflict_view_includes_location_and_event_id(self):
        """_conflict_event_view should carry location (so the agent can say
        'you're in two places') and event_id (so the agent can dedupe
        across multiple briefing renders)."""
        events = [
            {**self._ev("Standup", 9 * 60, 10 * 60),
             "location": "Zoom", "event_id": "ev-1"},
            {**self._ev("Dentist", 9 * 60 + 30, 10 * 60 + 30),
             "location": "1500 Main", "event_id": "ev-2"},
        ]
        conflicts = mb._detect_conflicts(events)
        assert conflicts[0]["event_a"]["location"] == "Zoom"
        assert conflicts[0]["event_a"]["event_id"] == "ev-1"
        assert conflicts[0]["event_b"]["location"] == "1500 Main"
        assert conflicts[0]["event_b"]["event_id"] == "ev-2"


class TestCrossAccountDedup:
    """A shared event visible to both linked accounts (e.g. a family
    calendar both adults can see) must be deduped at merge time — it's
    one event, not a cross-account conflict with itself."""

    def _shared_event(self, account, *, event_id="shared-ev-1", title="Family Dinner"):
        # Raw shape mirrors calendar_fetch's normalize_event output for an
        # event surfaced on multiple accounts via a shared calendar.
        return {
            "title": title,
            "date": "2026-04-09",
            "time": "7:00 PM",
            "end_time": "8:00 PM",
            "start_minutes": 19 * 60,
            "end_minutes": 20 * 60,
            "is_all_day": False,
            "location": "Home",
            "description": "",
            "calendar": "Family",
            "event_id": event_id,
            "calendar_id": "family-cal",
            "access_role": "reader",
            "is_opaque": False,
        }

    def test_shared_event_deduped_by_event_id(self):
        per_account = {
            "primary": {
                "today_events": [self._shared_event("primary")],
                "week_events": [],
            },
            "personal": {
                "today_events": [self._shared_event("personal")],  # same id
                "week_events": [],
            },
        }
        today_events, _, _ = mb._merge_calendar_payloads(per_account, date(2026, 4, 9))
        assert len(today_events) == 1, (
            f"shared event was not deduped; brief would emit a phantom "
            f"cross-account conflict. Got: {today_events}"
        )

    def test_shared_event_deduped_falls_back_to_shape_when_no_event_id(self):
        """If event_id is missing (older payloads, some sync edge cases),
        dedup falls back to (date, start, end, title)."""
        a = self._shared_event("primary", event_id="")
        b = self._shared_event("personal", event_id="")
        per_account = {
            "primary": {"today_events": [a], "week_events": []},
            "personal": {"today_events": [b], "week_events": []},
        }
        today_events, _, _ = mb._merge_calendar_payloads(per_account, date(2026, 4, 9))
        assert len(today_events) == 1

    def test_different_events_with_same_shape_not_deduped_when_ids_differ(self):
        """Two genuinely different events that happen to share a time slot
        AND a title (rare but possible) must NOT be deduped when their
        event_ids are distinct — that's a real conflict, not a duplicate."""
        per_account = {
            "primary": {
                "today_events": [self._shared_event("primary", event_id="ev-a")],
                "week_events": [],
            },
            "personal": {
                "today_events": [self._shared_event("personal", event_id="ev-b")],
                "week_events": [],
            },
        }
        today_events, _, _ = mb._merge_calendar_payloads(per_account, date(2026, 4, 9))
        assert len(today_events) == 2

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
    {"subject": "Invoice #123", "sender": "billing@vendor.com", "action": "Pay invoice", "urgency": "today"},
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
            elif "email_action_items" in script:
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
            if "email_action_items" in script:
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
        # The brief advertises which accounts it pulled from.
        assert result["accounts"] == ["primary", "personal"]
        assert "calendar_partial" not in result

    def test_single_account_no_partial_marker(self, monkeypatch, capsys):
        per_account = {
            "primary": {"today_events": [{"title": "Standup"}], "week_events": []},
        }
        result = self._run_main_with_accounts(
            monkeypatch, capsys,
            account_names=["primary"],
            per_account_calendar=per_account,
        )
        # Single-account brief omits the multi-account `accounts` summary
        # (it's only useful when there's more than one).
        assert "accounts" not in result
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
        assert "accounts" not in result  # single-account run

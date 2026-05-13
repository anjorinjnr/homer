"""Tests for list_reminders_due.py — today-only reminder filter."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import list_reminders_due as lrd
from tasks_update import TASK_TYPE_AGENTIC, TASK_TYPE_REMINDER, TASK_TYPE_SYSTEM


TODAY = date(2026, 5, 13)


def _t(description, schedule, **overrides):
    base = {
        "id": "t_" + description[:6],
        "description": description,
        "type": TASK_TYPE_REMINDER,
        "schedule": schedule,
        "until": "",
        "recur": "",
        "recipients": "",
        "model": "",
        "goal": "",
    }
    base.update(overrides)
    return base


class TestFilter:
    def test_today_with_time_included(self):
        out = lrd.filter_reminders([_t("Pick up Sade", "2026-05-13 15:30")], TODAY)
        assert len(out) == 1
        assert out[0]["display_when"] == "3:30pm Today"

    def test_today_all_day_included(self):
        out = lrd.filter_reminders([_t("Anniversary", "2026-05-13")], TODAY)
        assert len(out) == 1
        assert out[0]["display_when"] == "Today"

    def test_tomorrow_excluded(self):
        # Regression: previously surfaced via days_out=1..5 lookahead.
        out = lrd.filter_reminders([_t("Future", "2026-05-14 09:00")], TODAY)
        assert out == []

    def test_five_days_out_excluded(self):
        out = lrd.filter_reminders([_t("Far", "2026-05-18 09:00")], TODAY)
        assert out == []

    def test_yesterday_excluded(self):
        out = lrd.filter_reminders([_t("Past", "2026-05-12 09:00")], TODAY)
        assert out == []

    def test_system_task_excluded(self):
        out = lrd.filter_reminders([
            _t("Morning briefing", "2026-05-13 07:00", type=TASK_TYPE_SYSTEM),
        ], TODAY)
        assert out == []

    def test_agentic_task_excluded(self):
        out = lrd.filter_reminders([
            _t("Inbox sweep", "2026-05-13 09:00", type=TASK_TYPE_AGENTIC),
        ], TODAY)
        assert out == []

    def test_goal_bearing_excluded(self):
        out = lrd.filter_reminders([
            _t("Ping Sade", "2026-05-13 09:00", goal="Stay in touch weekly"),
        ], TODAY)
        assert out == []

    def test_missing_schedule_excluded(self):
        out = lrd.filter_reminders([_t("Floater", "")], TODAY)
        assert out == []

    def test_malformed_schedule_excluded(self):
        out = lrd.filter_reminders([_t("Bogus", "next Friday")], TODAY)
        assert out == []

    def test_mixed_only_today_returned(self):
        out = lrd.filter_reminders([
            _t("Today AM", "2026-05-13 09:00"),
            _t("Tomorrow", "2026-05-14 09:00"),
            _t("Today PM", "2026-05-13 17:30"),
            _t("System", "2026-05-13 07:00", type=TASK_TYPE_SYSTEM),
        ], TODAY)
        descriptions = sorted(r["description"] for r in out)
        assert descriptions == ["Today AM", "Today PM"]


class TestDisplayWhen:
    def test_morning_time(self):
        out = lrd.filter_reminders([_t("X", "2026-05-13 09:00")], TODAY)
        assert out[0]["display_when"] == "9am Today"

    def test_noon(self):
        out = lrd.filter_reminders([_t("X", "2026-05-13 12:00")], TODAY)
        assert out[0]["display_when"] == "12pm Today"

    def test_minute_precision(self):
        out = lrd.filter_reminders([_t("X", "2026-05-13 14:45")], TODAY)
        assert out[0]["display_when"] == "2:45pm Today"

    def test_midnight(self):
        out = lrd.filter_reminders([_t("X", "2026-05-13 00:00")], TODAY)
        assert out[0]["display_when"] == "12am Today"


class TestCLI:
    def test_default_uses_today(self, monkeypatch, capsys):
        from datetime import datetime
        fixed = datetime(2026, 5, 13, 8, 0, tzinfo=lrd.LOCAL_TZ)

        class FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr(lrd, "datetime", FrozenDT)
        monkeypatch.setattr(lrd, "_run_tasks_list", lambda: [
            _t("Today thing", "2026-05-13 09:00"),
            _t("Tomorrow thing", "2026-05-14 09:00"),
        ])
        monkeypatch.setattr(sys, "argv", ["list_reminders_due.py"])
        lrd.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["description"] == "Today thing"

    def test_explicit_date(self, monkeypatch, capsys):
        monkeypatch.setattr(lrd, "_run_tasks_list", lambda: [
            _t("Today thing", "2026-05-13 09:00"),
            _t("Tomorrow thing", "2026-05-14 09:00"),
        ])
        monkeypatch.setattr(sys, "argv",
                            ["list_reminders_due.py", "--date", "2026-05-14"])
        lrd.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1
        assert out[0]["description"] == "Tomorrow thing"

    def test_invalid_date_errors(self, monkeypatch, capsys):
        import pytest
        monkeypatch.setattr(sys, "argv",
                            ["list_reminders_due.py", "--date", "May 13"])
        with pytest.raises(SystemExit):
            lrd.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_tasks_list_failure_returns_empty(self, monkeypatch, capsys):
        monkeypatch.setattr(lrd, "_run_tasks_list", lambda: None)
        monkeypatch.setattr(sys, "argv",
                            ["list_reminders_due.py", "--date", "2026-05-13"])
        lrd.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert out == []

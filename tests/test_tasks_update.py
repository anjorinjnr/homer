"""Tests for tasks_update.py — focused on --tick Last-run tracking."""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

LOCAL_TZ = ZoneInfo("America/New_York")

# tasks_update.py uses a hardcoded HEARTBEAT_FILE path, so we monkey-patch it
import tools.tasks_update as tu


SAMPLE_HEARTBEAT = """\
# Heartbeat Tasks

## User Tasks

### Remind: complete Taxes
Schedule: 2027-06-10
Recur: every 1 day
Until: 2027-06-20
Added: 2027-06-08

## Completed
"""


@pytest.fixture()
def heartbeat_file(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(SAMPLE_HEARTBEAT, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


def test_tick_advances_schedule(heartbeat_file, capsys):
    tu.tick_task("taxes")
    content = heartbeat_file.read_text()
    assert "Schedule: 2027-06-11" in content
    assert "Schedule: 2027-06-10" not in content


def test_tick_writes_last_run(heartbeat_file, capsys):
    before = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    tu.tick_task("taxes")
    after = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

    content = heartbeat_file.read_text()
    m = re.search(r"Last-run:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", content)
    assert m, "Last-run field not written after --tick"
    assert before <= m.group(1) <= after


def test_tick_updates_existing_last_run(heartbeat_file, capsys):
    """A second --tick must overwrite Last-run, not duplicate it."""
    # Inject a stale Last-run
    content = heartbeat_file.read_text()
    content = content.replace(
        "Schedule: 2027-06-10",
        "Schedule: 2027-06-10\nLast-run: 2020-01-01 09:00",
    )
    heartbeat_file.write_text(content)

    tu.tick_task("taxes")

    result = heartbeat_file.read_text()
    assert result.count("Last-run:") == 1, "duplicate Last-run fields after second tick"
    assert "2020-01-01" not in result


def test_tick_output_includes_last_run(heartbeat_file, capsys):
    tu.tick_task("taxes")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ticked"
    assert "last_run" in out
    import re
    assert re.match(r"\d{4}-\d{2}-\d{2}", out["last_run"])


def test_parse_user_tasks_reads_last_run(heartbeat_file):
    content = heartbeat_file.read_text().replace(
        "Schedule: 2027-06-10",
        "Schedule: 2027-06-10\nLast-run: 2027-06-10 12:05",
    )
    heartbeat_file.write_text(content)

    tasks = tu.parse_user_tasks(content)
    assert tasks[0].get("last_run") == "2027-06-10 12:05"


# ---------------------------------------------------------------------------
# New tests: datetime schedule + hourly recurrence
# ---------------------------------------------------------------------------

HEARTBEAT_DATETIME = """\
# Heartbeat Tasks

## User Tasks

### Gmail scan
Schedule: 2027-06-10 08:00
Recur: every 2 hours
Until: 2027-06-15
Added: 2027-06-08

## Completed
"""

HEARTBEAT_HOURLY_LATE = """\
# Heartbeat Tasks

## User Tasks

### Late night check
Schedule: 2027-06-10 23:00
Recur: every 2 hours
Until: 2027-06-15
Added: 2027-06-08

## Completed
"""

HEARTBEAT_DAILY_DATETIME = """\
# Heartbeat Tasks

## User Tasks

### Morning briefing
Schedule: 2027-06-10 07:00
Recur: every 1 day
Until: 2027-06-20
Added: 2027-06-08

## Completed
"""

HEARTBEAT_DATE_ONLY = """\
# Heartbeat Tasks

## User Tasks

### Daily reminder
Schedule: 2027-06-10
Recur: every 1 day
Until: 2027-06-20
Added: 2027-06-08

## Completed
"""


@pytest.fixture()
def heartbeat_datetime(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(HEARTBEAT_DATETIME, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


@pytest.fixture()
def heartbeat_hourly_late(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(HEARTBEAT_HOURLY_LATE, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


@pytest.fixture()
def heartbeat_daily_datetime(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(HEARTBEAT_DAILY_DATETIME, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


@pytest.fixture()
def heartbeat_date_only(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(HEARTBEAT_DATE_ONLY, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


def test_tick_datetime_schedule_advances_time(heartbeat_datetime, capsys):
    """Task with Schedule 2027-06-10 08:00 and Recur every 2 hours → next is 2027-06-10 10:00."""
    tu.tick_task("gmail scan")
    content = heartbeat_datetime.read_text()
    assert "Schedule: 2027-06-10 10:00" in content
    assert "Schedule: 2027-06-10 08:00" not in content

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ticked"
    assert out["next"] == "2027-06-10 10:00"


def test_tick_hourly_preserves_time_component(heartbeat_hourly_late, capsys):
    """Tick on hourly task at 23:00 wraps to next day correctly: 2027-06-11 01:00."""
    tu.tick_task("late night")
    content = heartbeat_hourly_late.read_text()
    assert "Schedule: 2027-06-11 01:00" in content
    assert "Schedule: 2027-06-10 23:00" not in content

    out = json.loads(capsys.readouterr().out)
    assert out["next"] == "2027-06-11 01:00"


def test_tick_daily_datetime_preserves_time(heartbeat_daily_datetime, capsys):
    """Task with Schedule 2027-06-10 07:00 and Recur every 1 day → 2027-06-11 07:00."""
    tu.tick_task("morning briefing")
    content = heartbeat_daily_datetime.read_text()
    assert "Schedule: 2027-06-11 07:00" in content
    assert "Schedule: 2027-06-10 07:00" not in content

    out = json.loads(capsys.readouterr().out)
    assert out["next"] == "2027-06-11 07:00"


def test_tick_hourly_writes_last_run(heartbeat_datetime, capsys):
    """After tick on an hourly task, Last-run field is written."""
    before = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    tu.tick_task("gmail scan")
    after = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

    content = heartbeat_datetime.read_text()
    m = re.search(r"Last-run:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", content)
    assert m, "Last-run field not written after --tick on hourly task"
    assert before <= m.group(1) <= after


def test_tick_catches_up_past_schedule_to_future(tmp_path, monkeypatch, capsys):
    """When schedule is far in the past, tick advances to the next future occurrence."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""\
# Heartbeat Tasks

## User Tasks

### Stale task
Schedule: 1999-01-01 08:00
Recur: every 1 day
Added: 1999-01-01

## Completed
""", encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.tick_task("Stale task")
    content = hb.read_text()
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ticked"
    # The next schedule should be in the future, not 2025-01-02
    next_dt = datetime.strptime(out["next"], "%Y-%m-%d %H:%M")
    now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    assert next_dt > now, f"Expected future date, got {out['next']}"
    assert "1999-01-02" not in content


def test_tick_catches_up_hourly(tmp_path, monkeypatch, capsys):
    """Hourly task with stale schedule jumps to next future hour."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""\
# Heartbeat Tasks

## User Tasks

### Old scan
Schedule: 1999-06-01 03:00
Recur: every 1 hour
Added: 1999-06-01

## Completed
""", encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.tick_task("Old scan")
    out = json.loads(capsys.readouterr().out)
    next_dt = datetime.strptime(out["next"], "%Y-%m-%d %H:%M")
    now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    assert next_dt > now


def test_tick_date_only_schedule_unchanged(heartbeat_date_only, capsys):
    """Existing date-only task (Recur every 1 day) still advances correctly (regression)."""
    tu.tick_task("daily reminder")
    content = heartbeat_date_only.read_text()
    assert "Schedule: 2027-06-11" in content
    assert "Schedule: 2027-06-10\n" not in content

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ticked"
    assert out["next"] == "2027-06-11"


# ---------------------------------------------------------------------------
# Channel/To routing fields
# ---------------------------------------------------------------------------

HEARTBEAT_EMPTY_TASKS = """\
# Heartbeat Tasks

## User Tasks

## Completed
"""


@pytest.fixture()
def heartbeat_empty(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(HEARTBEAT_EMPTY_TASKS, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


def test_format_task_includes_recipients():
    block = tu.format_task("Remind: call HVAC", "2026-03-20 09:00",
                           recipients="abc123@lid:whatsapp")
    assert "Recipients: abc123@lid:whatsapp" in block


def test_format_task_includes_multiple_recipients():
    block = tu.format_task("Morning briefing", "2026-03-20 07:00",
                           recipients="primary:whatsapp,sam:telegram")
    assert "Recipients: primary:whatsapp,sam:telegram" in block


def test_format_task_omits_recipients_when_not_provided():
    block = tu.format_task("Remind: call HVAC", "2026-03-20 09:00")
    assert "Recipients:" not in block


def test_add_task_writes_recipients(heartbeat_empty, capsys):
    tu.add_task("Remind: taxes", "2026-03-20", recipients="abc123@lid:whatsapp")
    content = heartbeat_empty.read_text()
    assert "Recipients: abc123@lid:whatsapp" in content


def test_add_task_writes_multiple_recipients(heartbeat_empty, capsys):
    tu.add_task("Briefing", "2026-03-20 07:00",
                recipients="primary:whatsapp,sam:telegram")
    content = heartbeat_empty.read_text()
    assert "Recipients: primary:whatsapp,sam:telegram" in content


def test_parse_user_tasks_reads_recipients(heartbeat_empty):
    tu.add_task("Remind: taxes", "2026-03-20",
                recipients="abc123@lid:whatsapp,xyz@lid:telegram")
    content = heartbeat_empty.read_text()
    tasks = tu.parse_user_tasks(content)
    assert tasks[0]["recipients"] == "abc123@lid:whatsapp,xyz@lid:telegram"


def test_parse_user_tasks_missing_recipients(heartbeat_file):
    """Tasks without Recipients field parse without error and omit the key."""
    tasks = tu.parse_user_tasks(heartbeat_file.read_text())
    assert "recipients" not in tasks[0]


# ---------------------------------------------------------------------------
# --edit flag tests
# ---------------------------------------------------------------------------

def test_edit_task_changes_description(heartbeat_file, capsys):
    tu.edit_task("taxes", new_desc="Remind: complete Tax Filing")
    content = heartbeat_file.read_text()
    assert "### Remind: complete Tax Filing" in content
    assert "### Remind: complete Taxes" not in content
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "edited"
    assert out["task"] == "Remind: complete Tax Filing"


def test_edit_task_changes_schedule(heartbeat_file, capsys):
    tu.edit_task("taxes", new_schedule="2027-07-15")
    content = heartbeat_file.read_text()
    assert "Schedule: 2027-07-15" in content
    assert "Schedule: 2027-06-10\n" not in content
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "edited"
    assert out["fields"]["Schedule"] == "2027-07-15"


def test_edit_task_changes_recur(heartbeat_file, capsys):
    tu.edit_task("taxes", new_recur="every 7 days")
    content = heartbeat_file.read_text()
    assert "Recur: every 7 days" in content
    assert "Recur: every 1 day" not in content


def test_edit_task_removes_recur_with_empty_string(heartbeat_file, capsys):
    tu.edit_task("taxes", new_recur="")
    content = heartbeat_file.read_text()
    assert "Recur:" not in content


def test_edit_task_removes_until_with_empty_string(heartbeat_file, capsys):
    tu.edit_task("taxes", new_until="")
    content = heartbeat_file.read_text()
    assert "Until:" not in content


def test_edit_task_preserves_unchanged_fields(heartbeat_file, capsys):
    """Editing only the schedule must not disturb Recur, Until, or Added."""
    tu.edit_task("taxes", new_schedule="2027-07-01")
    content = heartbeat_file.read_text()
    assert "Recur: every 1 day" in content
    assert "Until: 2027-06-20" in content
    assert "Added: 2027-06-08" in content


def test_edit_task_adds_recipients(heartbeat_file, capsys):
    """A task without Recipients gets one after edit."""
    tu.edit_task("taxes", new_recipients="primary:telegram")
    content = heartbeat_file.read_text()
    assert "Recipients: primary:telegram" in content


def test_edit_task_updates_existing_recipients(heartbeat_empty, capsys):
    tu.add_task("Pay bills", "2026-04-01", recipients="old:channel")
    capsys.readouterr()
    tu.edit_task("Pay bills", new_recipients="new:telegram")
    content = heartbeat_empty.read_text()
    assert "Recipients: new:telegram" in content
    assert "Recipients: old:channel" not in content


def test_edit_task_not_found_exits(heartbeat_file, capsys):
    with pytest.raises(SystemExit):
        tu.edit_task("nonexistent task xyz", new_desc="New")


def test_edit_task_no_fields_exits(heartbeat_file, capsys):
    with pytest.raises(SystemExit):
        tu.edit_task("taxes")


def test_edit_task_preserves_blank_line_before_completed(heartbeat_file, capsys):
    """Edited task must keep exactly one blank line before ## Completed."""
    tu.edit_task("taxes", new_schedule="2027-07-01")
    content = heartbeat_file.read_text()
    assert "\n\n## Completed" in content
    assert "\n\n\n## Completed" not in content


TWO_TASK_HEARTBEAT = """\
# Heartbeat Tasks

## User Tasks

### Remind: call HVAC
Schedule: 2027-06-20
Added: 2027-06-08

### Remind: complete Taxes
Schedule: 2027-06-10
Recur: every 1 day
Until: 2027-06-20
Added: 2027-06-08

## Completed
"""


@pytest.fixture()
def heartbeat_two_tasks(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(TWO_TASK_HEARTBEAT, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


def test_edit_non_last_task_no_extra_blank_lines(heartbeat_two_tasks, capsys):
    """Editing the first of two tasks must not balloon whitespace between tasks."""
    tu.edit_task("HVAC", new_schedule="2027-07-01")
    content = heartbeat_two_tasks.read_text()
    # Exactly one blank line between tasks, not two or more
    assert "\n\n\n" not in content
    assert "### Remind: complete Taxes" in content


def test_edit_last_task_preserves_blank_line_before_completed(heartbeat_two_tasks, capsys):
    """Editing the last task must keep exactly one blank line before ## Completed."""
    tu.edit_task("taxes", new_schedule="2027-07-01")
    content = heartbeat_two_tasks.read_text()
    assert "\n\n## Completed" in content
    assert "\n\n\n## Completed" not in content
    assert "\n\n\n" not in content


def test_edit_task_multiple_fields_at_once(heartbeat_file, capsys):
    tu.edit_task("taxes", new_desc="Updated Task", new_schedule="2027-08-01",
                 new_recur="every 3 days")
    content = heartbeat_file.read_text()
    assert "### Updated Task" in content
    assert "Schedule: 2027-08-01" in content
    assert "Recur: every 3 days" in content
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "edited"
    assert out["task"] == "Updated Task"


# ---------------------------------------------------------------------------
# Model field tests
# ---------------------------------------------------------------------------

def test_format_task_includes_model():
    block = tu.format_task("Remind: vitamins", "2027-06-10 09:00",
                           recipients="primary:whatsapp", model="flash")
    assert "Model: flash" in block


def test_format_task_omits_model_when_not_provided():
    block = tu.format_task("Remind: vitamins", "2027-06-10 09:00")
    assert "Model:" not in block


def test_add_task_writes_model(heartbeat_empty, capsys):
    tu.add_task("Remind: vitamins", "2027-06-10 09:00",
                recipients="primary:whatsapp", model="flash")
    content = heartbeat_empty.read_text()
    assert "Model: flash" in content


def test_parse_user_tasks_reads_model(heartbeat_empty):
    tu.add_task("Remind: vitamins", "2027-06-10 09:00",
                recipients="primary:whatsapp", model="haiku")
    content = heartbeat_empty.read_text()
    tasks = tu.parse_user_tasks(content)
    assert tasks[0].get("model") == "haiku"


def test_edit_task_adds_model(heartbeat_file, capsys):
    tu.edit_task("taxes", new_model="flash")
    content = heartbeat_file.read_text()
    assert "Model: flash" in content


def test_edit_task_removes_model(heartbeat_empty, capsys):
    tu.add_task("Remind: test", "2027-06-10", recipients="primary:whatsapp",
                model="flash")
    capsys.readouterr()
    tu.edit_task("test", new_model="")
    content = heartbeat_empty.read_text()
    assert "Model:" not in content


def test_edit_task_preserves_model_when_not_edited(heartbeat_empty, capsys):
    tu.add_task("Remind: test", "2027-06-10", recipients="primary:whatsapp",
                model="pro")
    capsys.readouterr()
    tu.edit_task("test", new_schedule="2027-07-01")
    content = heartbeat_empty.read_text()
    assert "Model: pro" in content


def test_list_tasks_includes_model(heartbeat_empty, capsys):
    tu.add_task("Remind: test", "2027-06-10", recipients="primary:whatsapp",
                model="sonnet")
    capsys.readouterr()
    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["model"] == "sonnet"


def test_list_tasks_includes_type(heartbeat_empty, capsys):
    """System tasks should have type='system' in list output."""
    heartbeat_empty.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Gmail scan\n"
        "Type: system\n"
        "Schedule: 2026-01-01 09:00\n"
        "Recur: every 1 hour\n"
        "Recipients: primary:whatsapp\n\n"
        "### Call dentist\n"
        "Schedule: 2026-04-15 14:00\n"
        "Recipients: primary:whatsapp\n\n"
        "## Completed\n"
    )
    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 2
    gmail = next(t for t in out if t["description"] == "Gmail scan")
    dentist = next(t for t in out if t["description"] == "Call dentist")
    assert gmail["type"] == "system"
    assert dentist["type"] == ""


# ---------------------------------------------------------------------------
# System task field preservation in edit (Type, Account, Institution)
# ---------------------------------------------------------------------------

HEARTBEAT_SYSTEM_TASK = """\
# Heartbeat Tasks

## User Tasks

### Balance check
Type: system
Schedule: 2027-06-10 09:00
Recur: every 1 day
Recipients: primary:whatsapp
Account: 5733
Institution: ally
Added: 2027-06-08

## Completed
"""


@pytest.fixture()
def heartbeat_system(tmp_path, monkeypatch):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(HEARTBEAT_SYSTEM_TASK, encoding="utf-8")
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    return hb


def test_edit_preserves_type_account_institution(heartbeat_system, capsys):
    """Editing a system task must not drop Type, Account, or Institution fields."""
    tu.edit_task("Balance check", new_model="flash")
    content = heartbeat_system.read_text()
    assert "Type: system" in content
    assert "Account: 5733" in content
    assert "Institution: ally" in content
    assert "Model: flash" in content


# ---------------------------------------------------------------------------
# Agentic task support (Type: agentic + Goal field)
# ---------------------------------------------------------------------------


def test_format_task_agentic_includes_type_and_goal():
    block = tu.format_task(
        "Generate math report", "2027-06-01 08:00",
        recipients="primary:whatsapp", task_type="agentic",
        goal="Read Kemi's math practice log, summarize progress, send report",
    )
    assert "Type: agentic" in block
    assert "Goal: Read Kemi's math practice log" in block


def test_format_task_sanitizes_multiline_goal():
    block = tu.format_task(
        "Test task", "2027-06-01 08:00",
        recipients="primary:whatsapp", task_type="agentic",
        goal="Line one\nLine two\nLine three",
    )
    assert "Goal: Line one Line two Line three" in block
    assert "\n" not in block.split("Goal: ")[1].split("\n")[0].replace("Line one Line two Line three", "")


def test_edit_task_sanitizes_multiline_goal(heartbeat_empty, capsys):
    tu.add_task("Test task", "2027-06-10", recipients="primary:whatsapp",
                task_type="agentic", goal="original")
    capsys.readouterr()
    tu.edit_task("Test task", new_goal="line one\nline two")
    content = heartbeat_empty.read_text()
    assert "Goal: line one line two" in content
    assert "line one\nline two" not in content


def test_format_task_agentic_without_goal():
    block = tu.format_task(
        "Research weekend activities", "2027-06-01 08:00",
        recipients="primary:whatsapp", task_type="agentic",
    )
    assert "Type: agentic" in block
    assert "Goal:" not in block


def test_format_task_type_appears_before_schedule():
    block = tu.format_task(
        "Test task", "2027-06-01 08:00",
        recipients="primary:whatsapp", task_type="agentic",
    )
    lines = block.split("\n")
    type_idx = next(i for i, l in enumerate(lines) if l.startswith("Type:"))
    sched_idx = next(i for i, l in enumerate(lines) if l.startswith("Schedule:"))
    assert type_idx < sched_idx


def test_add_task_agentic_writes_all_fields(heartbeat_empty, capsys):
    tu.add_task(
        "Generate math report", "2027-06-01 08:00",
        recipients="primary:whatsapp", task_type="agentic",
        goal="Read math log and send summary",
    )
    content = heartbeat_empty.read_text()
    assert "Type: agentic" in content
    assert "Goal: Read math log and send summary" in content
    assert "### Generate math report" in content


def test_parse_user_tasks_reads_agentic_fields(heartbeat_empty):
    tu.add_task(
        "Generate math report", "2027-06-01 08:00",
        recipients="primary:whatsapp", task_type="agentic",
        goal="Read math log and send summary",
    )
    content = heartbeat_empty.read_text()
    tasks = tu.parse_user_tasks(content)
    task = tasks[0]
    assert task["type"] == "agentic"
    assert task["goal"] == "Read math log and send summary"


def test_edit_task_adds_goal(heartbeat_file, capsys):
    tu.edit_task("taxes", new_goal="Look up IRS deadlines and summarize")
    content = heartbeat_file.read_text()
    assert "Goal: Look up IRS deadlines and summarize" in content


def test_edit_task_removes_goal(heartbeat_empty, capsys):
    tu.add_task("Test task", "2027-06-10", recipients="primary:whatsapp",
                task_type="agentic", goal="some instructions")
    capsys.readouterr()
    tu.edit_task("Test task", new_goal="")
    content = heartbeat_empty.read_text()
    assert "Goal:" not in content


def test_edit_task_preserves_goal_when_not_edited(heartbeat_empty, capsys):
    tu.add_task("Test task", "2027-06-10", recipients="primary:whatsapp",
                task_type="agentic", goal="important instructions")
    capsys.readouterr()
    tu.edit_task("Test task", new_schedule="2027-07-01")
    content = heartbeat_empty.read_text()
    assert "Goal: important instructions" in content
    assert "Type: agentic" in content


def test_list_tasks_includes_goal(heartbeat_empty, capsys):
    tu.add_task("Math report", "2027-06-10", recipients="primary:whatsapp",
                task_type="agentic", goal="generate and send report")
    capsys.readouterr()
    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["goal"] == "generate and send report"
    assert out[0]["type"] == "agentic"


def test_list_tasks_includes_type_agentic(heartbeat_empty, capsys):
    """Agentic tasks should have type='agentic' in list output."""
    heartbeat_empty.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Generate math report\n"
        "Type: agentic\n"
        "Schedule: 2027-06-01 08:00\n"
        "Recur: every 1 month\n"
        "Recipients: primary:whatsapp\n"
        "Goal: Read Kemi's math log and summarize\n\n"
        "### Call dentist\n"
        "Schedule: 2026-04-15 14:00\n"
        "Recipients: primary:whatsapp\n\n"
        "## Completed\n"
    )
    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    math = next(t for t in out if t["description"] == "Generate math report")
    dentist = next(t for t in out if t["description"] == "Call dentist")
    assert math["type"] == "agentic"
    assert math["goal"] == "Read Kemi's math log and summarize"
    assert dentist["type"] == ""
    assert dentist["goal"] == ""


def test_tick_agentic_task(heartbeat_empty, capsys):
    """Agentic recurring tasks should tick correctly, preserving Type and Goal."""
    heartbeat_empty.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Generate weekly report\n"
        "Type: agentic\n"
        "Schedule: 2027-06-01 08:00\n"
        "Recur: every 7 days\n"
        "Recipients: primary:whatsapp\n"
        "Goal: Read Kemi's math log and summarize\n"
        "Added: 2027-05-01\n\n"
        "## Completed\n"
    )
    tu.tick_task("weekly report")
    content = heartbeat_empty.read_text()
    assert "Type: agentic" in content
    assert "Goal: Read Kemi's math log and summarize" in content
    assert "Schedule: 2027-06-08 08:00" in content


# ---------------------------------------------------------------------------
# heartbeat_lock: cross-process serialization
# ---------------------------------------------------------------------------

import multiprocessing
import time as _time


def _hold_lock_then_tick(tools_dir: str, workspace: str, hold_s: float, ready_path: str) -> None:
    """Child process: take the workspace lock and hold for hold_s. Pass
    tools_dir explicitly so this works under both fork and spawn start
    methods — spawn'd children don't inherit the parent's sys.path."""
    import sys
    from pathlib import Path
    sys.path.insert(0, tools_dir)
    from heartbeat_lock import heartbeat_lock as _hb_lock
    Path(ready_path).write_text("ready")
    with _hb_lock(workspace):
        _time.sleep(hold_s)


def test_concurrent_tick_serializes_via_lock(heartbeat_file, monkeypatch):
    """Repro of the prod _advance_schedules vs --tick race: while one
    holder owns the lock, a parallel mutation must wait — last-writer-wins
    can't lose the slower writer's update because they're now serialized."""
    ready = heartbeat_file.parent / "ready"
    tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
    proc = multiprocessing.Process(
        target=_hold_lock_then_tick,
        args=(tools_dir, str(heartbeat_file.parent), 0.4, str(ready)),
    )
    proc.start()
    try:
        deadline = _time.monotonic() + 2.0
        while not ready.exists() and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert ready.exists()

        t0 = _time.monotonic()
        tu.tick_task("taxes")
        elapsed = _time.monotonic() - t0
        assert elapsed > 0.2, f"tick did not block on lock (elapsed {elapsed}s)"
    finally:
        proc.join(timeout=2.0)
        if proc.is_alive():
            proc.terminate()


def test_lock_file_created_in_workspace(heartbeat_file):
    tu.tick_task("taxes")
    assert (heartbeat_file.parent / ".heartbeat.lock").exists()


def test_atomic_write_no_tmp_residue(heartbeat_file):
    tu.tick_task("taxes")
    assert not (heartbeat_file.parent / "HEARTBEAT.md.tmp").exists()


def test_tick_auto_complete_emits_single_status_line(tmp_path, monkeypatch, capsys):
    """tick_task's past-Until path must NOT double-print — the inner
    complete_task call now uses silent=True so callers see only the
    auto-completed JSON line. Pre-fix the exec tool's parser saw two
    JSON objects on stdout and choked."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n## User Tasks\n\n"
        "### Already past\n"
        "Schedule: 2027-06-08\n"
        "Recur: every 1 day\n"
        "Until: 2027-06-08\n"
        "Added: 2027-06-01\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    tu.tick_task("Already past")
    captured = capsys.readouterr().out.strip()
    lines = [line for line in captured.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one JSON line, got {lines!r}"
    payload = json.loads(lines[0])
    assert payload["status"] == "auto-completed"


def test_edit_task_field_sets_unknown_field(heartbeat_file, capsys):
    tu.edit_task("taxes", extra_fields=["SheetId=1aBcD"])
    content = heartbeat_file.read_text()
    assert "SheetId: 1aBcD" in content


def test_edit_task_field_clears_with_empty_value(heartbeat_file, capsys):
    tu.edit_task("taxes", extra_fields=["SheetId=1aBcD"])
    tu.edit_task("taxes", extra_fields=["SheetId="])
    content = heartbeat_file.read_text()
    assert "SheetId" not in content


def test_edit_task_field_preserves_unknown_field_across_unrelated_edit(heartbeat_file, capsys):
    """Regression: previously, edits dropped any field not in the hardcoded order list."""
    tu.edit_task("taxes", extra_fields=["SheetId=1aBcD", "Period=biweekly"])
    tu.edit_task("taxes", new_schedule="2027-07-01")
    content = heartbeat_file.read_text()
    assert "SheetId: 1aBcD" in content
    assert "Period: biweekly" in content
    assert "Schedule: 2027-07-01" in content


def test_edit_task_field_multiple_in_one_call(heartbeat_file, capsys):
    tu.edit_task("taxes", extra_fields=["SheetId=1aBcD", "Period=monthly", "Anchor=2026-05-01"])
    content = heartbeat_file.read_text()
    assert "SheetId: 1aBcD" in content
    assert "Period: monthly" in content
    assert "Anchor: 2026-05-01" in content


def test_edit_task_field_rejects_missing_equals(heartbeat_file, capsys):
    with pytest.raises(SystemExit):
        tu.edit_task("taxes", extra_fields=["SheetId-no-equals"])


def test_edit_task_field_rejects_empty_key(heartbeat_file, capsys):
    with pytest.raises(SystemExit):
        tu.edit_task("taxes", extra_fields=["=value"])


def test_edit_task_field_strips_embedded_newlines(heartbeat_file, capsys):
    """Defense in depth — a malicious value can't break the line-based file format."""
    tu.edit_task("taxes", extra_fields=["SheetId=abc\n### Fake Task\nSchedule: 2099-01-01"])
    content = heartbeat_file.read_text()
    # No injected `### ` heading, and no second `Schedule:` line that would override the real one.
    heading_lines = [ln for ln in content.splitlines() if ln.startswith("### ")]
    assert heading_lines == ["### Remind: complete Taxes"]
    schedule_lines = [ln for ln in content.splitlines() if ln.startswith("Schedule:")]
    assert schedule_lines == ["Schedule: 2027-06-10"]
    assert "SheetId: abc" in content


# ---------------------------------------------------------------------------
# Default-tier user-task cap (HOMER_MODEL_TIER=default)
# ---------------------------------------------------------------------------


def _seed_user_tasks(hb_file, count: int, *, prefix: str = "Task") -> None:
    """Append `count` reminder tasks (no Type:) to the User Tasks section."""
    content = hb_file.read_text()
    user_marker = "## User Tasks"
    completed_marker = "## Completed"
    insert = ""
    for i in range(count):
        insert += (
            f"\n### {prefix} {i}\n"
            f"Schedule: 2027-06-{10 + i:02d}\n"
            f"Recipients: primary:whatsapp\n"
            f"Added: 2027-06-08\n"
        )
    completed_pos = content.index(completed_marker)
    content = content[:completed_pos] + insert + "\n" + content[completed_pos:]
    hb_file.write_text(content)


def test_default_tier_cap_allows_under_limit(heartbeat_empty, monkeypatch, capsys):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.delenv("HOMER_DEFAULT_TIER_MAX_USER_TASKS", raising=False)
    _seed_user_tasks(heartbeat_empty, 4)
    tu.add_task("Remind: 5th task", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"
    assert "### Remind: 5th task" in heartbeat_empty.read_text()


def test_default_tier_cap_blocks_at_limit(heartbeat_empty, monkeypatch, capsys):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.delenv("HOMER_DEFAULT_TIER_MAX_USER_TASKS", raising=False)
    _seed_user_tasks(heartbeat_empty, 5)
    with pytest.raises(SystemExit) as exc:
        tu.add_task("Remind: overflow", "2027-07-01", recipients="primary:whatsapp")
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out
    assert "Default tier supports up to 5" in out["error"]
    assert "BYOK" in out["error"]
    assert "<portal>/settings/ai-provider" in out["error"]
    # The would-be 6th task was not written
    assert "Remind: overflow" not in heartbeat_empty.read_text()


def test_default_tier_cap_respects_env_override(heartbeat_empty, monkeypatch, capsys):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.setenv("HOMER_DEFAULT_TIER_MAX_USER_TASKS", "2")
    _seed_user_tasks(heartbeat_empty, 2)
    with pytest.raises(SystemExit):
        tu.add_task("Remind: third", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert "Default tier supports up to 2" in out["error"]


def test_default_tier_cap_invalid_env_falls_back_to_default(heartbeat_empty, monkeypatch, capsys):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.setenv("HOMER_DEFAULT_TIER_MAX_USER_TASKS", "not-a-number")
    _seed_user_tasks(heartbeat_empty, 5)
    with pytest.raises(SystemExit):
        tu.add_task("Remind: overflow", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert "Default tier supports up to 5" in out["error"]


def test_byok_tier_no_cap(heartbeat_empty, monkeypatch, capsys):
    monkeypatch.setenv("HOMER_MODEL_TIER", "byok")
    monkeypatch.setenv("HOMER_DEFAULT_TIER_MAX_USER_TASKS", "5")
    _seed_user_tasks(heartbeat_empty, 100)
    tu.add_task("Remind: 101st", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"


def test_managed_tier_no_cap(heartbeat_empty, monkeypatch, capsys):
    """managed tier (future Anthropic-managed-key state) is uncapped."""
    monkeypatch.setenv("HOMER_MODEL_TIER", "managed")
    _seed_user_tasks(heartbeat_empty, 50)
    tu.add_task("Remind: more", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"


def test_no_tier_env_no_cap(heartbeat_empty, monkeypatch, capsys):
    """Unset HOMER_MODEL_TIER (e.g. local dev) must not trip the cap."""
    monkeypatch.delenv("HOMER_MODEL_TIER", raising=False)
    _seed_user_tasks(heartbeat_empty, 100)
    tu.add_task("Remind: more", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"


# ---------------------------------------------------------------------------
# Stable task IDs
# ---------------------------------------------------------------------------


def test_generate_task_id_format():
    for _ in range(20):
        tid = tu.generate_task_id()
        assert re.match(r"^t_[a-z2-7]{8}$", tid), f"bad id: {tid}"


def test_is_task_id_validates_format():
    # Valid: lowercase base32 (alphabet a-z + 2-7), 8 chars after `t_`.
    assert tu.is_task_id("t_a2b3c4d5")
    assert tu.is_task_id("t_22222222")
    assert tu.is_task_id("t_zzzzzzzz")
    # Wrong case — id alphabet is lowercase only.
    assert not tu.is_task_id("t_ABC")
    assert not tu.is_task_id("t_A1B2C3D4")
    # Wrong prefix / wrong length.
    assert not tu.is_task_id("a_a2b3c4d5")
    assert not tu.is_task_id("t_short")
    assert not tu.is_task_id("t_a2b3c4d5e")
    # Characters outside the base32 alphabet (0, 1, 8, 9 are excluded).
    assert not tu.is_task_id("t_0a2b3c4d")
    assert not tu.is_task_id("t_1a2b3c4d")
    assert not tu.is_task_id("t_8a2b3c4d")
    assert not tu.is_task_id("t_9a2b3c4d")


def test_two_add_calls_produce_distinct_ids(heartbeat_empty, capsys):
    tu.add_task("Remind: foo", "2027-06-10", recipients="primary:whatsapp")
    out1 = json.loads(capsys.readouterr().out)
    tu.add_task("Remind: bar", "2027-06-11", recipients="primary:whatsapp")
    out2 = json.loads(capsys.readouterr().out)
    assert out1["id"] != out2["id"]
    assert tu.is_task_id(out1["id"])
    assert tu.is_task_id(out2["id"])


def test_add_emits_id_line_in_block(heartbeat_empty, capsys):
    tu.add_task("Remind: HVAC", "2027-06-10 09:00", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    content = heartbeat_empty.read_text()
    assert f"Id: {out['id']}" in content
    # Id sits immediately after the title heading.
    block = re.search(r"### Remind: HVAC\n([^\n]+)", content)
    assert block is not None
    assert block.group(1) == f"Id: {out['id']}"


def test_complete_by_id_matches_exact_block(heartbeat_empty, capsys):
    """Two tasks share the same title — only the one whose id we pass should be removed.
    This is the Piedmont scenario: title-substring matching nukes the wrong row."""
    tu.add_task("Remind: Piedmont doctor appointment", "2027-06-10 09:00",
                recipients="primary:whatsapp")
    out_a = json.loads(capsys.readouterr().out)
    tu.add_task("Remind: Piedmont doctor appointment", "2027-07-10 09:00",
                recipients="primary:whatsapp")
    out_b = json.loads(capsys.readouterr().out)
    id_b = out_b["id"]

    tu.complete_task(id_b)
    capsys.readouterr()

    content = heartbeat_empty.read_text()
    # Block A still in User Tasks, block B moved to Completed.
    assert f"Id: {out_a['id']}" in content
    assert f"Id: {id_b}" not in content


def test_complete_by_title_substring_still_works(heartbeat_empty, capsys):
    tu.add_task("Remind: file taxes", "2027-06-10", recipients="primary:whatsapp")
    capsys.readouterr()
    tu.complete_task("taxes")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "completed"
    assert "### Remind: file taxes" not in heartbeat_empty.read_text()


def test_tick_by_id(heartbeat_empty, capsys):
    tu.add_task("Daily reminder", "2027-06-10",
                recipients="primary:whatsapp", recur="every 1 day")
    out_add = json.loads(capsys.readouterr().out)
    tid = out_add["id"]

    tu.tick_task(tid)
    out_tick = json.loads(capsys.readouterr().out)
    assert out_tick["status"] == "ticked"
    # Id survives the tick.
    assert f"Id: {tid}" in heartbeat_empty.read_text()


def test_lazy_backfill_adds_ids_to_legacy_blocks(tmp_path, monkeypatch, capsys):
    """A HEARTBEAT.md from before stable IDs gets one on first read; idempotent thereafter."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Remind: pay bills\n"
        "Schedule: 2027-06-10\n"
        "Recipients: primary:whatsapp\n"
        "Added: 2027-06-08\n\n"
        "### Gmail scan\n"
        "Type: system\n"
        "Schedule: 2027-06-10 09:00\n"
        "Recur: every 1 hour\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 2
    for task in out:
        assert tu.is_task_id(task["id"]), f"missing id on {task['description']!r}"

    content_after = hb.read_text()
    assert content_after.count("Id: t_") == 2

    # Second run is idempotent — no new IDs, no duplicate writes.
    tu.list_tasks()
    out2 = json.loads(capsys.readouterr().out)
    ids_first = {t["id"] for t in out}
    ids_second = {t["id"] for t in out2}
    assert ids_first == ids_second
    assert hb.read_text() == content_after


def test_backfill_ids_cli(tmp_path, monkeypatch, capsys):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Remind: a\n"
        "Schedule: 2027-06-10\n\n"
        "### Remind: b\n"
        "Schedule: 2027-06-11\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.backfill_ids()
    out = json.loads(capsys.readouterr().out)
    assert out == {"status": "backfilled", "added": 2}

    # Idempotent: second run reports zero.
    tu.backfill_ids()
    out2 = json.loads(capsys.readouterr().out)
    assert out2 == {"status": "backfilled", "added": 0}


def test_list_tasks_includes_id(heartbeat_empty, capsys):
    tu.add_task("Remind: foo", "2027-06-10", recipients="primary:whatsapp")
    out_add = json.loads(capsys.readouterr().out)
    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == out_add["id"]


def test_format_task_includes_id_when_provided():
    block = tu.format_task("Remind: thing", "2027-06-10",
                           recipients="primary:whatsapp",
                           task_id="t_a1b2c3d4")
    lines = block.split("\n")
    assert lines[0] == "### Remind: thing"
    assert lines[1] == "Id: t_a1b2c3d4"


def test_format_task_omits_id_when_not_provided():
    block = tu.format_task("Remind: thing", "2027-06-10",
                           recipients="primary:whatsapp")
    assert "Id:" not in block


def test_find_task_block_by_id_tolerates_whitespace(tmp_path, monkeypatch):
    """Manual edits that leave stray whitespace around the Id value still resolve."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Remind: thing\n"
        "Id:  t_a2b3c4d5  \n"  # double-space + trailing space
        "Schedule: 2027-06-10\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)
    bounds = tu.find_task_block(hb.read_text(), "t_a2b3c4d5")
    assert bounds is not None


def test_edit_preserves_id(heartbeat_empty, capsys):
    """Editing a task must keep its Id intact and at the top of the block."""
    tu.add_task("Remind: foo", "2027-06-10", recipients="primary:whatsapp")
    tid = json.loads(capsys.readouterr().out)["id"]
    tu.edit_task(tid, new_schedule="2027-07-01")
    capsys.readouterr()
    content = heartbeat_empty.read_text()
    assert f"Id: {tid}" in content
    # Id stays right under the title (first field line).
    block = re.search(r"### Remind: foo\n([^\n]+)", content)
    assert block.group(1) == f"Id: {tid}"


def test_default_tier_cap_excludes_system_tasks(heartbeat_empty, monkeypatch, capsys):
    """System-typed tasks shouldn't count toward the user-task cap."""
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    # Inject 3 system tasks + 4 user tasks. Cap of 5 → adding the 5th user task succeeds.
    content = heartbeat_empty.read_text()
    completed_pos = content.index("## Completed")
    sys_blocks = ""
    for i in range(3):
        sys_blocks += (
            f"\n### System task {i}\n"
            f"Type: system\n"
            f"Schedule: 2027-06-{10 + i:02d} 09:00\n"
            f"Recur: every 1 hour\n"
        )
    user_blocks = ""
    for i in range(4):
        user_blocks += (
            f"\n### User task {i}\n"
            f"Schedule: 2027-06-{20 + i:02d}\n"
            f"Recipients: primary:whatsapp\n"
            f"Added: 2027-06-08\n"
        )
    content = (
        content[:completed_pos] + sys_blocks + user_blocks + "\n" + content[completed_pos:]
    )
    heartbeat_empty.write_text(content)

    tu.add_task("Remind: 5th user task", "2027-07-01", recipients="primary:whatsapp")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"

    # Adding a 6th user task should now trip the cap (5 user tasks present).
    with pytest.raises(SystemExit):
        tu.add_task("Remind: 6th user task", "2027-07-02", recipients="primary:whatsapp")
    err_out = json.loads(capsys.readouterr().out)
    assert "Default tier supports up to 5" in err_out["error"]


# ---------------------------------------------------------------------------
# Backfill must skip prose chunks that look like "### something" but have no
# Schedule: field. Regression for the PR #293 prose-injection bug.
# ---------------------------------------------------------------------------


def test_backfill_skips_instruction_prose_with_triple_hash(tmp_path, monkeypatch, capsys):
    """An instruction line referencing a literal `### title` heading inside the
    User Tasks section must NOT receive an Id: injection — only blocks with a
    Schedule: field are real tasks. The single real task in the section gets
    the id."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n"
        "Each task block has an `Id:` line right under its `### title` heading.\n\n"
        "### Remind: pay bills\n"
        "Schedule: 2027-06-10\n"
        "Recipients: primary:whatsapp\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.list_tasks()
    out = json.loads(capsys.readouterr().out)
    # Only the real task is parsed and gets an id.
    assert len(out) == 1
    assert out[0]["description"] == "Remind: pay bills"
    assert tu.is_task_id(out[0]["id"])

    content_after = hb.read_text()
    # Exactly one Id: t_ line — the prose chunk above did NOT get one.
    assert content_after.count("Id: t_") == 1
    # The original prose is untouched.
    assert "`### title` heading." in content_after


def test_backfill_skips_block_without_schedule(tmp_path, monkeypatch, capsys):
    """Symmetric case: a `### Foo` block missing Schedule: is left alone."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n"
        "## User Tasks\n\n"
        "### Foo\n"
        "Notes: no schedule here\n\n"
        "### Remind: pay bills\n"
        "Schedule: 2027-06-10\n"
        "Recipients: primary:whatsapp\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.backfill_ids()
    out = json.loads(capsys.readouterr().out)
    # Only one block (the real task) gets an id.
    assert out == {"status": "backfilled", "added": 1}

    content_after = hb.read_text()
    assert content_after.count("Id: t_") == 1
    # The Foo block is preserved as-is, with no Id: line.
    foo_block = re.search(r"### Foo\n(.*?)(?=\n###|\n## )", content_after, re.DOTALL)
    assert foo_block is not None
    assert "Id:" not in foo_block.group(1)


# ── Recipients validation ────────────────────────────────────────────────────
#
# tasks_update.py refuses --recipients values that aren't known user symbols
# (e.g. a raw chat-id or LID). Without this check, the heartbeat dispatcher's
# `users_loader.resolve_handle` can't resolve the value and the task is
# silently undeliverable — every tick logs "had Recipients but none resolved"
# and the task retries forever without ever firing.
#
# Regression net for the 2026-05-27-aftermath bug where the agent kept
# writing `Recipients: <raw_lid>:whatsapp` into new reminder tasks.


import yaml


@pytest.fixture()
def users_yaml(tmp_path, monkeypatch):
    """Provision a minimal users.yaml with one symbol and point homer's
    users_loader at it. Reset its module-level mtime cache so the new path
    is honored on the next call."""
    path = tmp_path / "users.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": 2,
        "users": {
            "resident": {
                "display_name": "Resident",
                "role": "primary",
                "channels": {"whatsapp": "15550000001"},
            },
            "second": {
                "display_name": "Second",
                "role": "member",
                "channels": {"whatsapp": "15550000002"},
            },
        },
    }))
    monkeypatch.setenv("HOMER_USERS_YAML", str(path))
    # Bust any caches in tasks_update's transitive imports — users_loader
    # uses an mtime-checked load.
    import sys
    sys.modules.pop("tools.outbound_scope_lookup", None)
    return path


def test_validate_recipients_accepts_known_symbol(users_yaml):
    # Doesn't raise.
    tu.validate_recipients("resident:whatsapp")


def test_validate_recipients_accepts_multiple_known_symbols(users_yaml):
    tu.validate_recipients("resident:whatsapp,second:whatsapp")


def test_validate_recipients_accepts_empty(users_yaml):
    # Empty / blank values aren't an error here — the parent --add caller
    # enforces required-ness separately, and `--edit` allows ""-clearing.
    tu.validate_recipients("")
    tu.validate_recipients("   ")


def test_validate_recipients_rejects_raw_lid(users_yaml):
    with pytest.raises(tu.RecipientsValidationError) as exc:
        tu.validate_recipients("15550000001@lid.whatsapp.net:whatsapp")
    msg = str(exc.value)
    assert "is not a known user symbol" in msg


def test_validate_recipients_suggests_symbol_for_known_handle(users_yaml):
    """When the raw value is a recognised channel handle, point at the
    matching user symbol so the caller can fix the command without
    guessing."""
    with pytest.raises(tu.RecipientsValidationError) as exc:
        tu.validate_recipients("15550000001:whatsapp")
    msg = str(exc.value)
    assert "resident" in msg, f"missing 'resident' hint in: {msg}"


def test_validate_recipients_rejects_missing_channel_suffix(users_yaml):
    with pytest.raises(tu.RecipientsValidationError) as exc:
        tu.validate_recipients("resident")
    assert "missing ':channel'" in str(exc.value)


def test_validate_recipients_rejects_mixed_good_and_bad(users_yaml):
    """One bad token in a comma list still fails the whole call — partial
    validation would silently let through undeliverable destinations."""
    with pytest.raises(tu.RecipientsValidationError):
        tu.validate_recipients("resident:whatsapp,15550000002@lid:whatsapp")


# ── End-to-end: add_task / edit_task surface the validation error ───────────


def _make_empty_heartbeat(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n## User Tasks\n\n## Completed\n",
        encoding="utf-8",
    )
    return hb


def test_add_task_rejects_raw_lid_recipients(tmp_path, users_yaml, monkeypatch, capsys):
    """The --add path must surface the validation error as a JSON error and
    exit non-zero so the agent (or caller) sees the failure rather than
    silently producing a stuck task."""
    hb = _make_empty_heartbeat(tmp_path)
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    with pytest.raises(SystemExit) as exc:
        tu.add_task(
            desc="Remind: do X",
            schedule="2030-01-01",
            recipients="15550000001@lid.whatsapp.net:whatsapp",
        )
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out
    assert "not a known user symbol" in out["error"]
    # And critically — the task was NOT written.
    assert "Remind: do X" not in hb.read_text()


def test_add_task_accepts_user_symbol_recipients(tmp_path, users_yaml, monkeypatch, capsys):
    hb = _make_empty_heartbeat(tmp_path)
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.add_task(
        desc="Remind: do X",
        schedule="2030-01-01",
        recipients="resident:whatsapp",
    )
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"
    content = hb.read_text()
    assert "Remind: do X" in content
    assert "Recipients: resident:whatsapp" in content


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_add_task_requires_recipients(tmp_path, monkeypatch, capsys, missing):
    """add_task() — the function, not just the CLI — must reject a missing
    or blank recipients value. The CLI's arg-parse already requires
    --recipients, but the function is called programmatically (the portal's
    task_service.add_task seeds system tasks through it). Without this
    guard, a caller that omits recipients writes a task the heartbeat
    dispatcher (Rule 3) then refuses at every tick — silently
    undeliverable. Regression net: the 2026-05-27 new-reminder failures.

    No users.yaml fixture here on purpose — the required-check fires
    before validate_recipients (which reads users.yaml), so this also
    proves the order: 'missing' is reported as 'required', not as
    'unknown symbol'.
    """
    hb = _make_empty_heartbeat(tmp_path)
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    with pytest.raises(SystemExit) as exc:
        tu.add_task(desc="Remind: do X", schedule="2030-01-01", recipients=missing)
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "Recipients is required" in out["error"]
    # Nothing written.
    assert "Remind: do X" not in hb.read_text()


def test_edit_task_rejects_raw_lid_recipients(tmp_path, users_yaml, monkeypatch, capsys):
    """--edit --recipients also validates. A successful add followed by an
    edit-to-bad value must leave the original Recipients untouched."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n## User Tasks\n\n"
        "### Remind: do X\nId: t_aaaa1111\nSchedule: 2030-01-01\n"
        "Recipients: resident:whatsapp\nAdded: 2030-01-01\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    with pytest.raises(SystemExit) as exc:
        tu.edit_task("do X", new_recipients="15550000002@lid:whatsapp")
    assert exc.value.code == 1
    assert "not a known user symbol" in json.loads(capsys.readouterr().out)["error"]
    # Original Recipients line preserved.
    assert "Recipients: resident:whatsapp" in hb.read_text()


def test_edit_task_empty_recipients_clears(tmp_path, users_yaml, monkeypatch, capsys):
    """Empty-string for --recipients on --edit clears the field — this path
    bypasses validation (no value to validate). Lock in the existing
    clear-by-empty-string behavior so the new validation hook can't
    accidentally break it."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "# Heartbeat Tasks\n\n## User Tasks\n\n"
        "### Remind: do X\nId: t_aaaa1111\nSchedule: 2030-01-01\n"
        "Recipients: resident:whatsapp\nAdded: 2030-01-01\n\n"
        "## Completed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tu, "HEARTBEAT_FILE", hb)

    tu.edit_task("do X", new_recipients="")
    assert "Recipients:" not in hb.read_text()

"""Tests for build_context.py"""

import json
import re
import sys
from pathlib import Path

import pytest

import tools.build_context as bc


# ── HEARTBEAT.md test fixtures ────────────────────────────────────────────────

SYSTEM_PREFIX = """\
# Heartbeat Tasks

## CRITICAL: Silent Mode Rules
RULE 1 — silence.

"""

USER_TASKS_HEADER = """\
## User Tasks
For each task: if today >= Schedule → handle it, then tick.
"""

SYSTEM_TASKS = """\

### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
"""

TEMPLATE = (
    SYSTEM_PREFIX
    + USER_TASKS_HEADER
    + SYSTEM_TASKS
    + "\n## Completed\n\n"
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path.parent)
    monkeypatch.setattr(bc, "load_file", lambda *a, **kw: "")
    monkeypatch.setattr(bc, "SOUL_CONTENT", "# Soul\n")
    monkeypatch.setattr(bc, "AGENTS_CONTENT", "# Agents\n")
    monkeypatch.setattr(bc, "HEARTBEAT_CONTENT", TEMPLATE)
    monkeypatch.setattr(sys, "argv", ["build_context.py"])
    return tmp_path


def fake_config(tmp_path: Path, model: str) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"agents": {"defaults": {"model": model}}}))
    return p


# ── CURRENT_MODEL tests ───────────────────────────────────────────────────────
# build_context.py is NOT a writer of CURRENT_MODEL. switch_model.py is the
# sole writer (homer#247). build_context.py must neither create nor delete
# the file — it represents a deliberate runtime model switch and must survive
# rebuilds.

def test_does_not_write_current_model_on_fresh_workspace(workspace, tmp_path, monkeypatch):
    cfg = fake_config(tmp_path, "gemini/gemini-2.5-pro")
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", cfg)
    bc.main()
    assert not (workspace / "CURRENT_MODEL").exists()


def test_does_not_write_current_model_when_only_env_set(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.setenv("HOMER_DEFAULT_MODEL", "claude-sonnet-4-6")
    bc.main()
    assert not (workspace / "CURRENT_MODEL").exists()


def test_leaves_existing_current_model_untouched(workspace, tmp_path, monkeypatch):
    """An existing CURRENT_MODEL (written by switch_model.py) must survive
    a build_context.py run unchanged — that's the whole reason the file
    exists across container restarts."""
    (workspace / "CURRENT_MODEL").write_text("claude-sonnet-4-6")
    cfg = fake_config(tmp_path, "gemini/gemini-2.5-pro")
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", cfg)
    bc.main()
    assert (workspace / "CURRENT_MODEL").read_text() == "claude-sonnet-4-6"


# ── merge_heartbeat: basic cases ──────────────────────────────────────────────

def test_merge_no_live_file_returns_template():
    result = bc.merge_heartbeat(TEMPLATE, "")
    assert result == TEMPLATE


def test_merge_live_has_no_user_tasks_returns_template():
    live = "# Old heartbeat\n\n## Active Tasks\n\nsome old task\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert result == TEMPLATE


def test_merge_system_prefix_always_from_template():
    live = TEMPLATE.replace("RULE 1 — silence.", "RULE 1 — old rule.")
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "RULE 1 — silence." in result
    assert "RULE 1 — old rule." not in result


def test_merge_user_tasks_header_from_template():
    live = TEMPLATE.replace("if today >= Schedule → handle it, then tick.", "old instructions")
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "if today >= Schedule → handle it, then tick." in result
    assert "old instructions" not in result


# ── merge_heartbeat: system tasks ────────────────────────────────────────────

def test_merge_system_tasks_preserved_when_live_has_no_tasks():
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Gmail scan" in result
    assert "Morning briefing" in result


def test_merge_system_task_schedule_preserved_from_live():
    live = TEMPLATE.replace(
        "Schedule: 2026-01-01 09:00",
        "Schedule: 2026-03-15 10:00"
    )
    result = bc.merge_heartbeat(TEMPLATE, live)
    # Gmail scan schedule from live
    assert "2026-03-15 10:00" in result
    # Morning briefing schedule from template (wasn't changed)
    assert "2026-01-01 07:00" in result


def test_merge_system_task_last_run_preserved_from_live():
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-03-15 10:00
Last-run: 2026-03-15 09:30
Recur: every 1 hour

### Morning briefing
Type: system
Schedule: 2026-03-16 07:00
Recur: every 1 day
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Last-run: 2026-03-15 09:30" in result
    assert "Schedule: 2026-03-15 10:00" in result


def test_merge_new_system_task_injected_when_missing_from_live():
    """A system task added to template that doesn't exist in live gets injected."""
    # Live has only Gmail scan, missing Morning briefing
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-03-15 10:00
Recur: every 1 hour
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Gmail scan" in result
    assert "Morning briefing" in result  # injected from template


def test_merge_preserves_id_on_system_task():
    """Stable task IDs added to live system blocks must survive a merge.

    Before stable IDs landed, system tasks were regenerated from the template
    on every merge; the Id line lives on the live block and we want it to flow
    through (preserved by _merge_system_task / _parse_task_blocks).
    """
    live_tasks = """\

### Gmail scan
Id: t_a2b3c4d5
Type: system
Schedule: 2026-03-15 10:00
Recur: every 1 hour

### Morning briefing
Id: t_zzzzzzzz
Type: system
Schedule: 2026-03-16 07:00
Recur: every 1 day
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Id: t_a2b3c4d5" in result
    assert "Id: t_zzzzzzzz" in result


def test_merge_preserves_id_on_reminder_task():
    """Reminder tasks come straight from live; their Id must stay attached."""
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day

### Remind: call HVAC
Id: t_remndddd
Schedule: 2026-04-01 09:00
Recipients: primary:whatsapp
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Id: t_remndddd" in result
    assert "### Remind: call HVAC" in result


def test_merge_system_task_exec_updated_from_template():
    """Exec field in template takes precedence over live's outdated Exec."""
    template_with_exec = TEMPLATE.replace(
        "Recur: every 1 hour",
        "Recur: every 1 hour\nExec: /new/path/gmail_fetch.py",
        1  # only Gmail scan
    )
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-03-15 10:00
Recur: every 1 hour
Exec: /old/path/gmail_fetch.py
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(template_with_exec, live)
    assert "/new/path/gmail_fetch.py" in result
    assert "/old/path/gmail_fetch.py" not in result


# ── merge_heartbeat: per-household overlay ───────────────────────────────────

# Minimal baseline template: Check escalations only, no Gmail/briefing.
# Matches what agent/HEARTBEAT.md looks like after A1 sanitization.
BASELINE_TEMPLATE = (
    SYSTEM_PREFIX
    + USER_TASKS_HEADER
    + """\

### Check escalations
Type: system
Schedule: 2026-01-01 00:00
Recur: every 30 minutes
"""
    + "\n## Completed\n\n"
)

JOHNSON_OVERLAY = """\
### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour
Recipients: primary:whatsapp

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
Recipients: primary:whatsapp
"""


def test_merge_baseline_no_overlay_no_live_returns_baseline():
    """Hosted fresh boot: only baseline tasks, empty user_context."""
    result = bc.merge_heartbeat(BASELINE_TEMPLATE, "", "")
    assert "Check escalations" in result
    assert "Gmail scan" not in result
    assert "Morning briefing" not in result


def test_merge_baseline_with_overlay_injects_household_tasks():
    """Johnson's deploy: baseline + his heartbeat_tasks.md overlay."""
    live = BASELINE_TEMPLATE  # first build on his box renders same as template
    result = bc.merge_heartbeat(BASELINE_TEMPLATE, live, JOHNSON_OVERLAY)
    assert "Check escalations" in result
    assert "Gmail scan" in result
    assert "Morning briefing" in result


def test_merge_overlay_schedule_preserved_from_live():
    """Once the overlay has been rendered once, subsequent builds preserve
    the live Schedule/Last-run on overlay tasks the same as template tasks."""
    live_tasks = """\

### Check escalations
Type: system
Schedule: 2026-03-20 00:00
Recur: every 30 minutes

### Gmail scan
Type: system
Schedule: 2026-03-15 10:00
Last-run: 2026-03-15 09:30
Recur: every 1 hour
Recipients: primary:whatsapp
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(BASELINE_TEMPLATE, live, JOHNSON_OVERLAY)
    assert "Schedule: 2026-03-15 10:00" in result
    assert "Last-run: 2026-03-15 09:30" in result


def test_merge_overlay_overrides_template_for_same_name():
    """If the overlay and template both define 'Gmail scan', overlay's
    version of same-key fields wins."""
    template_with_gmail = (
        SYSTEM_PREFIX
        + USER_TASKS_HEADER
        + """\

### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour
Recipients: template-default:whatsapp
"""
        + "\n## Completed\n\n"
    )
    overlay = """\
### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour
Recipients: primary:whatsapp,alex:whatsapp
"""
    result = bc.merge_heartbeat(template_with_gmail, template_with_gmail, overlay)
    assert "primary:whatsapp,alex:whatsapp" in result
    assert "template-default:whatsapp" not in result


def test_merge_overlay_preserves_template_only_fields():
    """Regression: when the template adds a new field to a task that the
    overlay also defines, the new field must survive the merge — overlay
    patches template field-by-field, not full-block replace.

    The morning-brief redesign hit this: PR-C added `Prompt-file:` to the
    template's Morning briefing block, but Ebby's overlay had its own
    Morning briefing block (without that field). The deploy silently
    dropped Prompt-file and the brief safe-degraded to default summary."""
    template = (
        SYSTEM_PREFIX
        + USER_TASKS_HEADER
        + """\

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
Prompt-file: users/{recipient}.brief.md
"""
        + "\n## Completed\n\n"
    )
    overlay = """\
### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
Recipients: primary:whatsapp,alex:whatsapp
"""
    result = bc.merge_heartbeat(template, template, overlay)
    assert "Prompt-file: users/{recipient}.brief.md" in result
    assert "Recipients: primary:whatsapp,alex:whatsapp" in result


def test_merge_overlay_appends_overlay_only_fields():
    """An overlay-only field (e.g. Recipients on a template task that
    didn't declare any) should append, not replace existing template
    content."""
    template = (
        SYSTEM_PREFIX
        + USER_TASKS_HEADER
        + """\

### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour
"""
        + "\n## Completed\n\n"
    )
    overlay = """\
### Gmail scan
Type: system
Recipients: primary:whatsapp
"""
    result = bc.merge_heartbeat(template, template, overlay)
    assert "Schedule: 2026-01-01 09:00" in result
    assert "Recur: every 1 hour" in result
    assert "Recipients: primary:whatsapp" in result


def test_merge_overlay_field_value_wins_over_template():
    """Same key in both (excluding live-state fields Schedule/Last-run/
    Model/Id which _merge_system_task carries from live): overlay value
    replaces template value in place."""
    template = (
        SYSTEM_PREFIX
        + USER_TASKS_HEADER
        + """\

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
"""
        + "\n## Completed\n\n"
    )
    overlay = """\
### Morning briefing
Type: system
Recur: every 2 days
Recipients: primary:whatsapp
"""
    result = bc.merge_heartbeat(template, template, overlay)
    # Overlay's Recur wins (every 2 days, not every 1 day)
    assert "Recur: every 2 days" in result
    assert "Recur: every 1 day" not in result


def test_merge_overlay_non_system_tasks_ignored():
    """Overlay entries without Type: system are not added as system tasks
    (they'd be added as reminders only if present in live)."""
    reminder_overlay = """\
### Stray reminder
Schedule: 2026-05-01 12:00
"""
    result = bc.merge_heartbeat(BASELINE_TEMPLATE, BASELINE_TEMPLATE, reminder_overlay)
    assert "Stray reminder" not in result


# ── merge_heartbeat: model field preservation ────────────────────────────────

def test_merge_system_task_model_preserved_from_live():
    """Model set on live system task survives merge even when template lacks it."""
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-03-15 10:00
Last-run: 2026-03-15 09:30
Recur: every 1 hour
Recipients: primary:whatsapp
Model: flash

### Morning briefing
Type: system
Schedule: 2026-03-16 07:00
Recur: every 1 day
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Model: flash" in result


def test_merge_system_task_model_not_injected_when_absent():
    """When neither template nor live has Model, none appears in result."""
    result = bc.merge_heartbeat(TEMPLATE, TEMPLATE)
    assert "Model:" not in result


def test_merge_system_task_model_from_live_overrides_template():
    """If both template and live have Model, live value wins."""
    template_with_model = TEMPLATE.replace(
        "Recur: every 1 hour",
        "Recur: every 1 hour\nModel: pro",
        1  # only Gmail scan
    )
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-03-15 10:00
Recur: every 1 hour
Model: flash
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(template_with_model, live)
    assert "Model: flash" in result
    assert "Model: pro" not in result


# ── merge_heartbeat: user (reminder) tasks ───────────────────────────────────

def test_merge_user_reminder_tasks_preserved_from_live():
    live_tasks = SYSTEM_TASKS + """\

### Remind: call dentist
Schedule: 2026-03-20
Added: 2026-03-12
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "Remind: call dentist" in result
    assert "Gmail scan" in result  # system task still present


def test_merge_user_reminder_not_wiped_on_system_task_update():
    """User reminder tasks survive even when system task fields change in template."""
    old_template = TEMPLATE.replace("Recur: every 1 hour", "Recur: every 2 hours")
    live_tasks = SYSTEM_TASKS.replace("Recur: every 1 hour", "Recur: every 2 hours") + """\

### Remind: water plants
Schedule: 2026-03-25
Added: 2026-03-12
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)  # new template has "every 1 hour"
    assert "Remind: water plants" in result
    assert "every 1 hour" in result  # updated from template


def test_merge_live_system_type_tag_not_kept_for_user_tasks():
    """Non-system tasks in live are never treated as system tasks."""
    live_tasks = SYSTEM_TASKS + """\

### Remind: taxes
Schedule: 2026-04-01
Added: 2026-03-12
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    result = bc.merge_heartbeat(TEMPLATE, live)
    # taxes task is a reminder — should be preserved as-is, no Type: system
    assert "Remind: taxes" in result


# ── merge_heartbeat: completed section ───────────────────────────────────────

def test_merge_completed_section_from_live():
    live_tasks = SYSTEM_TASKS + "\n## Completed\n\n- old reminder done (completed 2026-03-10)\n"
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "old reminder done" in result


def test_merge_completed_section_empty_when_not_in_live():
    live_tasks = SYSTEM_TASKS
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks  # no ## Completed
    result = bc.merge_heartbeat(TEMPLATE, live)
    assert "## Completed" in result


# ── HEARTBEAT.md write behaviour ──────────────────────────────────────────────

def test_heartbeat_written_on_first_deploy(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.delenv("HOMER_DEFAULT_MODEL", raising=False)
    bc.main()
    assert (workspace / "HEARTBEAT.md").exists()
    content = (workspace / "HEARTBEAT.md").read_text()
    assert "Gmail scan" in content
    assert "Morning briefing" in content


def test_heartbeat_system_tasks_updated_on_redeploy(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.delenv("HOMER_DEFAULT_MODEL", raising=False)
    # Simulate live file with user task + ticked system task schedule
    live_tasks = """\

### Gmail scan
Type: system
Schedule: 2026-03-12 14:00
Last-run: 2026-03-12 13:30
Recur: every 1 hour

### Morning briefing
Type: system
Schedule: 2026-03-13 07:00
Recur: every 1 day

### Remind: water plants
Schedule: 2026-03-20
Added: 2026-03-12
"""
    live = SYSTEM_PREFIX + USER_TASKS_HEADER + live_tasks + "\n## Completed\n\n"
    (workspace / "HEARTBEAT.md").write_text(live)
    bc.main()
    content = (workspace / "HEARTBEAT.md").read_text()
    assert "Gmail scan" in content
    assert "Morning briefing" in content
    assert "2026-03-12 14:00" in content      # live schedule preserved
    assert "Last-run: 2026-03-12 13:30" in content  # last-run preserved
    assert "Remind: water plants" in content  # user task preserved
    assert "2026-01-01 09:00" not in content  # template initial date NOT used


# ── WHATS_NEW / announcements ─────────────────────────────────────────────────

ANNOUNCEMENTS_TEMPLATE = (
    SYSTEM_PREFIX
    + "## Announcements\nFor each entry: send and remove.\n\n"
    + USER_TASKS_HEADER
    + SYSTEM_TASKS
    + "\n## Completed\n\n"
)

WHATS_NEW_CONTENT = """\
# What's New in Homer

---

## 2026-03-16 — Smart reminder routing
Recipients: sam:whatsapp
Reminders now fire back on the channel you used.

## 2026-03-16 — WhatsApp media support
Recipients: sam:whatsapp
I can now send images over WhatsApp.
"""

WHATS_NEW_WITH_BAD_HEADING = """\
# What's New in Homer

---

## Not a date heading
Recipients: sam:whatsapp
This should be skipped.

## 2026-03-16 — Valid entry
Recipients: primary:whatsapp
Valid message.
"""


def test_parse_whats_new_returns_entries():
    entries = bc._parse_whats_new(WHATS_NEW_CONTENT)
    assert len(entries) == 2
    assert entries[0]["key"] == "2026-03-16 — Smart reminder routing"
    assert entries[0]["title"] == "Smart reminder routing"
    assert entries[0]["recipients"] == "sam:whatsapp"
    assert "channel you used" in entries[0]["message"]


def test_parse_whats_new_skips_non_date_headings():
    entries = bc._parse_whats_new(WHATS_NEW_WITH_BAD_HEADING)
    assert len(entries) == 1
    assert entries[0]["title"] == "Valid entry"


def test_merge_heartbeat_preserves_live_announcements():
    live_ann_block = "### Smart reminder routing\nRecipients: sam:whatsapp\nMessage: Reminders route back."
    live = (
        SYSTEM_PREFIX
        + "## Announcements\nFor each entry: send and remove.\n\n"
        + live_ann_block + "\n\n"
        + USER_TASKS_HEADER
        + SYSTEM_TASKS
        + "\n## Completed\n\n"
    )
    result = bc.merge_heartbeat(ANNOUNCEMENTS_TEMPLATE, live)
    assert "Smart reminder routing" in result
    assert "Reminders route back" in result


def test_merge_heartbeat_no_live_announcements_clean():
    live = ANNOUNCEMENTS_TEMPLATE  # no ### entries in Announcements section
    result = bc.merge_heartbeat(ANNOUNCEMENTS_TEMPLATE, live)
    # No stray ### blocks between Announcements and User Tasks
    ann_pos = result.index("## Announcements")
    user_tasks_pos = result.index("## User Tasks")
    between = result[ann_pos:user_tasks_pos]
    assert "###" not in between


def test_merge_heartbeat_preserves_agentic_tasks():
    """Agentic tasks (Type: agentic) are non-system and must be preserved from live."""
    template = SYSTEM_PREFIX + USER_TASKS_HEADER + SYSTEM_TASKS + "\n## Completed\n\n"
    live = (
        SYSTEM_PREFIX + USER_TASKS_HEADER
        + SYSTEM_TASKS
        + "\n\n### Generate math report\n"
        "Type: agentic\n"
        "Schedule: 2027-06-01 08:00\n"
        "Recur: every 1 month\n"
        "Recipients: primary:whatsapp\n"
        "Goal: Read Kemi's math log and summarize\n"
        "Added: 2027-05-01\n\n"
        "## Completed\n\n"
    )
    result = bc.merge_heartbeat(template, live)
    assert "Type: agentic" in result
    assert "Generate math report" in result
    assert "Goal: Read Kemi's math log and summarize" in result
    # System tasks still present
    assert "Gmail scan" in result


def test_inject_whats_new_queues_new_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "WORKSPACE_DIR", tmp_path)
    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_text(
        "## Announcements\nFor each entry: send and remove.\n\n## User Tasks\n\n## Completed\n",
        encoding="utf-8",
    )
    wn = tmp_path / "WHATS_NEW.md"
    wn.write_text(WHATS_NEW_CONTENT, encoding="utf-8")

    bc.inject_whats_new(heartbeat, whats_new_path=wn)

    content = heartbeat.read_text()
    assert "Smart reminder routing" in content
    assert "WhatsApp media support" in content
    assert "Message:" in content


def test_inject_whats_new_skips_announced_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "WORKSPACE_DIR", tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = state_dir / "whats_new_announced.txt"
    state.write_text("2026-03-16 — Smart reminder routing\n", encoding="utf-8")

    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_text(
        "## Announcements\nFor each entry: send and remove.\n\n## User Tasks\n\n## Completed\n",
        encoding="utf-8",
    )
    wn = tmp_path / "WHATS_NEW.md"
    wn.write_text(WHATS_NEW_CONTENT, encoding="utf-8")

    bc.inject_whats_new(heartbeat, whats_new_path=wn)

    content = heartbeat.read_text()
    assert "Smart reminder routing" not in content   # already announced
    assert "WhatsApp media support" in content        # new — should be queued


def test_inject_whats_new_updates_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "WORKSPACE_DIR", tmp_path)
    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_text(
        "## Announcements\nFor each entry: send and remove.\n\n## User Tasks\n\n## Completed\n",
        encoding="utf-8",
    )
    wn = tmp_path / "WHATS_NEW.md"
    wn.write_text(WHATS_NEW_CONTENT, encoding="utf-8")

    bc.inject_whats_new(heartbeat, whats_new_path=wn)

    state = tmp_path / "state" / "whats_new_announced.txt"
    assert state.exists()
    keys = state.read_text().splitlines()
    assert "2026-03-16 — Smart reminder routing" in keys
    assert "2026-03-16 — WhatsApp media support" in keys


# ── load_active_events ─────────────────────────────────────────────────────────

class TestLoadActiveEvents:
    def test_no_events_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        assert bc.load_active_events() == ""

    def test_empty_events_dir(self, tmp_path, monkeypatch):
        edir = tmp_path / "events"
        edir.mkdir()
        monkeypatch.setattr(bc, "EVENTS_DIR", edir)
        assert bc.load_active_events() == ""

    def test_loads_active_event(self, tmp_path, monkeypatch):
        edir = tmp_path / "events" / "trip1"
        edir.mkdir(parents=True)
        (edir / "status.md").write_text("# Trip 1\nStatus: Coordinating\nDates: TBD\n")
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "events")
        result = bc.load_active_events()
        assert "# Active Events" in result
        assert "# Trip 1" in result

    def test_skips_archived_event(self, tmp_path, monkeypatch):
        edir = tmp_path / "events" / "old_trip"
        edir.mkdir(parents=True)
        (edir / "status.md").write_text("# Old Trip\nStatus: Archived\nDates: 2025-01-01\n")
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "events")
        result = bc.load_active_events()
        assert result == ""

    def test_loads_confirmed_and_active(self, tmp_path, monkeypatch):
        events = tmp_path / "events"
        for name, status in [("trip_a", "Confirmed"), ("trip_b", "Active"), ("trip_c", "Archived")]:
            d = events / name
            d.mkdir(parents=True)
            (d / "status.md").write_text(f"# {name}\nStatus: {status}\n")
        monkeypatch.setattr(bc, "EVENTS_DIR", events)
        result = bc.load_active_events()
        assert "trip_a" in result
        assert "trip_b" in result
        assert "trip_c" not in result

    def test_multiple_events_separated_by_divider(self, tmp_path, monkeypatch):
        events = tmp_path / "events"
        for name in ["trip_a", "trip_b"]:
            d = events / name
            d.mkdir(parents=True)
            (d / "status.md").write_text(f"# {name}\nStatus: Coordinating\n")
        monkeypatch.setattr(bc, "EVENTS_DIR", events)
        result = bc.load_active_events()
        assert result.count("---") >= 1  # separator between events


class TestBuildUserContextWithEvents:
    def test_events_included_in_user_context(self, tmp_path, monkeypatch):
        events = tmp_path / "events"
        d = events / "mtb"
        d.mkdir(parents=True)
        (d / "status.md").write_text("# MTB Trip\nStatus: Coordinating\n")
        monkeypatch.setattr(bc, "EVENTS_DIR", events)
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "# Active Events" in result
        assert "# MTB Trip" in result

    def test_no_events_section_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "Active Events" not in result


class TestLoadPendingReplies:
    def test_pending_replies_injected_into_user_context(self, tmp_path, monkeypatch):
        import json as _json
        pending = [
            {"id": "abc-123", "from": "sam", "topic": "weekend plans",
             "notify_channel": "whatsapp", "notify_recipient": "111@s.whatsapp.net",
             "created_at": "2026-03-29T10:00:00+00:00"},
        ]
        (tmp_path / "pending_replies.json").write_text(_json.dumps(pending))
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "Pending Follow-ups" in result
        assert "sam" in result
        assert "weekend plans" in result
        assert "abc-123" in result

    def test_no_pending_section_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "Pending Follow-ups" not in result

    def test_no_pending_section_when_empty_list(self, tmp_path, monkeypatch):
        import json as _json
        (tmp_path / "pending_replies.json").write_text(_json.dumps([]))
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "Pending Follow-ups" not in result

    def test_no_pending_section_when_corrupt_json(self, tmp_path, monkeypatch):
        (tmp_path / "pending_replies.json").write_text("not valid json")
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "Pending Follow-ups" not in result

    def test_multiple_pending_entries_all_shown(self, tmp_path, monkeypatch):
        import json as _json
        pending = [
            {"id": "id-1", "from": "sam", "topic": "weekend plans",
             "notify_channel": "whatsapp", "notify_recipient": "111@s.whatsapp.net",
             "created_at": "2026-03-29T10:00:00+00:00"},
            {"id": "id-2", "from": "alex", "topic": "doctor appointment",
             "notify_channel": "telegram", "notify_recipient": "9876",
             "created_at": "2026-03-29T11:00:00+00:00"},
        ]
        (tmp_path / "pending_replies.json").write_text(_json.dumps(pending))
        monkeypatch.setattr(bc, "CONTEXT_DIR", tmp_path)
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(bc, "load_file", lambda name: f"# {name}\n")
        result = bc.build_user_context()
        assert "sam" in result
        assert "alex" in result
        assert "doctor appointment" in result


# ── build_guest_agent_workspace ───────────────────────────────────────────────

class TestBuildGuestAgentWorkspace:
    def _setup(self, tmp_path, monkeypatch):
        """Common setup: create events, guest_agent templates, and wire monkeypatches."""
        events = tmp_path / "events"
        d = events / "trip1"
        d.mkdir(parents=True)
        (d / "status.md").write_text("# Trip 1\nStatus: Coordinating\n")

        guest_agent_ws = tmp_path / "workspace" / "guest_agent"
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "GUEST_AGENT_SOUL.md").write_text("# Guest Agent Soul\n")
        (agent_dir / "GUEST_AGENT.md").write_text("# Guest Agent Instructions\nWorkspace: {HOMER_WORKSPACE}\n")

        monkeypatch.setattr(bc, "EVENTS_DIR", events)
        monkeypatch.setattr(bc, "GUEST_AGENT_WORKSPACE_DIR", guest_agent_ws)
        monkeypatch.setattr(bc, "WORKSPACE_DIR", workspace)
        monkeypatch.setattr(bc, "GUEST_AGENT_SOUL_PATH", agent_dir / "GUEST_AGENT_SOUL.md")
        monkeypatch.setattr(bc, "GUEST_AGENT_AGENTS_PATH", agent_dir / "GUEST_AGENT.md")
        return guest_agent_ws, events

    def test_builds_guest_agent_workspace(self, tmp_path, monkeypatch):
        guest_agent_ws, _ = self._setup(tmp_path, monkeypatch)
        bc.build_guest_agent_workspace()
        assert (guest_agent_ws / "SOUL.md").exists()
        assert (guest_agent_ws / "AGENTS.md").exists()
        assert (guest_agent_ws / "USER.md").exists()
        assert (guest_agent_ws / "sessions").exists()

    def test_guest_agent_user_md_is_stub_no_scope_data(self, tmp_path, monkeypatch):
        """USER.md must contain NO scope sections, participants, tasks, or pending
        replies — scope context is injected per-turn by nanobot.
        """
        guest_agent_ws, _ = self._setup(tmp_path, monkeypatch)
        bc.build_guest_agent_workspace()
        user = (guest_agent_ws / "USER.md").read_text()
        # Stub marker
        assert "Guest Agent Context" in user
        assert "injected per-turn" in user
        # Absolutely no scope data
        assert "## Scope:" not in user
        assert "### Context" not in user
        assert "### Conversation History" not in user
        assert "### Pending Follow-ups" not in user
        assert "Disclosure rules" not in user
        # No household data either
        assert "# household" not in user.lower()
        assert "household.md" not in user.lower()

    def test_guest_agent_agents_has_template_vars(self, tmp_path, monkeypatch):
        guest_agent_ws, _ = self._setup(tmp_path, monkeypatch)
        bc.build_guest_agent_workspace()
        agents = (guest_agent_ws / "AGENTS.md").read_text()
        # Template var should be resolved, not literal
        assert "{HOMER_WORKSPACE}" not in agents

    def test_guest_agent_workspace_built_regardless_of_events(self, tmp_path, monkeypatch):
        """Workspace is always built — guest agent is scope-based, not event-based."""
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "nonexistent")
        guest_agent_ws = tmp_path / "workspace" / "guest_agent"
        monkeypatch.setattr(bc, "GUEST_AGENT_WORKSPACE_DIR", guest_agent_ws)
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "GUEST_AGENT_SOUL.md").write_text("# Soul\n")
        (agent_dir / "GUEST_AGENT.md").write_text("# Agents\n")
        monkeypatch.setattr(bc, "GUEST_AGENT_SOUL_PATH", agent_dir / "GUEST_AGENT_SOUL.md")
        monkeypatch.setattr(bc, "GUEST_AGENT_AGENTS_PATH", agent_dir / "GUEST_AGENT.md")
        bc.build_guest_agent_workspace()
        # Workspace should exist even with no events — scope store is the source of truth
        assert guest_agent_ws.exists()
        assert (guest_agent_ws / "USER.md").exists()

    def test_no_guest_agent_workspace_when_no_templates(self, tmp_path, monkeypatch):
        events = tmp_path / "events" / "trip"
        events.mkdir(parents=True)
        (events / "status.md").write_text("# Trip\nStatus: Coordinating\n")
        monkeypatch.setattr(bc, "EVENTS_DIR", tmp_path / "events")
        monkeypatch.setattr(bc, "GUEST_AGENT_SOUL_PATH", tmp_path / "nonexistent_soul.md")
        monkeypatch.setattr(bc, "GUEST_AGENT_AGENTS_PATH", tmp_path / "nonexistent_agents.md")
        guest_agent_ws = tmp_path / "workspace" / "guest_agent"
        monkeypatch.setattr(bc, "GUEST_AGENT_WORKSPACE_DIR", guest_agent_ws)
        bc.build_guest_agent_workspace()
        assert not guest_agent_ws.exists()

    def test_no_acl_data_in_user_md(self, tmp_path, monkeypatch):
        """Legacy ACL participant data must NOT appear in USER.md — scope context
        is nanobot-injected only. ACL is used for routing (sender_map) but never
        rendered into the guest's loaded context.
        """
        guest_agent_ws, events = self._setup(tmp_path, monkeypatch)
        acl = {"15551234567@s.whatsapp.net": {"name": "Jake", "event_id": "trip1"}}
        acl_path = events / "guest_agent_acl.json"
        acl_path.write_text(json.dumps(acl))
        monkeypatch.setattr(bc, "GUEST_AGENT_ACL_FILE", acl_path)
        monkeypatch.setenv("HOMER_SCOPE_DB", str(tmp_path / "nonexistent_scopes.db"))
        bc.build_guest_agent_workspace()
        user = (guest_agent_ws / "USER.md").read_text()
        assert "Jake" not in user
        assert "15551234567" not in user

    def test_workspace_persists_when_events_archived(self, tmp_path, monkeypatch):
        """Archiving events does not remove the guest workspace — scopes may still be active."""
        guest_agent_ws, events = self._setup(tmp_path, monkeypatch)
        bc.build_guest_agent_workspace()
        assert guest_agent_ws.exists()
        # Archive the event — workspace should still exist
        (events / "trip1" / "status.md").write_text("# Trip 1\nStatus: Archived\n")
        bc.build_guest_agent_workspace()
        assert guest_agent_ws.exists()

    def test_copies_guest_skills_to_workspace(self, tmp_path, monkeypatch):
        guest_agent_ws, _ = self._setup(tmp_path, monkeypatch)
        # Create a skill with a guest/SKILL.md
        skills_dir = tmp_path / "skills" / "event-management" / "guest"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("## Event Guest Skill\nSome skill instructions.\n")
        monkeypatch.setattr(bc, "REPO_ROOT", tmp_path)
        bc.build_guest_agent_workspace()
        skill_dst = guest_agent_ws / "skills" / "event-management" / "SKILL.md"
        assert skill_dst.exists()
        assert "Event Guest Skill" in skill_dst.read_text()

    def test_no_guest_skills_copied_when_none_exist(self, tmp_path, monkeypatch):
        guest_agent_ws, _ = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bc, "REPO_ROOT", tmp_path)
        bc.build_guest_agent_workspace()
        assert not (guest_agent_ws / "skills").exists()


# ── _build_sender_map ───────────────────────────────────────────────────────

class TestBuildSenderMap:
    @pytest.fixture(autouse=True)
    def _no_lid_map(self, monkeypatch):
        """Isolate tests from any real lid_map.json on disk."""
        monkeypatch.setattr(bc, "_load_lid_map", lambda: {})

    def test_maps_phone_from_scopes(self):
        scopes = [
            {
                "participants": [
                    {"name": "Emeka", "party_id": "14125550002@s.whatsapp.net", "handle": "14125550002@s.whatsapp.net"},
                ]
            }
        ]
        result = bc._build_sender_map(scopes, {})
        assert result["14125550002"] == "Emeka"

    def test_maps_lid_from_acl(self):
        acl = {
            "14125550002@s.whatsapp.net": {
                "name": "Emeka",
                "phone": "+14125550002",
                "lid": "914125550002",
            }
        }
        result = bc._build_sender_map([], acl)
        assert result["14125550002"] == "Emeka"
        assert result["914125550002"] == "Emeka"

    def test_maps_both_phone_and_lid(self):
        scopes = [
            {
                "participants": [
                    {"name": "Wale", "party_id": "14125550003@s.whatsapp.net", "handle": "14125550003@s.whatsapp.net"},
                ]
            }
        ]
        acl = {
            "14125550003@s.whatsapp.net": {
                "name": "Wale",
                "lid": "914125550003",
            }
        }
        result = bc._build_sender_map(scopes, acl)
        assert result["14125550003"] == "Wale"
        assert result["914125550003"] == "Wale"

    def test_empty_inputs(self):
        result = bc._build_sender_map([], {})
        assert result == {}

    def test_skips_telegram_participants(self):
        acl = {
            "tg:123456": {
                "name": "TelegramUser",
                "telegram_id": "123456",
            }
        }
        result = bc._build_sender_map([], acl)
        # Telegram entries should not produce phone-style mappings
        assert "123456" not in result

    def test_multiple_guests(self):
        scopes = [
            {
                "participants": [
                    {"name": "Emeka", "party_id": "14125550002@s.whatsapp.net", "handle": "14125550002@s.whatsapp.net"},
                    {"name": "Wale", "party_id": "14125550003@s.whatsapp.net", "handle": "14125550003@s.whatsapp.net"},
                ]
            }
        ]
        acl = {
            "14125550002@s.whatsapp.net": {"name": "Emeka", "lid": "914125550002"},
            "14125550003@s.whatsapp.net": {"name": "Wale", "lid": "914125550003"},
        }
        result = bc._build_sender_map(scopes, acl)
        assert len(result) == 4  # 2 phones + 2 LIDs
        assert result["14125550002"] == "Emeka"
        assert result["914125550002"] == "Emeka"
        assert result["14125550003"] == "Wale"
        assert result["914125550003"] == "Wale"

    def test_acl_without_lid_only_maps_phone(self):
        acl = {
            "14125550002@s.whatsapp.net": {
                "name": "Emeka",
                "phone": "+14125550002",
            }
        }
        result = bc._build_sender_map([], acl)
        assert result == {"14125550002": "Emeka"}

    def test_lid_map_cross_references_phone_to_name(self, monkeypatch):
        """lid_map has LID→phone, sender_map has phone→name → LID gets name."""
        acl = {
            "14125550002@s.whatsapp.net": {"name": "Emeka"},
        }
        # Override autouse fixture with a real lid_map
        monkeypatch.setattr(bc, "_load_lid_map", lambda: {
            "914125550002": {"phone": "14125550002"},
        })
        result = bc._build_sender_map([], acl)
        assert result["14125550002"] == "Emeka"
        assert result["914125550002"] == "Emeka"

    def test_lid_map_with_name_takes_precedence(self, monkeypatch):
        """lid_map entry with name set directly doesn't need cross-reference."""
        monkeypatch.setattr(bc, "_load_lid_map", lambda: {
            "914125550002": {"phone": "14125550002", "name": "Emeka Direct"},
        })
        result = bc._build_sender_map([], {})
        assert result["914125550002"] == "Emeka Direct"


class TestUpdateGuestConfigAllowFrom:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "guest_config.json"
        cfg_path.write_text(json.dumps({
            "channels": {
                "whatsapp": {"allow_from": [], "enabled": False},
                "telegram": {"allowFrom": [], "enabled": False},
                "email": {"allowFrom": [], "enabled": False},
            }
        }))
        monkeypatch.setattr(bc, "GUEST_NANOBOT_CONFIG_PATH", cfg_path)
        monkeypatch.setattr(bc, "GUEST_AGENT_ACL_FILE", tmp_path / "nonexistent_acl.json")
        monkeypatch.setattr(bc, "_load_lid_map", lambda: {})
        self.cfg_path = cfg_path

    def test_email_participant_not_added_to_whatsapp_allow_from(self, monkeypatch):
        """Regression: email participant_ids like dbblair8@gmail.com must not
        leak into WhatsApp allow_from (bug where split('@')[0] produced 'dbblair8')."""
        # update_guest_config_allow_from() does `import scope_store` after
        # inserting HOMER_TOOLS into sys.path, so we patch both module names.
        import tools.scope_store as ss_pkg
        import sys
        sys.path.insert(0, bc.HOMER_TOOLS)
        import scope_store as ss_bare
        for mod in (ss_pkg, ss_bare):
            monkeypatch.setattr(mod, "get_all_active_participant_ids",
                                lambda: ["14125550002@s.whatsapp.net", "dbblair8@gmail.com", "tg:999"])
            monkeypatch.setattr(mod, "get_all_active_email_addresses",
                                lambda: ["dbblair8@gmail.com"])

        bc.update_guest_config_allow_from()

        cfg = json.loads(self.cfg_path.read_text())
        wa = cfg["channels"]["whatsapp"]["allow_from"]
        assert "dbblair8" not in wa
        assert "dbblair8@gmail.com" not in wa
        assert wa == ["14125550002"]
        assert cfg["channels"]["telegram"]["allowFrom"] == ["999"]
        assert cfg["channels"]["email"]["allowFrom"] == ["dbblair8@gmail.com"]

    def test_acl_fallback_also_filters_email(self, monkeypatch, tmp_path):
        """When scope_store returns nothing, the ACL fallback must not leak
        email keys into WhatsApp allow_from either."""
        import tools.scope_store as ss_pkg
        import sys
        sys.path.insert(0, bc.HOMER_TOOLS)
        import scope_store as ss_bare
        for mod in (ss_pkg, ss_bare):
            monkeypatch.setattr(mod, "get_all_active_participant_ids", lambda: [])
            monkeypatch.setattr(mod, "get_all_active_email_addresses", lambda: [])

        acl_path = tmp_path / "acl.json"
        acl_path.write_text(json.dumps({
            "14125550002@s.whatsapp.net": {"name": "A"},
            "vendor@example.com": {"name": "B", "channel": "email"},
            "tg:42": {"name": "C", "channel": "telegram"},
        }))
        monkeypatch.setattr(bc, "GUEST_AGENT_ACL_FILE", acl_path)

        bc.update_guest_config_allow_from()

        cfg = json.loads(self.cfg_path.read_text())
        wa = cfg["channels"]["whatsapp"]["allow_from"]
        assert "vendor" not in wa
        assert wa == ["14125550002"]


# ── stamp_heartbeat_model: default-tier heartbeat model defaulting ──────────

STAMP_TEMPLATE = (
    SYSTEM_PREFIX
    + USER_TASKS_HEADER
    + """\

### Gmail scan
Type: system
Schedule: 2026-01-01 09:00
Recur: every 1 hour

### Remind: vitamins
Schedule: 2026-03-20 09:00
Recur: every 1 day
Recipients: primary:whatsapp
Added: 2026-03-12

### Remind: already-set
Schedule: 2026-04-01
Recipients: primary:whatsapp
Model: pro
Added: 2026-03-12
"""
    + "\n## Completed\n- old task (completed 2026-03-01)\n\n"
)


def test_stamp_heartbeat_model_adds_to_task_missing_model():
    out = bc.stamp_heartbeat_model(STAMP_TEMPLATE, "default-cheap")
    # Reminder task without Model gains one after Schedule
    assert "Schedule: 2026-03-20 09:00\nModel: default-cheap" in out
    # System task without Model also gains one
    assert "Schedule: 2026-01-01 09:00\nModel: default-cheap" in out


def test_stamp_heartbeat_model_preserves_existing_model():
    out = bc.stamp_heartbeat_model(STAMP_TEMPLATE, "default-cheap")
    # Task that already declared Model: pro is unchanged
    assert "Model: pro" in out
    # And not double-stamped
    assert out.count("Model: default-cheap\nModel:") == 0
    # Total Model occurrences == initial 1 (pro) + Gmail scan + vitamins = 3
    assert out.count("\nModel:") == 3


def test_stamp_heartbeat_model_idempotent():
    once = bc.stamp_heartbeat_model(STAMP_TEMPLATE, "default-cheap")
    twice = bc.stamp_heartbeat_model(once, "default-cheap")
    assert once == twice


def test_stamp_heartbeat_model_leaves_completed_section_alone():
    out = bc.stamp_heartbeat_model(STAMP_TEMPLATE, "default-cheap")
    # Completed section content untouched
    assert "- old task (completed 2026-03-01)" in out
    # No Model: stamp leaked into the Completed bullets
    completed_idx = out.index("## Completed")
    assert "Model:" not in out[completed_idx:]


def test_resolve_heartbeat_model_default_tier_with_preset(monkeypatch):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "default-cheap")
    assert bc._resolve_heartbeat_model_default() == "default-cheap"


def test_resolve_heartbeat_model_byok_returns_none(monkeypatch):
    monkeypatch.setenv("HOMER_MODEL_TIER", "byok")
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "default-cheap")
    assert bc._resolve_heartbeat_model_default() is None


def test_resolve_heartbeat_model_default_tier_unset_preset(monkeypatch):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.delenv("HOMER_HEARTBEAT_MODEL", raising=False)
    assert bc._resolve_heartbeat_model_default() is None


def test_resolve_heartbeat_model_default_tier_blank_preset(monkeypatch):
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "   ")
    assert bc._resolve_heartbeat_model_default() is None


def test_resolve_heartbeat_model_managed_returns_none(monkeypatch):
    monkeypatch.setenv("HOMER_MODEL_TIER", "managed")
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "default-cheap")
    assert bc._resolve_heartbeat_model_default() is None


def test_resolve_heartbeat_model_no_tier_returns_none(monkeypatch):
    monkeypatch.delenv("HOMER_MODEL_TIER", raising=False)
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "default-cheap")
    assert bc._resolve_heartbeat_model_default() is None


def test_main_stamps_heartbeat_on_default_tier(workspace, tmp_path, monkeypatch):
    """End-to-end: build_context.main() injects Model on default tier."""
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.delenv("HOMER_DEFAULT_MODEL", raising=False)
    monkeypatch.setattr(bc, "HEARTBEAT_CONTENT", STAMP_TEMPLATE)
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "default-cheap")
    bc.main()
    hb = (workspace / "HEARTBEAT.md").read_text()
    assert "Model: default-cheap" in hb
    # Existing Model: pro on the third task survives
    assert "Model: pro" in hb


def test_main_does_not_stamp_on_byok_tier(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.delenv("HOMER_DEFAULT_MODEL", raising=False)
    monkeypatch.setattr(bc, "HEARTBEAT_CONTENT", STAMP_TEMPLATE)
    monkeypatch.setenv("HOMER_MODEL_TIER", "byok")
    monkeypatch.setenv("HOMER_HEARTBEAT_MODEL", "default-cheap")
    bc.main()
    hb = (workspace / "HEARTBEAT.md").read_text()
    assert "Model: default-cheap" not in hb


def test_main_does_not_stamp_when_preset_unset(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.delenv("HOMER_DEFAULT_MODEL", raising=False)
    monkeypatch.setattr(bc, "HEARTBEAT_CONTENT", STAMP_TEMPLATE)
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")
    monkeypatch.delenv("HOMER_HEARTBEAT_MODEL", raising=False)
    bc.main()
    hb = (workspace / "HEARTBEAT.md").read_text()
    assert "Model: default-cheap" not in hb
    # The pre-existing Model: pro line survives
    assert "Model: pro" in hb


# ── fresh-tenant Last-run stamping ────────────────────────────────────────────
# Background: when a tenant is freshly provisioned, build_context.py writes
# HEARTBEAT.md from the template — which ships fixed-anchor Schedules for
# recurring system tasks (e.g. `2026-01-01 07:00` for the daily morning
# briefing). Nanobot's heartbeat treats "past Schedule + missing Last-run"
# as past-due, so on the first heartbeat tick after provisioning EVERY
# recurring system task fires regardless of local time. The user observed
# this as Homer sending a "Good morning!" briefing at 10pm immediately
# after onboarding. Stamping Last-run = now during fresh-workspace bootstrap
# makes a fresh tenant look like an existing one that just ticked the task.

def test_main_stamps_last_run_on_fresh_workspace(workspace, tmp_path, monkeypatch):
    """Fresh workspace (no live HEARTBEAT.md) gets Last-run stamped on
    every system task that lacks one — so the months-old Schedule anchor
    doesn't fire on the first heartbeat tick after provisioning."""
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    bc.main()
    hb = (workspace / "HEARTBEAT.md").read_text()
    # Both system tasks in the template should now carry Last-run.
    morning_block = re.search(
        r"### Morning briefing\n(?:.*\n)*?(?=\n###|\n##\s|\Z)",
        hb,
    )
    assert morning_block is not None
    assert "Last-run:" in morning_block.group(0)
    gmail_block = re.search(
        r"### Gmail scan\n(?:.*\n)*?(?=\n###|\n##\s|\Z)",
        hb,
    )
    assert gmail_block is not None
    assert "Last-run:" in gmail_block.group(0)


def test_main_does_not_overwrite_existing_last_run(workspace, tmp_path, monkeypatch):
    """An existing HEARTBEAT.md (live tenant restart) must keep its real
    Last-run values. The bootstrap stamp only fires when the file is
    being created from scratch."""
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    live = (
        SYSTEM_PREFIX
        + USER_TASKS_HEADER
        + "\n### Morning briefing\nType: system\n"
        "Schedule: 2026-01-01 07:00\nLast-run: 2026-05-09 07:00\n"
        "Recur: every 1 day\n"
        "\n### Gmail scan\nType: system\n"
        "Schedule: 2026-01-01 09:00\nLast-run: 2026-05-09 21:00\n"
        "Recur: every 1 hour\n"
        + "\n## Completed\n\n"
    )
    (workspace / "HEARTBEAT.md").write_text(live, encoding="utf-8")
    bc.main()
    hb = (workspace / "HEARTBEAT.md").read_text()
    assert "Last-run: 2026-05-09 07:00" in hb
    assert "Last-run: 2026-05-09 21:00" in hb


def test_stamp_aligns_with_schedule_grid_not_now():
    """The smart stamp lands Last-run on the schedule's own recurrence grid
    (most-recent-past occurrence), not on wall-clock now. This is what makes
    the morning brief fire at tomorrow 7am instead of tomorrow at the
    onboarding time."""
    from datetime import datetime

    block = (
        "### Morning briefing\n"
        "Type: system\n"
        "Schedule: 2026-01-01 07:00\n"
        "Recur: every 1 day\n"
    )
    stamped = bc._stamp_last_run_for_fresh_system_tasks(
        block, now=datetime(2026, 5, 9, 22, 16),
    )
    # The most recent 07:00 occurrence at-or-before 2026-05-09 22:16 is
    # 2026-05-09 07:00. Stamping that means Last-run + 1 day = tomorrow 07:00,
    # which is exactly when the brief should next fire.
    assert "Last-run: 2026-05-09 07:00" in stamped


def test_stamp_anchors_first_fire_when_schedule_is_future():
    """When Schedule is in the future (e.g. tenant provisioned before the
    template anchor date), the first fire should still land exactly on
    Schedule. Stamping Last-run = Schedule - Recur achieves that:
    Last-run + Recur = Schedule, so effective_due == Schedule."""
    from datetime import datetime

    block = (
        "### Morning briefing\n"
        "Type: system\n"
        "Schedule: 2030-06-15 07:00\n"
        "Recur: every 1 day\n"
    )
    stamped = bc._stamp_last_run_for_fresh_system_tasks(
        block, now=datetime(2030, 6, 1, 12, 0),
    )
    # Schedule - 1 day = 2030-06-14 07:00
    assert "Last-run: 2030-06-14 07:00" in stamped


def test_stamp_aligns_30min_recur_to_grid():
    """Same logic for sub-hour recurs: a 30-minute escalation check
    onboarded at 22:16 should land on the previous :00 or :30 boundary."""
    from datetime import datetime

    block = (
        "### Check escalations\n"
        "Type: system\n"
        "Schedule: 2026-01-01 00:00\n"
        "Recur: every 30 minutes\n"
    )
    stamped = bc._stamp_last_run_for_fresh_system_tasks(
        block, now=datetime(2026, 5, 9, 22, 16),
    )
    # 22:16 floors to 22:00 on the 30-minute grid anchored at 00:00.
    assert "Last-run: 2026-05-09 22:00" in stamped


def test_main_does_not_stamp_reminders(workspace, tmp_path, monkeypatch):
    """Only system tasks get the bootstrap Last-run stamp. Reminders
    (no `Type:` line) intentionally rely on past-Schedule firing as a
    catch-up semantic — stamping them would silence overdue reminders."""
    monkeypatch.setattr(bc, "NANOBOT_CONFIG_PATH", tmp_path / "nonexistent.json")
    template_with_reminder = (
        SYSTEM_PREFIX
        + USER_TASKS_HEADER
        + "\n### Morning briefing\nType: system\n"
        "Schedule: 2026-01-01 07:00\nRecur: every 1 day\n"
        "\n### Remind: call the dentist\n"
        "Schedule: 2026-04-21 10:00\nRecipients: primary:whatsapp\n"
        + "\n## Completed\n\n"
    )
    monkeypatch.setattr(bc, "HEARTBEAT_CONTENT", template_with_reminder)
    bc.main()
    hb = (workspace / "HEARTBEAT.md").read_text()
    reminder_block = re.search(
        r"### Remind: call the dentist\n(?:.*\n)*?(?=\n###|\n##\s|\Z)",
        hb,
    )
    assert reminder_block is not None
    assert "Last-run:" not in reminder_block.group(0)

"""Tests for bootstrap_user_briefs.py — recipient-driven brief.md backfill."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import bootstrap_user_briefs as bub


SAMPLE_HEARTBEAT = """\
# Heartbeat Tasks

## User Tasks

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
Recipients: primary:whatsapp,seun:whatsapp
Prompt-file: users/{recipient}.brief.md

### Check escalations
Type: system
Recipients: ops:slack
"""


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    template = tmp_path / "skills" / "morning-brief" / "default.brief.md"
    template.parent.mkdir(parents=True)
    template.write_text("# Default brief template\n", encoding="utf-8")
    workspace = tmp_path / "context" / ".nanobot_workspace"
    users_dir = workspace / "users"
    heartbeat = workspace / "HEARTBEAT.md"
    workspace.mkdir(parents=True)
    heartbeat.write_text(SAMPLE_HEARTBEAT, encoding="utf-8")
    monkeypatch.setattr(bub, "TEMPLATE", template)
    monkeypatch.setattr(bub, "USERS_DIR", users_dir)
    monkeypatch.setattr(bub, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(bub, "HEARTBEAT_FILE", heartbeat)
    return tmp_path


class TestParseRecipients:
    def test_extracts_brief_recipients_only(self):
        # Check escalations also has Recipients, but we only want Morning briefing's
        assert bub.parse_brief_recipients(SAMPLE_HEARTBEAT) == ["primary", "seun"]

    def test_strips_channel_suffix(self):
        text = "### Morning briefing\nRecipients: alex:whatsapp,bob:telegram\n"
        assert bub.parse_brief_recipients(text) == ["alex", "bob"]

    def test_dedups_preserving_order(self):
        text = "### Morning briefing\nRecipients: alex:whatsapp,alex:telegram,bob:whatsapp\n"
        assert bub.parse_brief_recipients(text) == ["alex", "bob"]

    def test_ignores_empty_entries(self):
        text = "### Morning briefing\nRecipients: alex:whatsapp, ,bob:whatsapp\n"
        assert bub.parse_brief_recipients(text) == ["alex", "bob"]

    def test_no_morning_briefing_block_returns_empty(self):
        text = "### Check escalations\nRecipients: ops:slack\n"
        assert bub.parse_brief_recipients(text) == []

    def test_morning_briefing_without_recipients_returns_empty(self):
        text = "### Morning briefing\nType: system\nSchedule: X\n"
        assert bub.parse_brief_recipients(text) == []


class TestBootstrap:
    def test_creates_missing_file(self, isolated_paths):
        result = bub.bootstrap_recipient("primary")
        assert result["status"] == "created"
        target = isolated_paths / "context" / ".nanobot_workspace" / "users" / "primary.brief.md"
        assert target.exists()
        assert "Default brief template" in target.read_text()

    def test_preserves_existing_file(self, isolated_paths):
        target = isolated_paths / "context" / ".nanobot_workspace" / "users" / "primary.brief.md"
        target.parent.mkdir(parents=True)
        target.write_text("Custom edits — keep me", encoding="utf-8")
        result = bub.bootstrap_recipient("primary")
        assert result["status"] == "exists"
        assert target.read_text() == "Custom edits — keep me"

    def test_template_missing_returns_error(self, isolated_paths, monkeypatch):
        monkeypatch.setattr(bub, "TEMPLATE", isolated_paths / "does-not-exist.md")
        result = bub.bootstrap_recipient("primary")
        assert result["status"] == "error"
        assert "template missing" in result["error"]

    def test_rejects_path_separator_in_recipient(self, isolated_paths):
        result = bub.bootstrap_recipient("evil/path")
        assert result["status"] == "error"
        assert "'/'" in result["error"]

    def test_rejects_dotdot_in_recipient(self, isolated_paths):
        result = bub.bootstrap_recipient("..")
        assert result["status"] == "error"
        assert ".." in result["error"]


class TestCLI:
    def test_default_walks_heartbeat_recipients(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 2
        assert {r["recipient"] for r in out["results"]} == {"primary", "seun"}

    def test_idempotent_second_run(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        bub.main()
        capsys.readouterr()
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["created"] == 0
        assert out["exists"] == 2

    def test_explicit_recipient(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["bootstrap_user_briefs.py", "--recipient", "alex"])
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 1
        assert out["results"][0]["recipient"] == "alex"
        assert out["results"][0]["status"] == "created"

    def test_no_heartbeat_file_errors(self, monkeypatch, capsys):
        bub.HEARTBEAT_FILE.unlink()
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        with pytest.raises(SystemExit):
            bub.main()
        out = json.loads(capsys.readouterr().out)
        assert "HEARTBEAT.md not found" in out["error"]

    def test_heartbeat_without_brief_recipients_errors(self, monkeypatch, capsys):
        bub.HEARTBEAT_FILE.write_text(
            "### Check escalations\nType: system\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        with pytest.raises(SystemExit):
            bub.main()
        out = json.loads(capsys.readouterr().out)
        assert "No Morning briefing recipients" in out["error"]

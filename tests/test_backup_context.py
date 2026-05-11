"""Tests for backup_context.py."""

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from tools.backup_context import create_zip, should_exclude, prune_old_backups, upload_backup


@pytest.fixture
def fake_context(tmp_path):
    """Create a fake context directory mimicking the real layout."""
    ctx = tmp_path / "context"
    # user_context
    uc = ctx / "user_context"
    uc.mkdir(parents=True)
    (uc / "household.md").write_text("# Household")
    (uc / "property.md").write_text("# Property")
    # users.yaml
    (ctx / "users.yaml").write_text("users:\n  - name: Alice\n    role: admin\n")
    # events
    events = ctx / "events" / "trip_1"
    events.mkdir(parents=True)
    (events / "status.md").write_text("# Trip")
    # scopes.db
    (ctx / "scopes.db").write_bytes(b"\x00" * 100)
    # workspace files
    ws = ctx / ".nanobot_workspace"
    (ws / "state").mkdir(parents=True)
    (ws / "SOUL.md").write_text("# Soul")
    (ws / "state" / "payee_labels.json").write_text("{}")
    # sessions (should be excluded)
    sess = ws / "sessions"
    sess.mkdir()
    (sess / "telegram_123.jsonl").write_text('{"role":"user"}')
    # skills (should be excluded)
    skills = ws / "skills" / "weather"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Weather")
    # guest workspace sessions (should be excluded)
    gws = ctx / ".guest_workspace" / "sessions"
    gws.mkdir(parents=True)
    (gws / "whatsapp_456.jsonl").write_text('{"role":"user"}')
    return ctx


class TestShouldExclude:
    def test_excludes_sessions(self):
        assert should_exclude(".nanobot_workspace/sessions/telegram.jsonl")
        assert should_exclude(".guest_workspace/sessions/whatsapp.jsonl")

    def test_excludes_skills(self):
        assert should_exclude(".nanobot_workspace/skills/weather/SKILL.md")

    def test_includes_user_context(self):
        assert not should_exclude("user_context/household.md")

    def test_includes_state(self):
        assert not should_exclude(".nanobot_workspace/state/payee_labels.json")

    def test_includes_users_yaml(self):
        assert not should_exclude("users.yaml")

    def test_includes_events(self):
        assert not should_exclude("events/trip_1/status.md")


class TestCreateZip:
    def test_creates_valid_zip(self, fake_context, tmp_path):
        out = str(tmp_path / "backup.zip")
        path, size_mb = create_zip(fake_context, out)
        assert os.path.exists(path)
        assert size_mb >= 0

    def test_excludes_sessions(self, fake_context, tmp_path):
        out = str(tmp_path / "backup.zip")
        create_zip(fake_context, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert not any("sessions/" in n for n in names)

    def test_excludes_skills(self, fake_context, tmp_path):
        out = str(tmp_path / "backup.zip")
        create_zip(fake_context, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert not any("skills/" in n for n in names)

    def test_includes_context_files(self, fake_context, tmp_path):
        out = str(tmp_path / "backup.zip")
        create_zip(fake_context, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "user_context/household.md" in names
        assert "user_context/property.md" in names
        assert "users.yaml" in names
        assert "events/trip_1/status.md" in names
        assert ".nanobot_workspace/state/payee_labels.json" in names


class TestUploadBackup:
    def test_uploads_with_folder_id(self, tmp_path):
        zip_path = str(tmp_path / "test.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "hello")

        mock_service = MagicMock()
        mock_service.files().create().execute.return_value = {"id": "abc123", "name": "test.zip"}

        result = upload_backup(mock_service, zip_path, "folder_xyz")
        assert result["id"] == "abc123"

    def test_no_public_permission(self, tmp_path):
        zip_path = str(tmp_path / "test.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "hello")

        mock_service = MagicMock()
        mock_service.files().create().execute.return_value = {"id": "abc", "name": "test.zip"}

        upload_backup(mock_service, zip_path, "folder_xyz")
        # Verify no permissions().create() call (no public sharing)
        mock_service.permissions.assert_not_called()


class TestPruneOldBackups:
    def test_prunes_oldest(self):
        mock_service = MagicMock()
        files = [{"id": f"id_{i}", "name": f"backup_{i}.zip", "createdTime": f"2026-03-{i:02d}"}
                 for i in range(1, 11)]
        mock_service.files().list().execute.return_value = {"files": files}

        pruned = prune_old_backups(mock_service, "folder_xyz", retain=7)
        assert pruned == 3

    def test_noop_when_under_limit(self):
        mock_service = MagicMock()
        files = [{"id": f"id_{i}", "name": f"backup_{i}.zip"} for i in range(5)]
        mock_service.files().list().execute.return_value = {"files": files}

        pruned = prune_old_backups(mock_service, "folder_xyz", retain=7)
        assert pruned == 0
        mock_service.files().delete.assert_not_called()

    def test_prunes_exactly_right_amount(self):
        mock_service = MagicMock()
        files = [{"id": f"id_{i}", "name": f"backup_{i}.zip"} for i in range(7)]
        mock_service.files().list().execute.return_value = {"files": files}

        pruned = prune_old_backups(mock_service, "folder_xyz", retain=7)
        assert pruned == 0

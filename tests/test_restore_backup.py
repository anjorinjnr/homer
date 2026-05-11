"""Tests for restore_backup.py."""

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.restore_backup import list_backups, download_file, extract_backup
import tools.restore_backup as restore_mod


@pytest.fixture(autouse=True)
def _allow_tmp_paths(tmp_path, monkeypatch):
    """Allow tests to extract to pytest tmp dirs by relaxing the safe parent check."""
    monkeypatch.setattr(restore_mod, "WORKSPACE_DIR", tmp_path)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_drive_files():
    """Sample Drive file listing (newest first)."""
    return [
        {
            "id": "id_3",
            "name": "homer_backup_2026-03-30_0200.zip",
            "createdTime": "2026-03-30T02:00:00Z",
            "size": str(150 * 1024),  # 150 KB
        },
        {
            "id": "id_2",
            "name": "homer_backup_2026-03-29_0200.zip",
            "createdTime": "2026-03-29T02:00:00Z",
            "size": str(140 * 1024),
        },
        {
            "id": "id_1",
            "name": "homer_backup_2026-03-28_0200.zip",
            "createdTime": "2026-03-28T02:00:00Z",
            "size": str(130 * 1024),
        },
    ]


@pytest.fixture
def sample_zip(tmp_path):
    """Create a sample backup zip with realistic content."""
    zip_path = tmp_path / "backup.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("user_context/household.md", "# Household")
        zf.writestr("user_context/property.md", "# Property")
        zf.writestr("users.yaml", "users:\n  - name: Alice\n")
        zf.writestr("events/trip_1/status.md", "# Trip")
        zf.writestr(".nanobot_workspace/SOUL.md", "# Soul")
    return str(zip_path)


# ── list_backups ──────────────────────────────────────────────────────────────


class TestListBackups:
    def test_returns_formatted_list(self, mock_drive_files):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": mock_drive_files}

        result = list_backups(service, "folder_xyz")
        assert len(result) == 3
        assert result[0]["name"] == "homer_backup_2026-03-30_0200.zip"
        assert result[0]["id"] == "id_3"
        assert result[0]["created"] == "2026-03-30T02:00:00Z"
        assert result[0]["size_mb"] == round(150 / 1024, 2)

    def test_empty_folder(self):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": []}

        result = list_backups(service, "folder_xyz")
        assert result == []

    def test_queries_correct_folder(self, mock_drive_files):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": mock_drive_files}

        list_backups(service, "my_folder_id")
        service.files().list.assert_called_with(
            q="'my_folder_id' in parents and trashed=false",
            orderBy="createdTime desc",
            fields="files(id,name,createdTime,size)",
            pageSize=100,
        )

    def test_handles_missing_size(self):
        service = MagicMock()
        service.files().list().execute.return_value = {
            "files": [{"id": "id_1", "name": "test.zip", "createdTime": "2026-01-01T00:00:00Z"}]
        }

        result = list_backups(service, "folder_xyz")
        assert result[0]["size_mb"] == 0.0


# ── extract_backup ────────────────────────────────────────────────────────────


class TestExtractBackup:
    def test_extracts_all_files(self, sample_zip, tmp_path):
        out_dir = str(tmp_path / "tmp" / "restore")
        files = extract_backup(sample_zip, out_dir)

        assert "user_context/household.md" in files
        assert "user_context/property.md" in files
        assert "users.yaml" in files
        assert "events/trip_1/status.md" in files
        assert ".nanobot_workspace/SOUL.md" in files

    def test_files_have_correct_content(self, sample_zip, tmp_path):
        out_dir = tmp_path / "tmp" / "restore"
        extract_backup(sample_zip, str(out_dir))

        assert (out_dir / "user_context" / "household.md").read_text() == "# Household"
        assert (out_dir / "users.yaml").read_text() == "users:\n  - name: Alice\n"

    def test_creates_output_dir(self, sample_zip, tmp_path):
        out_dir = tmp_path / "tmp" / "nested" / "restore"
        extract_backup(sample_zip, str(out_dir))
        assert out_dir.exists()

    def test_cleans_previous_restore(self, sample_zip, tmp_path):
        out_dir = tmp_path / "tmp" / "restore"
        out_dir.mkdir(parents=True)
        (out_dir / "stale_file.txt").write_text("should be removed")

        extract_backup(sample_zip, str(out_dir))
        assert not (out_dir / "stale_file.txt").exists()

    def test_returns_sorted_names(self, sample_zip, tmp_path):
        out_dir = str(tmp_path / "tmp" / "restore")
        files = extract_backup(sample_zip, out_dir)
        assert files == sorted(files)


# ── Security tests ───────────────────────────────────────────────────────────


class TestExtractBackupSecurity:
    def test_zip_slip_rejected(self, tmp_path):
        """A zip entry with '../' path traversal must raise ValueError."""
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.sh", "#!/bin/bash\nrm -rf /")
        out_dir = str(tmp_path / "tmp" / "restore")
        with pytest.raises(ValueError, match="Unsafe path in zip"):
            extract_backup(str(zip_path), out_dir)

    def test_zip_absolute_path_rejected(self, tmp_path):
        """A zip entry with an absolute path must raise ValueError."""
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("/etc/passwd", "root:x:0:0:root:/root:/bin/bash")
        out_dir = str(tmp_path / "tmp" / "restore")
        with pytest.raises(ValueError, match="Unsafe path in zip"):
            extract_backup(str(zip_path), out_dir)

    def test_output_outside_workspace_rejected(self, tmp_path):
        """Output path outside workspace/tmp/ must raise ValueError."""
        zip_path = tmp_path / "backup.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "hello")
        # Try to extract to a directory outside the workspace tmp/
        out_dir = str(tmp_path / "outside" / "escape")
        with pytest.raises(ValueError, match="Output dir must be a subdirectory of"):
            extract_backup(str(zip_path), out_dir)

    def test_output_at_tmp_root_rejected(self, tmp_path):
        """Output path equal to workspace/tmp/ itself must raise ValueError."""
        zip_path = tmp_path / "backup.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test.txt", "hello")
        # Try to extract to the tmp/ root itself (would allow rmtree on tmp/)
        out_dir = str(tmp_path / "tmp")
        with pytest.raises(ValueError, match="Output dir must be a subdirectory of"):
            extract_backup(str(zip_path), out_dir)


# ── CLI integration (main) ────────────────────────────────────────────────────


class TestCLIList:
    def test_list_outputs_json(self, mock_drive_files, capsys):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": mock_drive_files}

        with patch("tools.restore_backup.get_drive_service", return_value=service):
            with patch("sys.argv", ["restore_backup.py", "--list", "--folder-id", "test_folder"]):
                from tools.restore_backup import main
                main()

        output = json.loads(capsys.readouterr().out)
        assert "backups" in output
        assert len(output["backups"]) == 3

    def test_list_no_folder_id_errors(self, capsys):
        with patch.dict(os.environ, {}, clear=True):
            # Remove HOMER_BACKUP_FOLDER_ID if present
            env = os.environ.copy()
            env.pop("HOMER_BACKUP_FOLDER_ID", None)
            with patch.dict(os.environ, env, clear=True):
                with patch("sys.argv", ["restore_backup.py", "--list"]):
                    from tools.restore_backup import main
                    with pytest.raises(SystemExit, match="1"):
                        main()

        output = json.loads(capsys.readouterr().out)
        assert "error" in output


class TestCLIDownload:
    def test_download_latest(self, mock_drive_files, sample_zip, tmp_path, capsys):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": mock_drive_files}

        out_dir = str(tmp_path / "tmp" / "restore")

        def fake_download(service, file_id, dest_path):
            """Copy sample zip to dest_path to simulate download."""
            import shutil
            shutil.copy2(sample_zip, dest_path)
            return dest_path

        with patch("tools.restore_backup.get_drive_service", return_value=service), \
             patch("tools.restore_backup.download_file", side_effect=fake_download):
            with patch("sys.argv", ["restore_backup.py", "--download", "latest",
                                     "--output", out_dir, "--folder-id", "test_folder"]):
                from tools.restore_backup import main
                main()

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["file"] == "homer_backup_2026-03-30_0200.zip"
        assert output["extracted_to"] == out_dir
        assert len(output["files"]) > 0

    def test_download_by_name(self, mock_drive_files, sample_zip, tmp_path, capsys):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": mock_drive_files}

        out_dir = str(tmp_path / "tmp" / "restore")

        def fake_download(service, file_id, dest_path):
            import shutil
            shutil.copy2(sample_zip, dest_path)
            return dest_path

        with patch("tools.restore_backup.get_drive_service", return_value=service), \
             patch("tools.restore_backup.download_file", side_effect=fake_download):
            with patch("sys.argv", ["restore_backup.py", "--download",
                                     "homer_backup_2026-03-29_0200.zip",
                                     "--output", out_dir, "--folder-id", "test_folder"]):
                from tools.restore_backup import main
                main()

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["file"] == "homer_backup_2026-03-29_0200.zip"

    def test_download_not_found(self, mock_drive_files, capsys):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": mock_drive_files}

        with patch("tools.restore_backup.get_drive_service", return_value=service):
            with patch("sys.argv", ["restore_backup.py", "--download",
                                     "nonexistent.zip", "--folder-id", "test_folder"]):
                from tools.restore_backup import main
                with pytest.raises(SystemExit, match="1"):
                    main()

        output = json.loads(capsys.readouterr().out)
        assert "error" in output
        assert "not found" in output["error"]

    def test_download_empty_folder(self, capsys):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": []}

        with patch("tools.restore_backup.get_drive_service", return_value=service):
            with patch("sys.argv", ["restore_backup.py", "--download", "latest",
                                     "--folder-id", "test_folder"]):
                from tools.restore_backup import main
                with pytest.raises(SystemExit, match="1"):
                    main()

        output = json.loads(capsys.readouterr().out)
        assert "error" in output
        assert "No backups" in output["error"]

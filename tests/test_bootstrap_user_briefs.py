"""Tests for bootstrap_user_briefs.py — idempotent per-user brief.md backfill."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import bootstrap_user_briefs as bub


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    template = tmp_path / "skills" / "morning-brief" / "default.brief.md"
    template.parent.mkdir(parents=True)
    template.write_text("# Default brief template\n", encoding="utf-8")
    users_dir = tmp_path / "context" / ".nanobot_workspace" / "users"
    monkeypatch.setattr(bub, "TEMPLATE", template)
    monkeypatch.setattr(bub, "USERS_DIR", users_dir)
    return tmp_path


def test_creates_missing_file(isolated_paths):
    result = bub.bootstrap_user("alpha")
    assert result["status"] == "created"
    target = isolated_paths / "context" / ".nanobot_workspace" / "users" / "alpha.brief.md"
    assert target.exists()
    assert "Default brief template" in target.read_text()


def test_preserves_existing_file(isolated_paths):
    target = isolated_paths / "context" / ".nanobot_workspace" / "users" / "alpha.brief.md"
    target.parent.mkdir(parents=True)
    target.write_text("Custom edits — keep me", encoding="utf-8")
    result = bub.bootstrap_user("alpha")
    assert result["status"] == "exists"
    assert target.read_text() == "Custom edits — keep me"


def test_template_missing_returns_error(isolated_paths, monkeypatch):
    monkeypatch.setattr(bub, "TEMPLATE", isolated_paths / "does-not-exist.md")
    result = bub.bootstrap_user("alpha")
    assert result["status"] == "error"
    assert "template missing" in result["error"]


def test_creates_users_dir_if_absent(isolated_paths):
    users_dir = isolated_paths / "context" / ".nanobot_workspace" / "users"
    assert not users_dir.exists()
    bub.bootstrap_user("alpha")
    assert users_dir.exists()


class TestCLI:
    def test_all_users(self, monkeypatch, capsys):
        monkeypatch.setattr(bub, "list_users", lambda: [
            {"name": "alpha"},
            {"name": "bravo"},
        ])
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 2
        assert out["created"] == 2
        assert out["exists"] == 0
        assert out["errors"] == 0

    def test_idempotent_second_run(self, monkeypatch, capsys):
        monkeypatch.setattr(bub, "list_users", lambda: [{"name": "alpha"}])
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        bub.main()
        capsys.readouterr()
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["created"] == 0
        assert out["exists"] == 1

    def test_single_user_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(bub, "list_users", lambda: [
            {"name": "alpha"},
            {"name": "bravo"},
        ])
        monkeypatch.setattr(sys, "argv",
                            ["bootstrap_user_briefs.py", "--user", "bravo"])
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 1
        assert out["results"][0]["user"] == "bravo"
        assert out["results"][0]["status"] == "created"

    def test_skips_users_without_name(self, monkeypatch, capsys):
        monkeypatch.setattr(bub, "list_users", lambda: [
            {"name": "alpha"},
            {},  # malformed entry
            {"name": ""},
        ])
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        bub.main()
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 1
        assert out["results"][0]["user"] == "alpha"

    def test_errors_exit_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(bub, "TEMPLATE",
                            isolated_paths_value(monkeypatch) / "missing.md")
        monkeypatch.setattr(bub, "list_users", lambda: [{"name": "alpha"}])
        monkeypatch.setattr(sys, "argv", ["bootstrap_user_briefs.py"])
        with pytest.raises(SystemExit) as exc:
            bub.main()
        assert exc.value.code == 1


def isolated_paths_value(monkeypatch):
    """Helper: return the tmp_path used by the isolated_paths fixture."""
    # The fixture has already set bub.USERS_DIR to <tmp>/context/users
    return bub.USERS_DIR.parent.parent

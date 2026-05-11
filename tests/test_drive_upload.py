"""
Tests for drive_upload.py — upload files/content to Google Drive.

All tests mock the Drive API — no real credentials needed.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import drive_upload as du


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_service(file_id="abc123"):
    service = MagicMock()
    service.files().create().execute.return_value = {"id": file_id, "name": "test.md"}
    service.permissions().create().execute.return_value = {}
    return service


# ── upload() ─────────────────────────────────────────────────────────────────

def test_upload_returns_url_and_name():
    service = make_service("file123")
    result = du.upload(service, "report.md", b"content", "text/plain", folder_id=None)
    assert result["url"] == "https://drive.google.com/file/d/file123/view"
    assert result["name"] == "report.md"


def test_upload_sets_folder_in_metadata():
    service = make_service()
    du.upload(service, "report.md", b"content", "text/plain", folder_id="folder999")
    call_args = service.files().create.call_args
    assert call_args.kwargs["body"]["parents"] == ["folder999"]


def test_upload_no_folder_omits_parents():
    service = make_service()
    du.upload(service, "report.md", b"content", "text/plain", folder_id=None)
    call_args = service.files().create.call_args
    assert "parents" not in call_args.kwargs["body"]


def test_upload_makes_file_public():
    service = make_service("file123")
    du.upload(service, "report.md", b"content", "text/plain", folder_id=None)
    perm_call = service.permissions().create.call_args
    assert perm_call.kwargs["fileId"] == "file123"
    assert perm_call.kwargs["body"] == {"type": "anyone", "role": "reader"}


def test_upload_uses_correct_filename():
    service = make_service()
    du.upload(service, "my_report.md", b"hello", "text/plain", folder_id=None)
    call_args = service.files().create.call_args
    assert call_args.kwargs["body"]["name"] == "my_report.md"


# ── main() — --file ───────────────────────────────────────────────────────────

def test_main_file_upload(tmp_path, capsys, monkeypatch):
    f = tmp_path / "report.md"
    f.write_text("hello world")
    service = make_service("xyz")
    monkeypatch.setattr(du, "build_service_or_exit", lambda *a, **kw: service)
    monkeypatch.setattr(du, "load_default_folder_id", lambda: None)
    with pytest.raises(SystemExit, match="0") if False else __import__("contextlib").nullcontext():
        import sys as _sys
        _sys.argv = ["drive_upload.py", "--file", str(f)]
        du.main()
    out = json.loads(capsys.readouterr().out)
    assert out["url"].startswith("https://drive.google.com")
    assert out["name"] == "report.md"


def test_main_content_upload(capsys, monkeypatch):
    service = make_service("abc")
    monkeypatch.setattr(du, "build_service_or_exit", lambda *a, **kw: service)
    monkeypatch.setattr(du, "load_default_folder_id", lambda: None)
    import sys as _sys
    _sys.argv = ["drive_upload.py", "--content", "my report text", "--name", "summary.md"]
    du.main()
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "summary.md"
    assert "drive.google.com" in out["url"]


def test_main_content_without_name_exits(monkeypatch):
    monkeypatch.setattr(du, "build_service_or_exit", lambda *a, **kw: MagicMock())
    import sys as _sys
    _sys.argv = ["drive_upload.py", "--content", "some text"]
    with pytest.raises(SystemExit):
        du.main()


def test_main_missing_file_exits(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(du, "build_service_or_exit", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(du, "load_default_folder_id", lambda: None)
    import sys as _sys
    _sys.argv = ["drive_upload.py", "--file", str(tmp_path / "nonexistent.md")]
    with pytest.raises(SystemExit):
        du.main()
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_main_uses_custom_folder_id(tmp_path, capsys, monkeypatch):
    f = tmp_path / "f.md"
    f.write_text("data")
    service = make_service("id1")
    monkeypatch.setattr(du, "build_service_or_exit", lambda *a, **kw: service)
    monkeypatch.setattr(du, "load_default_folder_id", lambda: None)
    import sys as _sys
    _sys.argv = ["drive_upload.py", "--file", str(f), "--folder-id", "custom_folder"]
    du.main()
    call_args = service.files().create.call_args
    assert call_args.kwargs["body"]["parents"] == ["custom_folder"]

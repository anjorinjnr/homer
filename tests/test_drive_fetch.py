"""
Tests for drive_fetch.py — gogcli wrapper for Drive indexing.

Mocks gogcli.run — no real binary or token required.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import drive_fetch as df
import gogcli


def test_find_root_folder_returns_id(monkeypatch):
    payload = {"files": [{"id": "root-123", "name": "family_docs"}]}
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: payload)
    
    assert df.find_root_folder("tok") == "root-123"


def test_find_root_folder_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: {"files": []})
    assert df.find_root_folder("tok") is None


def test_walk_folder_recursively(monkeypatch):
    def fake_run(token, *args):
        # Determine parent from args
        parent_id = ""
        for i, arg in enumerate(args):
            if arg == "--parent":
                parent_id = args[i+1]
                break
        
        if parent_id == "root-id":
            return {"files": [
                {"id": "file-1", "name": "doc.pdf", "mimeType": "application/pdf", "size": "1024"},
                {"id": "subfolder-id", "name": "sub", "mimeType": "application/vnd.google-apps.folder"}
            ]}
        if parent_id == "subfolder-id":
            return {"files": [
                {"id": "file-2", "name": "sheet.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "size": "2048"}
            ]}
        return {"files": []}

    monkeypatch.setattr(gogcli, "run", fake_run)
    
    files = df.walk_folder("tok", "root-id", "family_docs")
    
    assert len(files) == 2
    assert files[0]["name"] == "doc.pdf"
    assert files[0]["folder"] == "family_docs"
    
    assert files[1]["name"] == "sheet.xlsx"
    assert files[1]["folder"] == "family_docs/sub"
    assert files[1]["path"] == "family_docs/sub/sheet.xlsx"


def test_already_synced_today(tmp_path, monkeypatch):
    last_sync_file = tmp_path / "last_sync.txt"
    monkeypatch.setattr(df, "LAST_SYNC_FILE", last_sync_file)
    
    # Not exists
    assert df.already_synced_today() is False
    
    # Exists but old
    last_sync_file.write_text("2020-01-01T12:00:00-05:00")
    assert df.already_synced_today() is False
    
    # Today
    from datetime import datetime
    now_str = datetime.now(df.LOCAL_TZ).isoformat()
    last_sync_file.write_text(now_str)
    assert df.already_synced_today() is True

"""
Tests for gmail_search.py — gogcli wrapper for Gmail search.

All tests mock gogcli.run — no real binary or token required.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import gmail_search as gs
import gogcli


def test_normalize_date():
    assert gs.normalize_date("Mon, 10 Mar 2026 12:00:00 +0000") == "2026-03-10 12:00 UTC"
    assert gs.normalize_date("garbage") == "garbage"


def test_fetch_message_returns_expected_fields(monkeypatch):
    payload = {
        "body": "Hello world",
        "headers": {
            "subject": "Test",
            "from": "alice@example.com",
            "date": "Mon, 10 Mar 2026 12:00:00 +0000"
        },
        "message": {"threadId": "t123"}
    }
    monkeypatch.setattr(gogcli, "run", lambda *a: payload)
    
    result = gs.fetch_message("tok", "m123")
    assert result["id"] == "m123"
    assert result["thread_id"] == "t123"
    assert result["subject"] == "Test"
    assert result["from"] == "alice@example.com"
    assert result["body"] == "Hello world"
    assert result["date"] == "2026-03-10 12:00 UTC"


def test_fetch_message_truncates_long_body(monkeypatch):
    long_body = "x" * 3000
    payload = {
        "body": long_body,
        "headers": {},
        "message": {}
    }
    monkeypatch.setattr(gogcli, "run", lambda *a: payload)
    
    result = gs.fetch_message("tok", "1")
    assert len(result["body"]) < 3000
    assert "truncated" in result["body"]


def test_main_empty_results(capsys, monkeypatch):
    monkeypatch.setattr(gs, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(gogcli, "run", lambda *a: {"messages": []})
    
    monkeypatch.setattr(sys, "argv", ["gmail_search.py", "--account", "primary", "--query", "from:nobody"])
    gs.main()
    assert json.loads(capsys.readouterr().out) == []


def test_main_returns_results(capsys, monkeypatch):
    monkeypatch.setattr(gs, "get_access_token", lambda a: "tok")
    
    def fake_run(token, *args):
        if "search" in args:
            return {"messages": [{"id": "m1"}]}
        if "get" in args:
            return {
                "body": "content",
                "headers": {"subject": "S", "from": "F", "date": "D"},
                "message": {"threadId": "t1"}
            }
        return {}

    monkeypatch.setattr(gogcli, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["gmail_search.py", "--account", "primary", "--query", "test"])
    gs.main()
    
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    assert results[0]["id"] == "m1"
    assert results[0]["body"] == "content"


def test_main_respects_limit(monkeypatch):
    captured_args = []
    monkeypatch.setattr(gs, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(gogcli, "run", lambda t, *args: captured_args.append(args) or {"messages": []})
    
    monkeypatch.setattr(sys, "argv", ["gmail_search.py", "--account", "primary", "--query", "test", "--limit", "3"])
    gs.main()
    
    # search call is the first one
    search_args = captured_args[0]
    assert "--max" in search_args
    assert "3" in search_args

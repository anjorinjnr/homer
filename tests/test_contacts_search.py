"""
Tests for contacts_search.py — Google Contacts search via gogcli wrapper.

All tests mock subprocess.run and load_google_credentials — no real gogcli
binary or OAuth tokens needed.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import contacts_search as cs
import gogcli
import google_auth


# ── normalize() ───────────────────────────────────────────────────────────────


def test_normalize_full_shape():
    payload = {"contacts": [
        {"resource": "people/c123", "name": "Maya Johnson",
         "email": "maya@example.com", "phone": "+15551234"},
    ]}
    assert cs.normalize(payload) == [{
        "name": "Maya Johnson",
        "emails": ["maya@example.com"],
        "phones": ["+15551234"],
        "resource_name": "people/c123",
    }]


def test_normalize_missing_email_phone():
    payload = {"contacts": [{"resource": "people/c1", "name": "Jane Doe"}]}
    result = cs.normalize(payload)
    assert result[0]["name"] == "Jane Doe"
    assert result[0]["emails"] == []
    assert result[0]["phones"] == []


def test_normalize_missing_name():
    payload = {"contacts": [{"resource": "people/c1", "email": "x@y.com"}]}
    result = cs.normalize(payload)
    assert result[0]["name"] == ""
    assert result[0]["emails"] == ["x@y.com"]


def test_normalize_empty_contacts():
    assert cs.normalize({"contacts": []}) == []


def test_normalize_no_contacts_key():
    assert cs.normalize({}) == []


# ── tool-specific mocks ───────────────────────────────────────────────────────


def _mock_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ── get_access_token() ────────────────────────────────────────────────────────


def test_get_access_token_returns_creds_token(monkeypatch):
    fake_creds = MagicMock(token="abc123", scopes=[cs.CONTACTS_SCOPE])
    monkeypatch.setattr(cs, "load_google_credentials", lambda account: fake_creds)
    assert cs.get_access_token("primary") == "abc123"


def test_get_access_token_raises_when_token_missing(monkeypatch):
    fake_creds = MagicMock(token=None, scopes=[cs.CONTACTS_SCOPE])
    monkeypatch.setattr(cs, "load_google_credentials", lambda account: fake_creds)
    with pytest.raises(RuntimeError, match="No access token"):
        cs.get_access_token("primary")


def test_get_access_token_raises_when_contacts_scope_missing(monkeypatch):
    """Token issued before contacts.readonly was added — surface a re-link message."""
    fake_creds = MagicMock(token="abc123", scopes=[
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar",
    ])
    monkeypatch.setattr(cs, "load_google_credentials", lambda account: fake_creds)
    with pytest.raises(PermissionError, match="contacts.readonly"):
        cs.get_access_token("primary")


# ── require_scopes() — defined in google_auth.py, exercised here ──────────────


def test_require_scopes_passes_when_all_present():
    creds = MagicMock(scopes=["a", "b", "c"])
    google_auth.require_scopes(creds, "primary", "a", "b")  # no exception


def test_require_scopes_raises_with_actionable_message():
    creds = MagicMock(scopes=["a"])
    with pytest.raises(PermissionError) as exc:
        google_auth.require_scopes(creds, "primary", "a", "b", "c")
    msg = str(exc.value)
    assert "primary" in msg
    assert "b, c" in msg  # missing scopes listed
    assert "re-link" in msg.lower()


def test_require_scopes_handles_none_scopes():
    """creds.scopes can be None right after load on some OAuth flows."""
    creds = MagicMock(scopes=None)
    with pytest.raises(PermissionError, match="missing"):
        google_auth.require_scopes(creds, "primary", "a")


def test_require_scopes_uses_friendly_short_names():
    """Error message should show last URL segment, not full scope URL."""
    creds = MagicMock(scopes=[])
    with pytest.raises(PermissionError) as exc:
        google_auth.require_scopes(creds, "primary",
                                   "https://www.googleapis.com/auth/contacts.readonly")
    assert "contacts.readonly" in str(exc.value)
    assert "https://" not in str(exc.value)


# ── main() ────────────────────────────────────────────────────────────────────


def test_main_happy_path(capsys, monkeypatch):
    monkeypatch.setattr(cs, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(gogcli, "run",
        lambda token, *args: {"contacts": [
            {"resource": "people/c1", "name": "Maya", "email": "maya@example.com"},
        ]},
    )
    monkeypatch.setattr(sys, "argv", ["contacts_search.py", "--query", "maya"])
    cs.main()
    out = json.loads(capsys.readouterr().out)
    assert out == [{
        "name": "Maya",
        "emails": ["maya@example.com"],
        "phones": [],
        "resource_name": "people/c1",
    }]


def test_main_emits_error_on_token_missing(capsys, monkeypatch):
    def boom(account):
        raise FileNotFoundError("token not found for account 'primary'")
    monkeypatch.setattr(cs, "get_access_token", boom)
    monkeypatch.setattr(sys, "argv", ["contacts_search.py", "--query", "x"])
    with pytest.raises(SystemExit) as exc:
        cs.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "token not found" in out["error"]


def test_main_emits_error_on_gogcli_failure(capsys, monkeypatch):
    monkeypatch.setattr(cs, "get_access_token", lambda a: "tok")
    def boom(token, *args):
        raise RuntimeError("gogcli failed (exit 2): scope insufficient")
    monkeypatch.setattr(gogcli, "run", boom)
    monkeypatch.setattr(sys, "argv", ["contacts_search.py", "--query", "x"])
    with pytest.raises(SystemExit) as exc:
        cs.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "scope insufficient" in out["error"]


def test_main_emits_friendly_error_when_scope_missing(capsys, monkeypatch):
    """End-to-end: stale token → main() surfaces the re-link message via {error}."""
    fake_creds = MagicMock(token="abc", scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(cs, "load_google_credentials", lambda account: fake_creds)
    monkeypatch.setattr(sys, "argv", ["contacts_search.py", "--query", "x"])
    with pytest.raises(SystemExit) as exc:
        cs.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "contacts.readonly" in out["error"]
    assert "re-link" in out["error"].lower()


def test_main_emits_error_when_binary_missing(capsys, monkeypatch):
    monkeypatch.setattr(cs, "get_access_token", lambda a: "tok")
    def boom(token, *args):
        raise RuntimeError(f"gogcli binary '{gogcli.GOG_BIN}' not found. Install: brew install gogcli")
    monkeypatch.setattr(gogcli, "run", boom)
    monkeypatch.setattr(sys, "argv", ["contacts_search.py", "--query", "x"])
    with pytest.raises(SystemExit) as exc:
        cs.main()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "not found" in out["error"]
    assert "brew install gogcli" in out["error"]


def test_main_respects_limit_flag(capsys, monkeypatch):
    captured = {}

    def fake_search(token, *args):
        # args = ('contacts', 'search', 'x', '--max', '3')
        captured["limit"] = args[-1]
        return {"contacts": []}

    monkeypatch.setattr(cs, "get_access_token", lambda a: "tok")
    monkeypatch.setattr(gogcli, "run", fake_search)
    monkeypatch.setattr(sys, "argv", ["contacts_search.py", "--query", "x", "--limit", "3"])
    cs.main()
    assert captured["limit"] == "3"

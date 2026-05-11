import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import gogcli


def _mock_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_run_argv_and_env(monkeypatch):
    captured = {}

    def fake_run(cmd, env=None, **_):
        captured["cmd"] = cmd
        captured["env"] = env
        return _mock_proc(stdout='{"foo": "bar"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = gogcli.run("fake-token", "calendar", "events", "--days=7")
    
    assert result == {"foo": "bar"}
    assert captured["env"]["GOG_ACCESS_TOKEN"] == "fake-token"
    assert captured["cmd"][0] == gogcli.GOG_BIN
    assert "--json" in captured["cmd"]
    assert "--no-input" in captured["cmd"]
    assert "calendar" in captured["cmd"]
    assert "events" in captured["cmd"]
    assert "--days=7" in captured["cmd"]


def test_run_token_overrides_caller_env(monkeypatch):
    """Token argument must win over any GOG_ACCESS_TOKEN already in os.environ."""
    captured = {}
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "stale-token-from-shell")

    def fake_run(cmd, env=None, **_):
        captured["env"] = env
        return _mock_proc(stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gogcli.run("fresh", "calendar", "events")
    assert captured["env"]["GOG_ACCESS_TOKEN"] == "fresh"


def test_run_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _mock_proc(stderr="auth failed", returncode=2),
    )
    with pytest.raises(RuntimeError, match="auth failed"):
        gogcli.run("tok", "calendar", "events")


def test_run_strips_note_prefix_from_error(monkeypatch):
    """gogcli writes a 'Note: Using direct access token...' line to stderr on
    every call. The error message surfaced to Homer should not include it."""
    stderr = (
        "Note: Using direct access token (expires in ~1 hour; no auto-refresh)\n"
        "Google API error (403 insufficientPermissions): real error text"
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _mock_proc(stderr=stderr, returncode=4),
    )
    with pytest.raises(RuntimeError) as exc:
        gogcli.run("tok", "calendar", "events")
    msg = str(exc.value)
    assert "Note:" not in msg
    assert "real error text" in msg


def test_run_friendly_error_when_binary_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory: 'gog'")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="not found.*brew install gogcli"):
        gogcli.run("tok", "calendar", "events")


def test_run_friendly_error_when_subprocess_times_out(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=kw.get("args") or a[0], timeout=30)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out after 30s"):
        gogcli.run("tok", "calendar", "events")


def test_run_passes_timeout_to_subprocess(monkeypatch):
    captured = {}

    def fake_run(cmd, env=None, **kw):
        captured["timeout"] = kw.get("timeout")
        return _mock_proc(stdout="{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gogcli.run("tok", "calendar", "events")
    assert captured["timeout"] == gogcli.GOG_TIMEOUT_SEC


def test_run_handles_empty_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _mock_proc(stdout=""))
    assert gogcli.run("tok", "calendar", "events") == {}


def test_run_raises_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _mock_proc(stdout="not json"),
    )
    with pytest.raises(RuntimeError, match="non-JSON"):
        gogcli.run("tok", "calendar", "events")


def test_clean_stderr():
    stderr = (
        "Note: Using direct access token...\n"
        "real error"
    )
    assert gogcli.clean_stderr(stderr) == "real error"

    stderr_only_note = "Note: some noise"
    assert gogcli.clean_stderr(stderr_only_note) == "Note: some noise"  # returns original if empty after filtering


def _mock_proc_bytes(stdout=b"", stderr=b"", returncode=0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_download_bytes_argv_omits_json(monkeypatch):
    """download_bytes must not pass --json (gogcli rejects --json with --out=-)."""
    captured = {}

    def fake_run(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["kwargs"] = kw
        return _mock_proc_bytes(stdout=b"binary-bytes")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = gogcli.download_bytes("fresh-token", "drive", "download", "FID", "--out=-")

    assert result == b"binary-bytes"
    assert "--json" not in captured["cmd"]
    assert "--no-input" in captured["cmd"]
    assert "--out=-" in captured["cmd"]
    assert captured["env"]["GOG_ACCESS_TOKEN"] == "fresh-token"
    # Must NOT use text=True — we want raw bytes for binary content (PDFs).
    assert captured["kwargs"].get("text") in (None, False)


def test_download_bytes_returns_raw_bytes(monkeypatch):
    """Binary content (e.g. PDF magic bytes) must round-trip unchanged."""
    pdf_bytes = b"%PDF-1.4\n\x00\xff\x80\x7f"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _mock_proc_bytes(stdout=pdf_bytes))
    assert gogcli.download_bytes("tok", "drive", "download", "FID", "--out=-") == pdf_bytes


def test_download_bytes_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _mock_proc_bytes(stderr=b"auth failed", returncode=2),
    )
    with pytest.raises(RuntimeError, match="auth failed"):
        gogcli.download_bytes("tok", "drive", "download", "FID", "--out=-")


def test_download_bytes_strips_note_from_error(monkeypatch):
    stderr = (
        b"Note: Using direct access token (expires in ~1 hour; no auto-refresh)\n"
        b"download failed: 404 Not Found"
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _mock_proc_bytes(stderr=stderr, returncode=4),
    )
    with pytest.raises(RuntimeError) as exc:
        gogcli.download_bytes("tok", "drive", "download", "FID", "--out=-")
    msg = str(exc.value)
    assert "Note:" not in msg
    assert "404 Not Found" in msg


def test_download_bytes_friendly_error_when_binary_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory: 'gog'")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="not found.*brew install gogcli"):
        gogcli.download_bytes("tok", "drive", "download", "FID", "--out=-")


def test_download_bytes_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=kw.get("args") or a[0], timeout=30)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        gogcli.download_bytes("tok", "drive", "download", "FID", "--out=-")

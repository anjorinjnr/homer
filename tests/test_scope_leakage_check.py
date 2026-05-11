"""Tests for tools/scope_leakage_check.py — USER.md stub integrity guard."""

from pathlib import Path

import pytest

import tools.scope_leakage_check as slc


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_file_returns_code_2(tmp_path):
    code, details = slc.check(tmp_path / "USER.md")
    assert code == 2
    assert details["status"] == "missing"


def test_stub_content_returns_code_0(tmp_path):
    user = _write(
        tmp_path / "USER.md",
        "# Guest Agent Context\n\nScope context injected per-turn by nanobot.\n",
    )
    code, details = slc.check(user)
    assert code == 0
    assert details["status"] == "ok"
    assert details["size_chars"] > 0


def test_empty_file_is_ok(tmp_path):
    """An empty file contains no leakage markers — not ideal but not a leak."""
    user = _write(tmp_path / "USER.md", "")
    code, _ = slc.check(user)
    assert code == 0


@pytest.mark.parametrize("marker", [
    "## Scope:",
    "### Context",
    "### Conversation History",
    "### Pending Follow-ups",
    "Disclosure rules",
    "## Active Scopes",
])
def test_each_marker_triggers_leakage_code_1(tmp_path, marker):
    user = _write(tmp_path / "USER.md", f"# Guest Agent Context\n\n{marker} foo\n")
    code, details = slc.check(user)
    assert code == 1
    assert details["status"] == "leakage_detected"
    assert marker in details["markers_found"]


def test_multiple_markers_all_reported(tmp_path):
    user = _write(
        tmp_path / "USER.md",
        "## Scope: rel_a\n### Context\nfoo\n### Conversation History\n- stuff\n",
    )
    code, details = slc.check(user)
    assert code == 1
    assert len(details["markers_found"]) == 3


def test_resolve_workspace_prefers_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMER_GUEST_WORKSPACE", "/env/path")
    resolved = slc._resolve_workspace(str(tmp_path))
    assert resolved == tmp_path


def test_resolve_workspace_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(tmp_path))
    resolved = slc._resolve_workspace(None)
    assert resolved == tmp_path

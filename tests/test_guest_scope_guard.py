"""Tests for tools/guest_scope_guard.py — per-sender scope gating.

Covers:
  * is_guest_mode() only True when HOMER_GUEST_WORKSPACE is set.
  * sender_scope_ids() reads ONLY current_sender_scopes.json — no fallback
    to the global active_scopes.json (which would defeat the gate for
    unscoped senders, the prod regression from the Adam/Maya leak).
  * assert_scope_allowed() refuses scopes not in the per-sender list.
  * assert_event_allowed() refuses event IDs not reachable from the sender's
    scopes via context_source.ref or task_tags.task_id.
  * End-to-end: event_manage.py --status --event-id <non-participant-event>
    exits non-zero when HOMER_GUEST_WORKSPACE points at a sender without that
    scope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import guest_scope_guard  # noqa: E402
import scope_store as ss  # noqa: E402


@pytest.fixture
def guest_ws(tmp_path, monkeypatch):
    ws = tmp_path / "guest_workspace"
    ws.mkdir()
    # Guest detection requires HOMER_WORKSPACE == HOMER_GUEST_WORKSPACE.
    monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(ws))
    monkeypatch.setenv("HOMER_WORKSPACE", str(ws))
    return ws


@pytest.fixture
def scope_db(tmp_path, monkeypatch):
    db = tmp_path / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db))
    # Scope A: denver_mtb — Adam is a participant; context_source.ref = denver_mtb
    env_a = ss.make_minimal_envelope(
        scope_id="denver_mtb",
        name="Adam",
        participant_id="16072348189@s.whatsapp.net",
        event_id="denver_mtb",
        principal="alex",
        context_source={"type": "event", "ref": "denver_mtb"},
    )
    ss.create_scope(env_a, db)
    # Scope B: maya_5th_bday — only Alex; different event
    env_b = ss.make_minimal_envelope(
        scope_id="maya_5th_bday",
        name="Alex",
        participant_id="4126920720@s.whatsapp.net",
        event_id="maya_5th_bday",
        principal="alex",
        context_source={"type": "event", "ref": "maya_5th_bday"},
    )
    ss.create_scope(env_b, db)
    return db


class TestIsGuestMode:
    def test_false_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("HOMER_GUEST_WORKSPACE", raising=False)
        monkeypatch.delenv("HOMER_WORKSPACE", raising=False)
        assert guest_scope_guard.is_guest_mode() is False

    def test_true_when_workspaces_match(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
        assert guest_scope_guard.is_guest_mode() is True

    def test_false_for_main_agent_with_both_vars_set(self, monkeypatch, tmp_path):
        """Main-agent regression: HOMER_GUEST_WORKSPACE is in the main
        config's allowedEnvKeys, so main-agent subprocesses see it too.
        Without checking HOMER_WORKSPACE, main got falsely gated as a
        guest and refused legitimate event_manage updates."""
        main_ws = tmp_path / "main_ws"
        guest_ws = tmp_path / "guest_ws"
        main_ws.mkdir()
        guest_ws.mkdir()
        monkeypatch.setenv("HOMER_WORKSPACE", str(main_ws))
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(guest_ws))
        assert guest_scope_guard.is_guest_mode() is False

    def test_false_when_only_guest_var_set(self, monkeypatch, tmp_path):
        """Missing HOMER_WORKSPACE means we can't confirm this is the
        guest process — fail open for detection (main agent path) since
        the strict gates elsewhere still protect data."""
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("HOMER_WORKSPACE", raising=False)
        assert guest_scope_guard.is_guest_mode() is False


class TestSenderScopeIds:
    def test_reads_current_sender_scopes(self, guest_ws):
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        # active_scopes.json with cross-scope content must be IGNORED — this
        # is the regression that let Adam (no scope) read maya_5th_bday.
        (guest_ws / "active_scopes.json").write_text(json.dumps(["denver_mtb", "maya_5th_bday"]))
        assert guest_scope_guard.sender_scope_ids() == ["denver_mtb"]

    def test_no_fallback_to_active_scopes(self, guest_ws):
        # Only the global file is present — gate must NOT widen to it.
        (guest_ws / "active_scopes.json").write_text(json.dumps(["denver_mtb", "maya_5th_bday"]))
        assert guest_scope_guard.sender_scope_ids() == []

    def test_returns_empty_when_per_sender_file_missing(self, guest_ws):
        assert guest_scope_guard.sender_scope_ids() == []

    def test_ignores_non_string_entries(self, guest_ws):
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb", 42, None]))
        assert guest_scope_guard.sender_scope_ids() == ["denver_mtb"]


class TestAssertScopeAllowed:
    def test_no_op_outside_guest_mode(self, monkeypatch):
        monkeypatch.delenv("HOMER_GUEST_WORKSPACE", raising=False)
        # Doesn't raise / exit even for bogus scope
        guest_scope_guard.assert_scope_allowed("anything_goes")

    def test_allows_scope_in_sender_list(self, guest_ws):
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        guest_scope_guard.assert_scope_allowed("denver_mtb")  # should not raise

    def test_rejects_scope_not_in_sender_list(self, guest_ws):
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        with pytest.raises(SystemExit) as excinfo:
            guest_scope_guard.assert_scope_allowed("maya_5th_bday")
        assert excinfo.value.code == 2

    def test_rejects_when_sender_list_empty(self, guest_ws):
        with pytest.raises(SystemExit) as excinfo:
            guest_scope_guard.assert_scope_allowed("denver_mtb")
        assert excinfo.value.code == 2


class TestAssertEventAllowed:
    def test_no_op_outside_guest_mode(self, monkeypatch, scope_db):
        monkeypatch.delenv("HOMER_GUEST_WORKSPACE", raising=False)
        guest_scope_guard.assert_event_allowed("maya_5th_bday")

    def test_allows_event_matching_sender_scope(self, guest_ws, scope_db):
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        guest_scope_guard.assert_event_allowed("denver_mtb")

    def test_rejects_event_not_in_sender_scopes(self, guest_ws, scope_db):
        # Adam is in denver_mtb only; maya_5th_bday is a different scope
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        with pytest.raises(SystemExit) as excinfo:
            guest_scope_guard.assert_event_allowed("maya_5th_bday")
        assert excinfo.value.code == 2

    def test_rejects_unknown_event(self, guest_ws, scope_db):
        (guest_ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        with pytest.raises(SystemExit):
            guest_scope_guard.assert_event_allowed("does_not_exist")

    def test_rejects_when_sender_has_no_scopes(self, guest_ws, scope_db):
        with pytest.raises(SystemExit):
            guest_scope_guard.assert_event_allowed("denver_mtb")


class TestScopeContextSideEffect:
    """render_scope_context_for_sender writes current_sender_scopes.json."""

    def test_writes_sender_scope_ids_to_guest_workspace(self, tmp_path, monkeypatch, scope_db):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(ws))
        # Adam is in denver_mtb only; his WhatsApp digit form is 16072348189.
        ss.render_scope_context_for_sender("16072348189")
        out = ws / "current_sender_scopes.json"
        assert out.exists()
        assert json.loads(out.read_text()) == ["denver_mtb"]

    def test_writes_empty_list_for_unknown_sender(self, tmp_path, monkeypatch, scope_db):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(ws))
        ss.render_scope_context_for_sender("99999999999")
        assert json.loads((ws / "current_sender_scopes.json").read_text()) == []

    def test_silent_noop_without_workspace_env(self, monkeypatch, tmp_path, scope_db):
        # No HOMER_GUEST_WORKSPACE / HOMER_WORKSPACE → no file written, no crash.
        monkeypatch.delenv("HOMER_GUEST_WORKSPACE", raising=False)
        monkeypatch.delenv("HOMER_WORKSPACE", raising=False)
        # Just verify it doesn't raise.
        ss.render_scope_context_for_sender("16072348189")


class TestEventManageGuestMode:
    """End-to-end: event_manage.py refuses cross-scope queries in guest mode."""

    def _run(self, args, env):
        cmd = [sys.executable, str(REPO_ROOT / "tools" / "event_manage.py")] + args
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_status_rejected_for_non_participant_event(self, tmp_path, scope_db):
        # Adam's session: only denver_mtb in his sender scopes
        ws = tmp_path / "guest_ws"
        ws.mkdir()
        (ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        env = os.environ.copy()
        env["HOMER_GUEST_WORKSPACE"] = str(ws)
        env["HOMER_WORKSPACE"] = str(ws)
        env["HOMER_SCOPE_DB"] = str(scope_db)
        env["HOMER_EVENTS_DIR"] = str(tmp_path / "events")
        (tmp_path / "events").mkdir()
        result = self._run(["--status", "--event-id", "maya_5th_bday"], env)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "maya_5th_bday" in result.stderr

    def test_blocks_list_action_in_guest_mode(self, tmp_path, scope_db):
        ws = tmp_path / "guest_ws"
        ws.mkdir()
        (ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        env = os.environ.copy()
        env["HOMER_GUEST_WORKSPACE"] = str(ws)
        env["HOMER_WORKSPACE"] = str(ws)
        env["HOMER_SCOPE_DB"] = str(scope_db)
        env["HOMER_EVENTS_DIR"] = str(tmp_path / "events")
        (tmp_path / "events").mkdir()
        result = self._run(["--list"], env)
        assert result.returncode != 0
        assert "--list" in result.stderr or "list" in result.stderr

    def test_unscoped_sender_with_global_active_scopes_still_refused(self, tmp_path, scope_db):
        """Prod regression: unscoped sender + populated active_scopes.json
        previously fell back to the global list and let the guest read any
        scope's events. Must refuse now."""
        ws = tmp_path / "guest_ws"
        ws.mkdir()
        # No current_sender_scopes.json. active_scopes.json contains everything
        # (as build_context.py used to write it pre-fix).
        (ws / "active_scopes.json").write_text(json.dumps(["denver_mtb", "maya_5th_bday"]))
        env = os.environ.copy()
        env["HOMER_GUEST_WORKSPACE"] = str(ws)
        env["HOMER_WORKSPACE"] = str(ws)
        env["HOMER_SCOPE_DB"] = str(scope_db)
        env["HOMER_EVENTS_DIR"] = str(tmp_path / "events")
        (tmp_path / "events").mkdir()
        result = self._run(["--status", "--event-id", "maya_5th_bday"], env)
        assert result.returncode != 0
        assert "No active scopes" in result.stderr or "not reachable" in result.stderr

    def test_allows_status_on_participant_event(self, tmp_path, scope_db):
        # Seed a status.md for denver_mtb so --status has something to read
        events_dir = tmp_path / "events"
        (events_dir / "denver_mtb").mkdir(parents=True)
        (events_dir / "denver_mtb" / "status.md").write_text(
            "# Denver MTB\nStatus: Coordinating\nDates: TBD\nCreated: 2026-03-20\n"
        )
        ws = tmp_path / "guest_ws"
        ws.mkdir()
        (ws / "current_sender_scopes.json").write_text(json.dumps(["denver_mtb"]))
        env = os.environ.copy()
        env["HOMER_GUEST_WORKSPACE"] = str(ws)
        env["HOMER_WORKSPACE"] = str(ws)
        env["HOMER_SCOPE_DB"] = str(scope_db)
        env["HOMER_EVENTS_DIR"] = str(events_dir)
        result = self._run(["--status", "--event-id", "denver_mtb"], env)
        assert result.returncode == 0, result.stdout + result.stderr

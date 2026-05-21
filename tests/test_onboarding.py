"""Tests for onboarding.py — cold-start and progressive new-user onboarding."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest

import tools.context_updater as cu
import tools.google_auth as ga
import tools.onboarding as ob
from tools.onboarding_fields import FIELDS, HOUSEHOLD_TEMPLATE, field_by_key


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_fs(tmp_path, monkeypatch):
    """Route every filesystem side effect into tmp_path and skip MEMORY rebuild."""
    db_path = tmp_path / "onboarding.db"
    monkeypatch.setenv("HOMER_ONBOARDING_DB", str(db_path))
    monkeypatch.setenv("HOMER_ONBOARDING_SKIP_REBUILD", "1")
    # Route heartbeat lookups to tmp (so _heartbeat_path finds nothing by default)
    monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path / "workspace"))

    # Route household.md writes into tmp_path — avoid touching the real repo
    context_dir = tmp_path / "context"
    user_context_dir = context_dir / "user_context"
    user_context_dir.mkdir(parents=True)
    monkeypatch.setattr(cu, "CONTEXT_DIR", context_dir)
    monkeypatch.setattr(cu, "USER_CONTEXT_DIR", user_context_dir)

    # Isolate Google token detection — a real token in secrets/ on a dev
    # machine would otherwise leak into _detect_workspace() and flip the
    # workspace setup status from "unknown" to "done".
    token_dir = tmp_path / "secrets" / "tokens"
    monkeypatch.setattr(ga, "TOKENS_DIR", token_dir)
    monkeypatch.setattr(ga, "LEGACY_TOKEN", tmp_path / "secrets" / "google_token.pickle")
    return tmp_path


def _capture(argv: list[str]) -> dict:
    old = sys.stdout
    sys.stdout = StringIO()
    try:
        ob.main(argv)
    finally:
        out = sys.stdout.getvalue()
        sys.stdout = old
    return json.loads(out)


def _write_household(body: str, *, workspace) -> Path:
    path = cu.USER_CONTEXT_DIR / "household.md"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# M1 — Foundation: DB init, status, gap, phase transitions
# ---------------------------------------------------------------------------

class TestInit:
    def test_init_creates_db_and_template(self, isolated_fs):
        result = ob.cmd_init(register_heartbeat=False)
        assert result["status"] == "initialized"
        assert Path(result["db_path"]).exists()
        # Template was created
        household = cu.USER_CONTEXT_DIR / "household.md"
        assert household.exists()
        assert "## Primary user" in household.read_text()
        assert result["phase"] == ob.PHASE_COLD

    def test_init_is_idempotent(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_init(register_heartbeat=False)
        # Field rows should be seeded exactly once
        with ob.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM onboarding_fields"
            ).fetchone()[0]
        assert count == len(FIELDS)

    def test_init_does_not_overwrite_existing_household(self, isolated_fs):
        existing = "# Household\n\n## Primary user\n- Name: Ada\n"
        _write_household(existing, workspace=isolated_fs)
        ob.cmd_init(register_heartbeat=False)
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        assert "Ada" in content

    def test_init_heartbeat_skipped_when_file_missing(self, isolated_fs):
        result = ob.cmd_init(register_heartbeat=True)
        assert result["heartbeat"]["registered"] is False
        assert result["heartbeat"]["reason"] == "heartbeat_missing"


class TestStatus:
    def test_status_on_fresh_install(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_status()
        assert result["phase"] == ob.PHASE_COLD
        assert result["counts"]["unknown"] == len(FIELDS)
        assert result["counts"]["answered"] == 0
        assert result["total_fields"] == len(FIELDS)
        assert result["suppressed"] is False
        assert result["next_gap"]["tier"] == 1

    def test_status_suppressed_after_complete(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_set_phase(ob.PHASE_COMPLETE)
        result = ob.cmd_status()
        assert result["suppressed"] is True


class TestGap:
    def test_gap_priority_order(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_gap()
        gaps = result["gaps"]
        # Tier 1 fields come first
        assert gaps[0]["tier"] == 1
        # Within tier, alphabetical when asked_count is equal
        tier1 = [g for g in gaps if g["tier"] == 1]
        assert [g["key"] for g in tier1] == sorted(g["key"] for g in tier1)
        # Count matches total fields minus terminal
        assert result["count"] == len(FIELDS)

    def test_gap_filter_by_tier(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_gap(tier=2)
        for g in result["gaps"]:
            assert g["tier"] == 2

    def test_gap_excludes_answered(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_answer("primary_user.name", "Ada")
        keys = [g["key"] for g in ob.cmd_gap()["gaps"]]
        assert "primary_user.name" not in keys


class TestPhaseTransitions:
    def test_auto_advance_to_progressive_when_tier1_done(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        tier1 = [f.key for f in FIELDS if f.tier == 1]
        for key in tier1:
            ob.cmd_decline(key)
        status = ob.cmd_status()
        assert status["phase"] == ob.PHASE_PROGRESSIVE

    def test_auto_advance_to_complete_when_all_terminal(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        for f in FIELDS:
            ob.cmd_decline(f.key)
        status = ob.cmd_status()
        assert status["phase"] == ob.PHASE_COMPLETE
        assert status["suppressed"] is True

    def test_set_phase_override(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_set_phase(ob.PHASE_PROGRESSIVE)
        assert result["phase"] == ob.PHASE_PROGRESSIVE
        assert ob.cmd_status()["phase"] == ob.PHASE_PROGRESSIVE


class TestHouseholdParsing:
    def test_extract_section_body(self, isolated_fs):
        content = "## A\n- foo\n- bar\n\n## B\nxyz\n"
        assert ob._extract_section_body(content, "A") == "- foo\n- bar"
        assert ob._extract_section_body(content, "B") == "xyz"
        assert ob._extract_section_body(content, "Missing") == ""

    def test_scalar_value_detection(self, isolated_fs):
        body = "- **Name**: Ada\n- Role: builder\n- Timezone:"
        assert ob._scalar_value(body, "Name") == "Ada"
        assert ob._scalar_value(body, "Role") == "builder"
        assert ob._scalar_value(body, "Timezone") == ""

    def test_scalar_value_treats_fill_placeholder_as_empty(self, isolated_fs):
        body = "- **Name**: [FILL: your name]"
        assert ob._scalar_value(body, "Name") == ""

    def test_is_field_filled_scalar(self, isolated_fs):
        content = "## Primary user\n- Name: Ada\n- Role:\n"
        name_field = field_by_key("primary_user.name")
        role_field = field_by_key("primary_user.role")
        assert ob.is_field_filled(content, name_field) is True
        assert ob.is_field_filled(content, role_field) is False

    def test_is_field_filled_group_empty_markers(self, isolated_fs):
        content = "## Pets\n(none recorded)\n"
        pets = field_by_key("pets")
        assert ob.is_field_filled(content, pets) is False

    def test_is_field_filled_group_with_content(self, isolated_fs):
        content = "## Pets\n- Rex (dog, 4yo)\n"
        pets = field_by_key("pets")
        assert ob.is_field_filled(content, pets) is True


# ---------------------------------------------------------------------------
# M2 — Import parse, answer, decline
# ---------------------------------------------------------------------------

class TestAnswer:
    def test_answer_scalar_writes_household(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_answer("primary_user.name", "Ada")
        assert result["status"] == "answered"
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        assert "Name: Ada" in content or "**Name**: Ada" in content

    def test_answer_group_replaces_section_body(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_answer("children", "- Kemi (5)\n- Sam (3)")
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        # The placeholder "(none recorded)" should be gone
        children_body = ob._extract_section_body(content, "Children")
        assert "Kemi (5)" in children_body
        assert "(none recorded)" not in children_body

    def test_answer_unknown_field_exits(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with pytest.raises(SystemExit):
            ob.cmd_answer("does.not.exist", "whatever")

    def test_answer_empty_value_exits(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with pytest.raises(SystemExit):
            ob.cmd_answer("primary_user.name", "  ")

    def test_correction_overwrites(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_answer("primary_user.name", "Ada")
        ob.cmd_answer("primary_user.name", "Adaeze")
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        assert "Adaeze" in content
        assert "Name: Ada\n" not in content  # original single-name gone

    def test_answer_clears_queued_pointer(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with ob.get_conn() as conn:
            conn.execute(
                "UPDATE onboarding_meta SET queued_field_key = ? WHERE id = 1",
                ("home.address",),
            )
            conn.commit()
        ob.cmd_answer("home.address", "Austin, TX")
        status = ob.cmd_status()
        assert status["queued_field_key"] is None

    def test_scalar_answer_round_trips_through_household_md(self, isolated_fs):
        """Regression: the template must carry keys in a form that
        update_key_value matches, so is_field_filled returns True after
        cmd_answer without a duplicate line being appended."""
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_answer("primary_user.name", "Ada")
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        name_field = field_by_key("primary_user.name")
        assert ob.is_field_filled(content, name_field) is True
        # Exactly one Name line in the Primary user section
        primary_body = ob._extract_section_body(content, "Primary user")
        assert primary_body.lower().count("**name**:") == 1

    def test_two_group_fields_do_not_clobber_each_other(self, isolated_fs):
        """Regression: multiple fields must not share a section body.
        Answering one must not wipe another."""
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_answer("dietary.restrictions", "vegetarian")
        ob.cmd_answer("allergies.medical", "peanuts")
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        assert "vegetarian" in content
        assert "peanuts" in content

    def test_value_with_markdown_header_is_rejected(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with pytest.raises(SystemExit):
            ob.cmd_answer("partner", "- Chike\n## Primary user\n- Mallory")


class TestDecline:
    def test_decline_marks_declined(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_decline("home.address", note="user prefers privacy")
        assert result["status"] == "declined"
        gaps = [g["key"] for g in ob.cmd_gap()["gaps"]]
        assert "home.address" not in gaps

    def test_decline_clears_queued_pointer(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with ob.get_conn() as conn:
            conn.execute(
                "UPDATE onboarding_meta SET queued_field_key = ? WHERE id = 1",
                ("pets",),
            )
            conn.commit()
        ob.cmd_decline("pets")
        assert ob.cmd_status()["queued_field_key"] is None


class TestParseImport:
    def test_parse_import_marks_filled_fields(self, isolated_fs):
        content = HOUSEHOLD_TEMPLATE.replace(
            "- **Name**: [FILL: your name]", "- **Name**: Ada"
        ).replace(
            "- **Role**: [FILL: what you do]", "- **Role**: scientist"
        ).replace(
            "- **Address**: [FILL: city + state, or full address]",
            "- **Address**: Austin TX"
        ).replace(
            "## Partner\n(none)", "## Partner\n- Chike"
        ).replace(
            "## Children\n(none recorded)", "## Children\n- Zora (7)"
        )
        _write_household(content, workspace=isolated_fs)
        result = ob.cmd_parse_import()
        assert result["count"] >= 5  # all tier-1 fields resolved
        # Phase should have advanced to progressive (tier 1 done)
        assert result["phase"] == ob.PHASE_PROGRESSIVE

    def test_parse_import_leaves_empty_fields_unknown(self, isolated_fs):
        _write_household(HOUSEHOLD_TEMPLATE, workspace=isolated_fs)
        ob.cmd_parse_import()
        status = ob.cmd_status()
        # Nothing was actually filled
        assert status["phase"] == ob.PHASE_COLD
        assert status["counts"]["answered"] == 0


class TestSync:
    def test_sync_marks_handedited_fields_inferred(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        household = cu.USER_CONTEXT_DIR / "household.md"
        content = household.read_text().replace(
            "- **Name**: [FILL: your name]", "- **Name**: Alex"
        )
        household.write_text(content, encoding="utf-8")
        result = ob.cmd_sync()
        assert "primary_user.name" in result["updated"]
        with ob.get_conn() as conn:
            row = conn.execute(
                "SELECT source FROM onboarding_fields WHERE field_key = ?",
                ("primary_user.name",),
            ).fetchone()
            assert row["source"] == ob.SOURCE_INFERRED


# ---------------------------------------------------------------------------
# M3 — Heartbeat queue + cooldown + global decline
# ---------------------------------------------------------------------------

def _init_progressive() -> None:
    """queue-next only runs in progressive; tests that target queue behaviour
    start there rather than in cold_start."""
    ob.cmd_init(register_heartbeat=False)
    ob.cmd_set_phase(ob.PHASE_PROGRESSIVE)


class TestQueueNext:
    def test_queue_next_skipped_in_cold_start(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_queue_next()
        assert result["queued"] is None
        assert result["reason"] == "cold_start"

    def test_queue_next_picks_tier1(self, isolated_fs):
        _init_progressive()
        result = ob.cmd_queue_next()
        assert result["queued"] is not None
        assert result["tier"] == 1

    def test_queue_next_respects_cooldown(self, isolated_fs):
        _init_progressive()
        first = ob.cmd_queue_next()
        assert first["queued"] is not None
        # Clear the queue, but leave last_nudge_at inside the cooldown window
        with ob.get_conn() as conn:
            conn.execute("UPDATE onboarding_meta SET queued_field_key = NULL WHERE id = 1")
            conn.commit()
        second = ob.cmd_queue_next()
        assert second["queued"] is None
        assert second["reason"] == "cooldown"

    def test_queue_next_past_cooldown_picks_again(self, isolated_fs):
        _init_progressive()
        ob.cmd_queue_next()
        # Move last_nudge_at back 25h, clear queued slot
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with ob.get_conn() as conn:
            conn.execute(
                "UPDATE onboarding_meta SET last_nudge_at = ?, queued_field_key = NULL WHERE id = 1",
                (stale,),
            )
            conn.commit()
        again = ob.cmd_queue_next()
        assert again["queued"] is not None

    def test_queue_next_idempotent_while_queued(self, isolated_fs):
        _init_progressive()
        first = ob.cmd_queue_next()
        second = ob.cmd_queue_next()
        assert second["queued"] == first["queued"]
        assert second["reason"] == "already_queued"

    def test_queue_next_noop_when_terminal(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_set_phase(ob.PHASE_DECLINED)
        result = ob.cmd_queue_next()
        assert result["queued"] is None
        assert result["reason"] == "terminal_phase"


class TestConsumeQueued:
    def test_consume_returns_and_clears(self, isolated_fs):
        _init_progressive()
        queued = ob.cmd_queue_next()["queued"]
        result = ob.cmd_consume_queued()
        assert result["field_key"] == queued
        assert "phrasing" in result
        # Queued pointer cleared
        assert ob.cmd_status()["queued_field_key"] is None

    def test_consume_marks_field_asked(self, isolated_fs):
        _init_progressive()
        ob.cmd_queue_next()
        ob.cmd_consume_queued()
        with ob.get_conn() as conn:
            rows = conn.execute(
                "SELECT status, asked_count FROM onboarding_fields "
                "WHERE status = 'asked'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["asked_count"] == 1

    def test_consume_returns_none_when_nothing_queued(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_consume_queued()
        assert result["field_key"] is None

    def test_consume_handles_stale_pointer(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with ob.get_conn() as conn:
            conn.execute(
                "UPDATE onboarding_meta SET queued_field_key = 'bogus.key' WHERE id = 1"
            )
            conn.commit()
        result = ob.cmd_consume_queued()
        assert result["field_key"] is None
        assert result["reason"] == "stale"


class TestReset:
    def test_reset_clears_state(self, isolated_fs):
        _init_progressive()
        ob.cmd_answer("primary_user.name", "Ada")
        ob.cmd_queue_next()
        result = ob.cmd_reset()
        assert result["status"] == "reset"
        status = ob.cmd_status()
        assert status["phase"] == ob.PHASE_COLD
        assert status["counts"]["answered"] == 0
        assert status["queued_field_key"] is None

    def test_reset_empty_household_flag(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_answer("primary_user.name", "Ada")
        ob.cmd_reset(empty_household=True)
        content = (cu.USER_CONTEXT_DIR / "household.md").read_text()
        assert "[FILL: your name]" in content
        assert "Ada" not in content


class TestGlobalDecline:
    def test_global_decline_sets_phase_and_clears_queue(self, isolated_fs):
        _init_progressive()
        ob.cmd_queue_next()
        result = ob.cmd_global_decline()
        assert result["status"] == "declined_global"
        status = ob.cmd_status()
        assert status["phase"] == ob.PHASE_DECLINED
        assert status["queued_field_key"] is None
        assert status["suppressed"] is True


# ---------------------------------------------------------------------------
# CLI — thin smoke tests via main(argv)
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_init(self, isolated_fs):
        result = _capture(["init", "--no-heartbeat"])
        assert result["status"] == "initialized"

    def test_cli_answer_then_status(self, isolated_fs):
        _capture(["init", "--no-heartbeat"])
        _capture(["answer", "--field-key", "primary_user.name", "--value", "Ada"])
        status = _capture(["status"])
        assert status["counts"]["answered"] >= 1

    def test_cli_gap_filter(self, isolated_fs):
        _capture(["init", "--no-heartbeat"])
        result = _capture(["gap", "--tier", "1"])
        for g in result["gaps"]:
            assert g["tier"] == 1


# ---------------------------------------------------------------------------
# Setup checklist — workspace, context_import, byok
# ---------------------------------------------------------------------------

class TestSetupChecklist:
    """The setup checklist is the cold-start priority ladder.

    On a fresh tenant with no Google token and no BYOK key, the priority
    order is workspace → context_import → byok. Each item must be detected
    automatically, asked at most once per 24h, and respect explicit
    `declined` even if the auto-detect later flips positive.
    """

    def _default_tier(self, monkeypatch):
        for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")

    def _byok(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def test_status_includes_setup_block(self, isolated_fs, monkeypatch):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_status()
        assert "setup" in result
        for item in ob.SETUP_ITEMS:
            assert item in result["setup"]
            assert result["setup"][item]["status"] == ob.SETUP_STATUS_UNKNOWN
        assert result["model_tier"] == "default-tier"

    def test_next_setup_priority_order_workspace_first(
        self, isolated_fs, monkeypatch
    ):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_status()
        assert result["next_setup"] == ob.SETUP_WORKSPACE

    def test_next_setup_advances_after_workspace_done(
        self, isolated_fs, monkeypatch
    ):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_DONE)
        result = ob.cmd_status()
        assert result["next_setup"] == ob.SETUP_CONTEXT_IMPORT

    def test_next_setup_advances_to_byok_after_context_done(
        self, isolated_fs, monkeypatch
    ):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_DONE)
        ob.cmd_setup_mark(ob.SETUP_CONTEXT_IMPORT, ob.SETUP_STATUS_DONE)
        result = ob.cmd_status()
        assert result["next_setup"] == ob.SETUP_BYOK

    def test_next_setup_null_when_all_done(self, isolated_fs, monkeypatch):
        self._byok(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        for item in ob.SETUP_ITEMS:
            ob.cmd_setup_mark(item, ob.SETUP_STATUS_DONE)
        result = ob.cmd_status()
        assert result["next_setup"] is None

    def test_next_setup_skips_declined_items(self, isolated_fs, monkeypatch):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_DECLINED)
        result = ob.cmd_status()
        assert result["next_setup"] == ob.SETUP_CONTEXT_IMPORT

    def test_setup_mark_asked_engages_24h_cooldown(
        self, isolated_fs, monkeypatch
    ):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_ASKED)
        # Within cooldown window — should not surface workspace again.
        result = ob.cmd_status()
        assert result["next_setup"] == ob.SETUP_CONTEXT_IMPORT
        assert result["setup"]["workspace"]["status"] == ob.SETUP_STATUS_ASKED
        assert result["setup"]["workspace"]["asked_count"] == 1

    def test_setup_mark_asked_resurfaces_after_cooldown(
        self, isolated_fs, monkeypatch
    ):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_ASKED)
        # Backdate the ask 25h so the cooldown has elapsed.
        past = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with ob.get_conn() as conn:
            conn.execute(
                "UPDATE onboarding_setup SET last_asked_at = ? WHERE item = ?",
                (past, ob.SETUP_WORKSPACE),
            )
            conn.commit()
        result = ob.cmd_status()
        assert result["next_setup"] == ob.SETUP_WORKSPACE

    def test_workspace_auto_detected_when_token_appears(
        self, isolated_fs, monkeypatch, tmp_path
    ):
        """Drop a token pickle on disk → next status flips workspace to done."""
        self._default_tier(monkeypatch)
        # Point google_auth at our temp tokens dir so the helper finds the
        # file we drop.
        from tools import google_auth
        tokens_dir = tmp_path / "tokens"
        tokens_dir.mkdir(parents=True)
        monkeypatch.setattr(google_auth, "TOKENS_DIR", tokens_dir)
        monkeypatch.setattr(google_auth, "LEGACY_TOKEN", tokens_dir / "missing.pickle")

        ob.cmd_init(register_heartbeat=False)
        before = ob.cmd_status()
        assert before["setup"]["workspace"]["status"] == ob.SETUP_STATUS_UNKNOWN
        assert before["setup"]["workspace"]["detected"] == "missing"

        # User completes OAuth → token pickle appears.
        (tokens_dir / "primary.pickle").write_bytes(b"fake-token")

        after = ob.cmd_status()
        assert after["setup"]["workspace"]["detected"] == "connected"
        assert after["setup"]["workspace"]["status"] == ob.SETUP_STATUS_DONE
        assert after["next_setup"] == ob.SETUP_CONTEXT_IMPORT

    def test_declined_is_never_overridden_by_auto_detect(
        self, isolated_fs, monkeypatch, tmp_path
    ):
        """Even if a token shows up later, declined stays declined."""
        self._default_tier(monkeypatch)
        from tools import google_auth
        tokens_dir = tmp_path / "tokens"
        tokens_dir.mkdir(parents=True)
        monkeypatch.setattr(google_auth, "TOKENS_DIR", tokens_dir)
        monkeypatch.setattr(google_auth, "LEGACY_TOKEN", tokens_dir / "missing.pickle")

        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_DECLINED)
        (tokens_dir / "primary.pickle").write_bytes(b"fake-token")

        result = ob.cmd_status()
        assert result["setup"]["workspace"]["status"] == ob.SETUP_STATUS_DECLINED
        assert result["next_setup"] == ob.SETUP_CONTEXT_IMPORT

    def test_context_auto_detected_when_tier1_complete(
        self, isolated_fs, monkeypatch
    ):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_DONE)
        # Fill all tier 1 fields.
        ob.cmd_answer("primary_user.name", "Ada")
        ob.cmd_answer("primary_user.role", "designer")
        ob.cmd_answer("home.address", "Austin, TX")
        ob.cmd_answer("partner", "- Chike")
        ob.cmd_answer("children", "- Zora (age 7)")

        result = ob.cmd_status()
        assert result["setup"]["context_import"]["detected"] == "done"
        assert result["setup"]["context_import"]["status"] == ob.SETUP_STATUS_DONE
        assert result["next_setup"] == ob.SETUP_BYOK

    def test_byok_detected_when_user_key_present(
        self, isolated_fs, monkeypatch
    ):
        self._byok(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_status()
        assert result["model_tier"] == "BYOK"
        assert result["setup"]["byok"]["detected"] == "active"
        assert result["setup"]["byok"]["status"] == ob.SETUP_STATUS_DONE

    def test_byok_silenced_on_managed_tier_with_user_key(
        self, isolated_fs, monkeypatch
    ):
        # Managed-tier container (OpenRouter present) where the user has also
        # added a BYOK key. model_tier must match switch_model._discover_tier
        # (which gives OpenRouter precedence), but the BYOK nudge must still
        # be silenced — the user already has a key.
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        ob.cmd_init(register_heartbeat=False)
        result = ob.cmd_status()
        assert result["model_tier"] == "default-tier"
        assert result["setup"]["byok"]["detected"] == "active"
        assert result["setup"]["byok"]["status"] == ob.SETUP_STATUS_DONE

    def test_setup_mark_invalid_item_exits(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with pytest.raises(SystemExit):
            ob.cmd_setup_mark("not_a_real_item", ob.SETUP_STATUS_ASKED)

    def test_setup_mark_invalid_status_exits(self, isolated_fs):
        ob.cmd_init(register_heartbeat=False)
        with pytest.raises(SystemExit):
            ob.cmd_setup_mark(ob.SETUP_WORKSPACE, "garbage")

    def test_reset_clears_setup_state(self, isolated_fs, monkeypatch):
        self._default_tier(monkeypatch)
        ob.cmd_init(register_heartbeat=False)
        ob.cmd_setup_mark(ob.SETUP_WORKSPACE, ob.SETUP_STATUS_DECLINED)
        ob.cmd_reset()
        result = ob.cmd_status()
        assert result["setup"]["workspace"]["status"] == ob.SETUP_STATUS_UNKNOWN
        assert result["next_setup"] == ob.SETUP_WORKSPACE

    def test_cli_setup_mark_round_trip(self, isolated_fs, monkeypatch):
        self._default_tier(monkeypatch)
        _capture(["init", "--no-heartbeat"])
        result = _capture(
            ["setup-mark", "--item", "workspace", "--status", "declined",
             "--note", "user said no"]
        )
        assert result["item"] == "workspace"
        assert result["status"] == "declined"
        status = _capture(["status"])
        assert status["next_setup"] == "context_import"

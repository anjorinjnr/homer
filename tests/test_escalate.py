"""Tests for Phase 2 escalation flow: escalate, resolve, deliver."""

import json
from pathlib import Path

import pytest

import tools.scope_store as ss
import tools.escalate as escalate
import tools.resolve_escalation as resolve_escalation
import tools.deliver_escalation as deliver_escalation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Provide a fresh, isolated scope DB for each test."""
    return tmp_path / "test_scopes.db"


def _make_scope(db: Path, scope_id: str = "rel_test_scope") -> dict:
    """Create a scope and return its envelope."""
    envelope = ss.make_minimal_envelope(
        scope_id=scope_id,
        name="TestGuest",
        participant_id="15551234567@s.whatsapp.net",
        event_id="test_event",
    )
    ss.create_scope(envelope, db)
    return envelope


# ---------------------------------------------------------------------------
# escalate.py — Creating escalations
# ---------------------------------------------------------------------------

class TestCreateEscalation:
    def test_basic_create(self, db):
        _make_scope(db)
        result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="Guest asked about budget",
            assessment="Need dollar amounts from spreadsheet",
            db_path=db,
        )
        assert result["ok"] is True
        assert result["scope_id"] == "rel_test_scope"
        assert result["trigger_type"] == "context_missing"
        assert result["status"] == "pending"
        assert "escalation_id" in result

    def test_escalation_in_db(self, db):
        _make_scope(db)
        result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="capability_exceeded",
            message="Guest wants to book a flight",
            db_path=db,
        )
        with ss.get_conn(db) as conn:
            row = conn.execute(
                "SELECT * FROM escalations WHERE escalation_id = ?",
                (result["escalation_id"],),
            ).fetchone()
        assert row is not None
        assert row["status"] == "pending"
        assert row["trigger_type"] == "capability_exceeded"
        assert row["triggering_message"] == "Guest wants to book a flight"

    def test_escalation_appended_to_envelope_log(self, db):
        _make_scope(db)
        result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="disclosure_risk",
            message="Guest asked about finances",
            assessment="Might be fishing for info",
            db_path=db,
        )
        envelope = ss.get_scope("rel_test_scope", db)
        assert len(envelope["escalation_log"]) == 1
        entry = envelope["escalation_log"][0]
        assert entry["escalation_id"] == result["escalation_id"]
        assert entry["trigger_type"] == "disclosure_risk"
        assert entry["status"] == "pending"

    def test_invalid_trigger_type(self, db):
        _make_scope(db)
        with pytest.raises(ValueError, match="Invalid trigger_type"):
            escalate.create_escalation(
                scope_id="rel_test_scope",
                trigger_type="invalid_type",
                message="test",
                db_path=db,
            )

    def test_nonexistent_scope(self, db):
        # Initialize DB (get_conn creates tables)
        ss.get_conn(db)
        with pytest.raises(ValueError, match="not found"):
            escalate.create_escalation(
                scope_id="rel_nonexistent",
                trigger_type="context_missing",
                message="test",
                db_path=db,
            )

    def test_all_trigger_types(self, db):
        _make_scope(db)
        for trigger in escalate.VALID_TRIGGER_TYPES:
            result = escalate.create_escalation(
                scope_id="rel_test_scope",
                trigger_type=trigger,
                message=f"Testing {trigger}",
                db_path=db,
            )
            assert result["ok"] is True

    def test_urgency_real_time(self, db):
        _make_scope(db)
        result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="Urgent request",
            urgency="real_time",
            db_path=db,
        )
        assert result["urgency"] == "real_time"

    def test_invalid_urgency(self, db):
        _make_scope(db)
        with pytest.raises(ValueError, match="Invalid urgency"):
            escalate.create_escalation(
                scope_id="rel_test_scope",
                trigger_type="context_missing",
                message="test",
                urgency="critical",
                db_path=db,
            )


# ---------------------------------------------------------------------------
# resolve_escalation.py — Resolving escalations
# ---------------------------------------------------------------------------

class TestResolveEscalation:
    def _create_pending(self, db) -> str:
        """Helper: create a scope + pending escalation, return escalation_id."""
        _make_scope(db)
        result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="Guest asked about lodging cost",
            assessment="Need budget details",
            db_path=db,
        )
        return result["escalation_id"]

    def test_resolve_with_drafted_response(self, db):
        esc_id = self._create_pending(db)
        result = resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="response_drafted",
            drafted_response="The Airbnb costs $450/night for 3 nights.",
            db_path=db,
            skip_rebuild=True,
        )
        assert result["ok"] is True
        assert result["status"] == "resolved"
        assert result["action"] == "response_drafted"
        assert result["resolution"]["drafted_response"] == "The Airbnb costs $450/night for 3 nights."

    def test_resolve_with_context_injection(self, db):
        esc_id = self._create_pending(db)
        result = resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="context_injected",
            context="Budget: $2000 total, Airbnb $450/night",
            db_path=db,
            skip_rebuild=True,
        )
        assert result["ok"] is True
        assert result["action"] == "context_injected"

        # Verify context was added to scope envelope
        envelope = ss.get_scope("rel_test_scope", db)
        injected = envelope["context_layers"]["injected"]
        assert len(injected) == 1
        assert injected[0]["content"] == "Budget: $2000 total, Airbnb $450/night"
        assert injected[0]["injected_by_escalation"] == esc_id

    def test_resolve_updates_db(self, db):
        esc_id = self._create_pending(db)
        resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="response_drafted",
            drafted_response="Answer here",
            db_path=db,
            skip_rebuild=True,
        )
        with ss.get_conn(db) as conn:
            row = conn.execute(
                "SELECT * FROM escalations WHERE escalation_id = ?",
                (esc_id,),
            ).fetchone()
        assert row["status"] == "resolved"
        assert row["resolved_at"] is not None
        resolution = json.loads(row["resolution"])
        assert resolution["drafted_response"] == "Answer here"

    def test_resolve_updates_envelope_log(self, db):
        esc_id = self._create_pending(db)
        resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="response_drafted",
            drafted_response="Done",
            db_path=db,
            skip_rebuild=True,
        )
        envelope = ss.get_scope("rel_test_scope", db)
        log_entry = envelope["escalation_log"][0]
        assert log_entry["status"] == "resolved"
        assert log_entry["resolution"]["action_taken"] == "response_drafted"

    def test_resolve_nonexistent(self, db):
        ss.get_conn(db)  # init tables
        with pytest.raises(ValueError, match="not found"):
            resolve_escalation.resolve_escalation(
                escalation_id="fake-id",
                action="response_drafted",
                drafted_response="Some response",
                db_path=db,
                skip_rebuild=True,
            )

    def test_resolve_already_resolved(self, db):
        esc_id = self._create_pending(db)
        resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="response_drafted",
            drafted_response="First",
            db_path=db,
            skip_rebuild=True,
        )
        with pytest.raises(ValueError, match="already"):
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="response_drafted",
                drafted_response="Second",
                db_path=db,
                skip_rebuild=True,
            )

    def test_invalid_action(self, db):
        esc_id = self._create_pending(db)
        with pytest.raises(ValueError, match="Invalid action"):
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="invalid_action",
                db_path=db,
                skip_rebuild=True,
            )

    def test_scope_terminated_deferred(self, db):
        """scope_terminated does NOT terminate immediately — deferred until delivery."""
        esc_id = self._create_pending(db)
        resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="scope_terminated",
            drafted_response="Goodbye!",
            db_path=db,
            skip_rebuild=True,
        )
        # Scope is still active (termination deferred until guest delivers)
        envelope = ss.get_scope("rel_test_scope", db)
        assert envelope["_status"] == "active"

        # After delivery, scope is terminated
        result = deliver_escalation.deliver_escalation(
            escalation_id=esc_id, db_path=db,
            active_scope_ids=["rel_test_scope"],
        )
        assert result["scope_terminated"] is True
        envelope = ss.get_scope("rel_test_scope", db)
        assert envelope["_status"] == "terminated"


# ---------------------------------------------------------------------------
# deliver_escalation.py — Delivering resolved escalation responses
# ---------------------------------------------------------------------------

class TestDeliverEscalation:
    def _create_and_resolve(self, db, action="response_drafted", **kwargs) -> str:
        """Helper: create + resolve an escalation, return escalation_id."""
        _make_scope(db)
        create_result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="Guest asked about lodging cost",
            db_path=db,
        )
        esc_id = create_result["escalation_id"]
        resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action=action,
            db_path=db,
            skip_rebuild=True,
            **kwargs,
        )
        return esc_id

    def test_deliver_drafted_response(self, db):
        esc_id = self._create_and_resolve(
            db, action="response_drafted",
            drafted_response="The Airbnb costs $450/night.",
        )
        result = deliver_escalation.deliver_escalation(
            escalation_id=esc_id,
            db_path=db,
        )
        assert result["ok"] is True
        assert result["drafted_response"] == "The Airbnb costs $450/night."
        assert result["delivered_at"] is not None

    def test_deliver_context_injected(self, db):
        esc_id = self._create_and_resolve(
            db, action="context_injected",
            context="Budget: $2000",
        )
        result = deliver_escalation.deliver_escalation(
            escalation_id=esc_id,
            db_path=db,
        )
        assert result["ok"] is True
        assert result["context_injected"] is True
        assert "original_message" in result

    def test_deliver_marks_outbound_sent(self, db):
        esc_id = self._create_and_resolve(
            db, action="response_drafted",
            drafted_response="Answer",
        )
        deliver_escalation.deliver_escalation(
            escalation_id=esc_id,
            db_path=db,
        )
        with ss.get_conn(db) as conn:
            row = conn.execute(
                "SELECT * FROM escalations WHERE escalation_id = ?",
                (esc_id,),
            ).fetchone()
        assert row["outbound_sent"] == 1
        assert row["outbound_sent_at"] is not None

    def test_deliver_unresolved_fails(self, db):
        """Attempting to deliver an unresolved escalation should fail."""
        _make_scope(db)
        create_result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="Still pending",
            db_path=db,
        )
        with pytest.raises(ValueError, match="not resolved"):
            deliver_escalation.deliver_escalation(
                escalation_id=create_result["escalation_id"],
                db_path=db,
            )

    def test_deliver_already_delivered_fails(self, db):
        esc_id = self._create_and_resolve(
            db, action="response_drafted",
            drafted_response="Answer",
        )
        deliver_escalation.deliver_escalation(
            escalation_id=esc_id,
            db_path=db,
        )
        with pytest.raises(ValueError, match="already been delivered"):
            deliver_escalation.deliver_escalation(
                escalation_id=esc_id,
                db_path=db,
            )

    def test_deliver_nonexistent_fails(self, db):
        ss.get_conn(db)  # init tables
        with pytest.raises(ValueError, match="not found"):
            deliver_escalation.deliver_escalation(
                escalation_id="fake-id",
                db_path=db,
            )


# ---------------------------------------------------------------------------
# scope_store.py — Pending escalations listing
# ---------------------------------------------------------------------------

class TestPendingEscalations:
    def test_empty_when_no_escalations(self, db):
        ss.get_conn(db)  # init tables
        result = ss.get_pending_escalations(db)
        assert result == []

    def test_lists_pending_only(self, db):
        _make_scope(db)
        # Create two escalations
        r1 = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="First",
            db_path=db,
        )
        r2 = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="capability_exceeded",
            message="Second",
            db_path=db,
        )
        # Resolve the first
        resolve_escalation.resolve_escalation(
            escalation_id=r1["escalation_id"],
            action="response_drafted",
            drafted_response="Done",
            db_path=db,
            skip_rebuild=True,
        )

        pending = ss.get_pending_escalations(db)
        assert len(pending) == 1
        assert pending[0]["escalation_id"] == r2["escalation_id"]
        assert pending[0]["trigger_type"] == "capability_exceeded"

    def test_includes_scope_context(self, db):
        _make_scope(db)
        escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="Need info",
            db_path=db,
        )
        pending = ss.get_pending_escalations(db)
        assert len(pending) == 1
        assert "participants" in pending[0]
        assert "TestGuest" in pending[0]["participants"]

    def test_multiple_scopes(self, db):
        _make_scope(db, "rel_scope_a")
        _make_scope(db, "rel_scope_b")
        escalate.create_escalation(
            scope_id="rel_scope_a",
            trigger_type="context_missing",
            message="From A",
            db_path=db,
        )
        escalate.create_escalation(
            scope_id="rel_scope_b",
            trigger_type="uncertainty",
            message="From B",
            db_path=db,
        )
        pending = ss.get_pending_escalations(db)
        assert len(pending) == 2
        scope_ids = {p["scope_id"] for p in pending}
        assert scope_ids == {"rel_scope_a", "rel_scope_b"}


# ---------------------------------------------------------------------------
# scope_store.py — Resolved undelivered escalations
# ---------------------------------------------------------------------------

class TestResolvedUndeliveredEscalations:
    def test_empty_when_none(self, db):
        ss.get_conn(db)
        result = ss.get_resolved_undelivered_escalations(db)
        assert result == []

    def test_lists_resolved_undelivered(self, db):
        _make_scope(db)
        r = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="test",
            db_path=db,
        )
        resolve_escalation.resolve_escalation(
            escalation_id=r["escalation_id"],
            action="response_drafted",
            drafted_response="The answer",
            db_path=db,
            skip_rebuild=True,
        )
        undelivered = ss.get_resolved_undelivered_escalations(db)
        assert len(undelivered) == 1
        assert undelivered[0]["has_drafted_response"] is True

    def test_excludes_delivered(self, db):
        _make_scope(db)
        r = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="test",
            db_path=db,
        )
        resolve_escalation.resolve_escalation(
            escalation_id=r["escalation_id"],
            action="response_drafted",
            drafted_response="The answer",
            db_path=db,
            skip_rebuild=True,
        )
        deliver_escalation.deliver_escalation(
            escalation_id=r["escalation_id"],
            db_path=db,
        )
        undelivered = ss.get_resolved_undelivered_escalations(db)
        assert len(undelivered) == 0

    def test_scope_id_filter_prevents_cross_scope_leak(self, db):
        """Filtering by scope_id must only return that scope's escalations."""
        _make_scope(db, "rel_alice")
        _make_scope(db, "rel_bob")
        # Create and resolve escalations for both scopes
        r_alice = escalate.create_escalation(
            scope_id="rel_alice",
            trigger_type="context_missing",
            message="Alice question",
            db_path=db,
        )
        r_bob = escalate.create_escalation(
            scope_id="rel_bob",
            trigger_type="context_missing",
            message="Bob question",
            db_path=db,
        )
        for esc_id in [r_alice["escalation_id"], r_bob["escalation_id"]]:
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="response_drafted",
                drafted_response="Answer",
                db_path=db,
                skip_rebuild=True,
            )

        # Without filter: sees both
        all_undelivered = ss.get_resolved_undelivered_escalations(db)
        assert len(all_undelivered) == 2

        # With filter: only the matching scope
        alice_only = ss.get_resolved_undelivered_escalations(db, scope_id="rel_alice")
        assert len(alice_only) == 1
        assert alice_only[0]["scope_id"] == "rel_alice"

        bob_only = ss.get_resolved_undelivered_escalations(db, scope_id="rel_bob")
        assert len(bob_only) == 1
        assert bob_only[0]["scope_id"] == "rel_bob"

    def test_scope_id_filter_empty_for_nonexistent(self, db):
        _make_scope(db)
        r = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="test",
            db_path=db,
        )
        resolve_escalation.resolve_escalation(
            escalation_id=r["escalation_id"],
            action="response_drafted",
            drafted_response="Answer",
            db_path=db,
            skip_rebuild=True,
        )
        result = ss.get_resolved_undelivered_escalations(db, scope_id="rel_nonexistent")
        assert result == []


# ---------------------------------------------------------------------------
# deliver_escalation.py — list_pending_for_scope
# ---------------------------------------------------------------------------

class TestListPendingForScope:
    def test_returns_only_matching_scope(self, db):
        _make_scope(db, "rel_scope_a")
        _make_scope(db, "rel_scope_b")
        r_a = escalate.create_escalation(
            scope_id="rel_scope_a",
            trigger_type="context_missing",
            message="From A",
            db_path=db,
        )
        r_b = escalate.create_escalation(
            scope_id="rel_scope_b",
            trigger_type="context_missing",
            message="From B",
            db_path=db,
        )
        for esc_id in [r_a["escalation_id"], r_b["escalation_id"]]:
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="response_drafted",
                drafted_response="Answer",
                db_path=db,
                skip_rebuild=True,
            )

        result = deliver_escalation.list_pending_for_scope(
            scope_id="rel_scope_a",
            db_path=db,
        )
        assert len(result) == 1
        assert result[0]["scope_id"] == "rel_scope_a"

    def test_empty_when_no_pending(self, db):
        ss.get_conn(db)
        result = deliver_escalation.list_pending_for_scope(
            scope_id="rel_anything",
            db_path=db,
        )
        assert result == []


# ---------------------------------------------------------------------------
# End-to-end flow
# ---------------------------------------------------------------------------

class TestEndToEndFlow:
    def test_full_escalation_lifecycle(self, db):
        """Test the complete flow: create -> resolve -> deliver."""
        _make_scope(db)

        # 1. Guest agent creates escalation
        create_result = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="What's the budget for lodging?",
            assessment="Guest needs dollar amounts from budget doc",
            db_path=db,
        )
        esc_id = create_result["escalation_id"]
        assert create_result["status"] == "pending"

        # 2. Main agent polls and finds pending
        pending = ss.get_pending_escalations(db)
        assert len(pending) == 1
        assert pending[0]["escalation_id"] == esc_id

        # 3. Main agent resolves with drafted response
        resolve_result = resolve_escalation.resolve_escalation(
            escalation_id=esc_id,
            action="response_drafted",
            drafted_response="The total lodging budget is $1,350 (3 nights at $450/night).",
            db_path=db,
            skip_rebuild=True,
        )
        assert resolve_result["status"] == "resolved"

        # 4. No more pending
        assert len(ss.get_pending_escalations(db)) == 0

        # 5. Guest agent polls and finds undelivered
        undelivered = ss.get_resolved_undelivered_escalations(db)
        assert len(undelivered) == 1

        # 6. Guest agent delivers
        deliver_result = deliver_escalation.deliver_escalation(
            escalation_id=esc_id,
            db_path=db,
        )
        assert deliver_result["ok"] is True
        assert deliver_result["drafted_response"] == "The total lodging budget is $1,350 (3 nights at $450/night)."

        # 7. No more undelivered
        assert len(ss.get_resolved_undelivered_escalations(db)) == 0


# ---------------------------------------------------------------------------
# Security: active_scopes.json scope auto-detection (escalate.py)
# ---------------------------------------------------------------------------

class TestEscalateAutoScope:
    # escalate now reads sender-scoped IDs via tools/guest_scope_guard.py, which
    # looks at $HOMER_GUEST_WORKSPACE/current_sender_scopes.json first and falls
    # back to active_scopes.json. Tests monkeypatch HOMER_GUEST_WORKSPACE into an
    # isolated tmp dir so we can control the list.

    def _set_sender_scopes(self, tmp_path, monkeypatch, scope_ids):
        ws = tmp_path / "guest_ws"
        ws.mkdir(exist_ok=True)
        (ws / "current_sender_scopes.json").write_text(json.dumps(scope_ids))
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(ws))

    def test_single_scope_auto_detected(self, tmp_path, db, monkeypatch):
        """When the sender has one scope, escalate auto-uses it."""
        _make_scope(db)
        self._set_sender_scopes(tmp_path, monkeypatch, ["rel_test_scope"])

        result = escalate._resolve_scope_id(None)
        assert result == "rel_test_scope"

    def test_multiple_scopes_requires_scope_id(self, tmp_path, monkeypatch):
        """Multiple sender scopes without --scope-id should fail."""
        self._set_sender_scopes(tmp_path, monkeypatch, ["rel_a", "rel_b"])

        with pytest.raises(SystemExit):
            escalate._resolve_scope_id(None)

    def test_multiple_scopes_valid_scope_id(self, tmp_path, monkeypatch):
        """Multiple scopes with a valid --scope-id should succeed."""
        self._set_sender_scopes(tmp_path, monkeypatch, ["rel_a", "rel_b"])

        result = escalate._resolve_scope_id("rel_b")
        assert result == "rel_b"

    def test_multiple_scopes_invalid_scope_id_rejected(self, tmp_path, monkeypatch):
        """Multiple scopes with a scope_id NOT in the sender's list should fail."""
        self._set_sender_scopes(tmp_path, monkeypatch, ["rel_a", "rel_b"])

        with pytest.raises(SystemExit):
            escalate._resolve_scope_id("rel_evil")

    def test_no_active_scopes_fails(self, tmp_path, monkeypatch):
        """Empty per-sender list should fail."""
        self._set_sender_scopes(tmp_path, monkeypatch, [])

        with pytest.raises(SystemExit):
            escalate._resolve_scope_id(None)

    def test_missing_file_fails(self, tmp_path, monkeypatch):
        """Missing per-sender file (and no legacy fallback) should fail."""
        ws = tmp_path / "empty_guest_ws"
        ws.mkdir()
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(ws))

        with pytest.raises(SystemExit):
            escalate._resolve_scope_id(None)

    def test_does_not_fall_back_to_legacy_active_scopes(self, tmp_path, monkeypatch):
        """Prod regression: an unscoped sender with the global active_scopes.json
        present must NOT silently widen to the global list — that's how Adam
        discovered maya_5th_bday."""
        ws = tmp_path / "guest_ws_legacy"
        ws.mkdir()
        (ws / "active_scopes.json").write_text(json.dumps(["rel_legacy"]))
        monkeypatch.setenv("HOMER_GUEST_WORKSPACE", str(ws))

        with pytest.raises(SystemExit):
            escalate._resolve_scope_id(None)


# ---------------------------------------------------------------------------
# Security: deliver_escalation.py scope membership validation
# ---------------------------------------------------------------------------

class TestDeliverScopeValidation:
    def test_deliver_rejects_cross_scope_escalation(self, db):
        """Delivering an escalation not in the active scope list should fail."""
        _make_scope(db, "rel_scope_a")
        _make_scope(db, "rel_scope_b")

        # Create + resolve escalation for scope_b
        r = escalate.create_escalation(
            scope_id="rel_scope_b",
            trigger_type="context_missing",
            message="From B",
            db_path=db,
        )
        resolve_escalation.resolve_escalation(
            escalation_id=r["escalation_id"],
            action="response_drafted",
            drafted_response="Answer for B",
            db_path=db,
            skip_rebuild=True,
        )

        # Attempt to deliver with active_scope_ids containing only scope_a
        with pytest.raises(ValueError, match="does not belong to any active scope"):
            deliver_escalation.deliver_escalation(
                escalation_id=r["escalation_id"],
                db_path=db,
                active_scope_ids=["rel_scope_a"],
            )

    def test_deliver_allows_matching_scope(self, db):
        """Delivering an escalation in the active scope list should work."""
        _make_scope(db)
        r = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="test",
            db_path=db,
        )
        resolve_escalation.resolve_escalation(
            escalation_id=r["escalation_id"],
            action="response_drafted",
            drafted_response="Answer",
            db_path=db,
            skip_rebuild=True,
        )
        result = deliver_escalation.deliver_escalation(
            escalation_id=r["escalation_id"],
            db_path=db,
            active_scope_ids=["rel_test_scope"],
        )
        assert result["ok"] is True

    def test_list_pending_respects_active_scopes(self, db):
        """list_pending_for_scope only returns matching scope's escalations."""
        _make_scope(db, "rel_alice")
        _make_scope(db, "rel_bob")

        for sid in ["rel_alice", "rel_bob"]:
            r = escalate.create_escalation(
                scope_id=sid,
                trigger_type="context_missing",
                message=f"From {sid}",
                db_path=db,
            )
            resolve_escalation.resolve_escalation(
                escalation_id=r["escalation_id"],
                action="response_drafted",
                drafted_response="Answer",
                db_path=db,
                skip_rebuild=True,
            )

        # Only query for alice
        result = deliver_escalation.list_pending_for_scope(
            scope_id="rel_alice", db_path=db,
        )
        assert len(result) == 1
        assert result[0]["scope_id"] == "rel_alice"


# ---------------------------------------------------------------------------
# Security: resolve_escalation.py empty payload validation
# ---------------------------------------------------------------------------

class TestResolveValidation:
    def _create_pending(self, db) -> str:
        _make_scope(db)
        r = escalate.create_escalation(
            scope_id="rel_test_scope",
            trigger_type="context_missing",
            message="test",
            db_path=db,
        )
        return r["escalation_id"]

    def test_response_drafted_rejects_empty(self, db):
        esc_id = self._create_pending(db)
        with pytest.raises(ValueError, match="non-empty --drafted-response"):
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="response_drafted",
                drafted_response="",
                db_path=db,
                skip_rebuild=True,
            )

    def test_response_drafted_rejects_whitespace(self, db):
        esc_id = self._create_pending(db)
        with pytest.raises(ValueError, match="non-empty --drafted-response"):
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="response_drafted",
                drafted_response="   ",
                db_path=db,
                skip_rebuild=True,
            )

    def test_context_injected_rejects_empty(self, db):
        esc_id = self._create_pending(db)
        with pytest.raises(ValueError, match="non-empty --context"):
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="context_injected",
                context="",
                db_path=db,
                skip_rebuild=True,
            )

    def test_context_injected_rejects_whitespace(self, db):
        esc_id = self._create_pending(db)
        with pytest.raises(ValueError, match="non-empty --context"):
            resolve_escalation.resolve_escalation(
                escalation_id=esc_id,
                action="context_injected",
                context="   ",
                db_path=db,
                skip_rebuild=True,
            )

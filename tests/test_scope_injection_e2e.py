"""End-to-end scope isolation test — validates the nanobot↔homer contract.

Spans both sides of the wire:
  nanobot.agent.loop.AgentLoop._get_scope_context
    → import "scope_store:render_scope_context_for_sender"
    → DB lookup + section render
    → return string

Runs without an LLM. This is the fastest reliable check that a guest
receives ONLY their own scope context.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The nanobot provider hook imports scope_store by bare name via importlib,
# so scope_store.py must be on sys.path. Register the SAME module under both
# "scope_store" and "tools.scope_store" in sys.modules so this test can do
# identity comparisons against the cached provider fn.
REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "tools"))
import scope_store as ss  # noqa: E402
sys.modules["tools.scope_store"] = ss

from nanobot.agent.loop import AgentLoop  # noqa: E402

# The local nanobot editable install can be on a branch that has refactored
# scope_context out of AgentLoop (e.g. feat/posthog-instrumentation). The
# deployed homer-patches branch still has it, so skip gracefully when the
# locally-checked-out version doesn't expose the symbol.
pytestmark = pytest.mark.skipif(
    not hasattr(AgentLoop, "_get_scope_context"),
    reason="installed nanobot lacks _get_scope_context (e.g. branch diverged from homer-patches)",
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point scope_store at a tmp DB so the test doesn't touch real data."""
    db_path = tmp_path / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db_path))
    yield db_path


@pytest.fixture
def loop_with_provider(tmp_path):
    """Minimal AgentLoop shell wired to the real scope_store provider."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = tmp_path
    loop._scope_context_provider = "scope_store:render_scope_context_for_sender"
    loop._scope_context_fn = None
    return loop


def _seed_two_scopes(db: Path) -> None:
    """Adam → MTB scope, Sam → Birthday scope. Fully disjoint."""
    mtb = ss.make_minimal_envelope(
        scope_id="rel_mtb", name="Adam",
        participant_id="16072348189@s.whatsapp.net", event_id="mtb",
    )
    mtb["context_layers"]["injected"] = [
        {"fragment_id": "evt_mtb", "content": "# MTB\nDates: April 24-26\nLocation: Crested Butte"},
    ]
    ss.create_scope(mtb, db)

    bday = ss.make_minimal_envelope(
        scope_id="rel_bday", name="Sam",
        participant_id="14125550004@s.whatsapp.net", event_id="bday",
    )
    bday["context_layers"]["injected"] = [
        {"fragment_id": "evt_bday", "content": "# Birthday\nDates: May 2\nLocation: Fun Spot"},
    ]
    ss.create_scope(bday, db)


def test_adam_sees_only_mtb_scope(isolated_db, loop_with_provider):
    _seed_two_scopes(isolated_db)
    # Wire-form sender_id (phone digits only — what WhatsApp bridge delivers)
    out = loop_with_provider._get_scope_context("16072348189")
    assert out is not None
    assert "## Scope: rel_mtb" in out
    assert "Crested Butte" in out
    # Cross-scope content must not leak
    assert "rel_bday" not in out
    assert "Fun Spot" not in out
    assert "Sam" not in out


def test_seun_sees_only_bday_scope(isolated_db, loop_with_provider):
    _seed_two_scopes(isolated_db)
    out = loop_with_provider._get_scope_context("14125550004")
    assert out is not None
    assert "## Scope: rel_bday" in out
    assert "Fun Spot" in out
    assert "rel_mtb" not in out
    assert "Crested Butte" not in out
    assert "Adam" not in out


def test_unknown_sender_gets_no_injection(isolated_db, loop_with_provider):
    _seed_two_scopes(isolated_db)
    # A phone that isn't a participant in any scope
    out = loop_with_provider._get_scope_context("19999999999")
    # Empty scope context collapses to None per nanobot contract
    assert out is None


def test_email_sender_routes_to_scope(isolated_db, loop_with_provider):
    """Interaction scope with email participant — email senders must resolve."""
    env = ss.make_interaction_envelope(
        scope_id="int_vendor", name="Vendor",
        participant_id="vendor@example.com", channel="email",
        purpose="Get quote",
    )
    ss.create_scope(env, isolated_db)

    out = loop_with_provider._get_scope_context("vendor@example.com")
    assert out is not None
    assert "## Scope: int_vendor" in out
    assert "Get quote" in out


def test_full_jid_form_also_works(isolated_db, loop_with_provider):
    """Defensive: if the bridge ever delivers full-JID sender_id, resolution still works."""
    _seed_two_scopes(isolated_db)
    out = loop_with_provider._get_scope_context("16072348189@s.whatsapp.net")
    assert out is not None
    assert "## Scope: rel_mtb" in out
    assert "rel_bday" not in out


def test_provider_lazy_loads_once(isolated_db, loop_with_provider):
    """Module import happens on first call, function reference cached."""
    _seed_two_scopes(isolated_db)
    assert loop_with_provider._scope_context_fn is None
    loop_with_provider._get_scope_context("16072348189")
    assert loop_with_provider._scope_context_fn is ss.render_scope_context_for_sender
    loop_with_provider._get_scope_context("14125550004")
    assert loop_with_provider._scope_context_fn is ss.render_scope_context_for_sender


def test_multi_scope_participant_sees_all_their_scopes(isolated_db, loop_with_provider):
    """A sender who's in multiple scopes sees them all, still isolated from others."""
    _seed_two_scopes(isolated_db)
    # Add Adam to the bday scope as well
    bday = ss.get_scope("rel_bday", isolated_db)
    bday["participants"].append({
        "party_id": "16072348189@s.whatsapp.net", "name": "Adam",
        "handle": "16072348189@s.whatsapp.net",
        "relationship_type": "personal", "channel": "whatsapp",
    })
    ss.update_scope("rel_bday", bday, isolated_db)

    # Create a THIRD scope Adam is NOT in, to prove isolation still holds
    other = ss.make_minimal_envelope(
        scope_id="rel_other", name="Someone",
        participant_id="15559998888@s.whatsapp.net", event_id="other",
    )
    other["context_layers"]["injected"] = [
        {"fragment_id": "evt_other", "content": "# Other Event\nSecret details"},
    ]
    ss.create_scope(other, isolated_db)

    out = loop_with_provider._get_scope_context("16072348189")
    assert "## Scope: rel_mtb" in out
    assert "## Scope: rel_bday" in out
    assert "## Scope: rel_other" not in out
    assert "Secret details" not in out

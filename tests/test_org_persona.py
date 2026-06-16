"""Tests for org-mode persona selection in build_context.

Org tenants ship a coordinator SOUL (agent/SOUL_ORG.md) that's selected only
when HOMER_ORG_MODE is on; templates with no _ORG variant fall back to the
household original.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.build_context as bc

AGENT_DIR = Path(bc.AGENT_DIR)


def test_org_variant_off_by_default(monkeypatch):
    monkeypatch.delenv("HOMER_ORG_MODE", raising=False)
    assert bc._org_template_name("SOUL.md") == "SOUL.md"


def test_org_variant_selected_when_on(monkeypatch):
    monkeypatch.setenv("HOMER_ORG_MODE", "1")
    assert bc._org_template_name("SOUL.md") == "SOUL_ORG.md"


def test_org_variant_falls_back_when_no_variant(monkeypatch):
    """AGENTS.md has no _ORG counterpart → keep the household original."""
    monkeypatch.setenv("HOMER_ORG_MODE", "1")
    assert not (AGENT_DIR / "AGENTS_ORG.md").exists()  # guard the premise
    assert bc._org_template_name("AGENTS.md") == "AGENTS.md"


def test_soul_org_ships_and_is_team_scoped():
    text = (AGENT_DIR / "SOUL_ORG.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "team" in lowered
    # The load-bearing promise: scope boundary between teams.
    assert "boundary" in lowered or "scope" in lowered
    # Must keep the PRIMARY_USER template var the loader substitutes.
    assert "{PRIMARY_USER}" in text

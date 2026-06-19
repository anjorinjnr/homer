"""Shape tests for cases.jsonl.

These don't run the LLM — they pin the case-set so a bad rebase or
copy-paste error doesn't silently corrupt the eval. The real eval
lives in run_eval.py and is invoked manually; this is the CI-safe
piece that catches obvious mistakes on every commit.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

CASES_PATH = Path(__file__).parent / "cases.jsonl"


def _load_raw_lines() -> list[str]:
    return [
        ln for ln in CASES_PATH.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return [json.loads(ln) for ln in _load_raw_lines()]


def test_jsonl_parses(cases):
    """Every non-blank, non-comment line is valid JSON.

    Pinned eagerly because a corrupt line silently truncates the eval
    case set and the resulting accuracy numbers are quietly wrong.
    """
    assert len(cases) > 0, "cases.jsonl must contain at least one case"


def test_required_fields_present(cases):
    """Every case has the fields the harness consumes."""
    required = {"id", "category", "task", "response", "expected"}
    for c in cases:
        missing = required - set(c.keys())
        assert not missing, f"case {c.get('id', '<no id>')} missing: {missing}"


def test_ids_unique(cases):
    """Duplicate IDs would silently let one case shadow another in
    per-case error reports — catch it on commit.
    """
    counts = Counter(c["id"] for c in cases)
    dupes = {k: v for k, v in counts.items() if v > 1}
    assert not dupes, f"duplicate case ids: {dupes}"


def test_expected_is_bool(cases):
    """`expected` is a bool (True=notify, False=suppress). A stray "true"
    string would still pass json.loads and confuse the harness's
    confusion-matrix math.
    """
    for c in cases:
        assert isinstance(c["expected"], bool), \
            f"case {c['id']}: expected must be bool, got {type(c['expected']).__name__}"


def test_category_is_valid(cases):
    """Only the three buckets the harness knows how to summarize."""
    allowed = {"should_suppress", "should_notify", "edge_case"}
    for c in cases:
        assert c["category"] in allowed, \
            f"case {c['id']}: unknown category {c['category']!r}"


def test_categories_have_balanced_coverage(cases):
    """At least 5 cases in each of the two main buckets, and at least
    3 edge cases. A one-sided eval (e.g. 20 suppress, 2 notify) is
    technically valid but gives a misleading accuracy number — TN
    cases pile up while FN risk goes undetected.
    """
    counts = Counter(c["category"] for c in cases)
    assert counts["should_suppress"] >= 5, f"too few should_suppress: {counts['should_suppress']}"
    assert counts["should_notify"] >= 5, f"too few should_notify: {counts['should_notify']}"
    assert counts["edge_case"] >= 3, f"too few edge_case: {counts['edge_case']}"


def test_response_non_trivial(cases):
    """Every response is either deliberately empty (edge cases) or
    long enough to be a real signal. Catches typos / truncations.
    """
    for c in cases:
        if c["category"] == "edge_case":
            continue  # empty / one-word edge cases are valid
        assert len(c["response"]) >= 20, \
            f"case {c['id']}: response too short for a realistic example ({len(c['response'])} chars)"


def test_no_pii_keywords(cases):
    """Quick guard against the OSS-public PII rule. Not exhaustive —
    just blocks the names that have leaked in past commits per
    project memory.
    """
    forbidden = ("seun", "tola", "kemi", "ebenezer", "anjorin")
    for c in cases:
        blob = f"{c['task']} {c['response']} {c.get('note', '')}".lower()
        for needle in forbidden:
            assert needle not in blob, \
                f"case {c['id']}: contains forbidden PII keyword {needle!r}"


def test_variants_baseline_in_sync_with_template():
    """The harness's `BASELINE` prompt must match the live
    `evaluator.md` template content exactly — if it drifts, every
    accuracy number on the baseline column is measuring the wrong
    thing.

    Located by walking up to the repo root and looking for nanobot's
    installed copy in `.venv/`. If the template can't be found (CI
    without the venv set up), skip with a clear message rather than
    fail — the human-readable mismatch is more useful than a noisy
    CI red.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from variants import BASELINE  # noqa: E402

    repo_root = Path(__file__).resolve().parents[3]
    # Search both the installed site-packages copy (production shape)
    # and any sibling nanobot checkout (dev shape).
    candidates = [
        *repo_root.glob(".venv/lib/python*/site-packages/nanobot/templates/agent/evaluator.md"),
        repo_root.parent / "nanobot" / "nanobot" / "templates" / "agent" / "evaluator.md",
    ]
    template_path = next((p for p in candidates if p.exists()), None)
    if template_path is None:
        pytest.skip("evaluator.md template not on disk (no venv install, no sibling nanobot checkout)")

    raw = template_path.read_text()
    # The template is jinja with {% if part == 'system' %} blocks.
    # Extract the system part between the two tags.
    sys_start = raw.find("{% if part == 'system' %}") + len("{% if part == 'system' %}")
    sys_end = raw.find("{% elif")
    template_system = raw[sys_start:sys_end].strip()

    assert template_system == BASELINE.strip(), (
        "BASELINE in variants.py has drifted from the live evaluator.md. "
        "Refresh BASELINE so the eval's baseline numbers reflect production."
    )

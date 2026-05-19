"""Drift guards: presets.py is the source of truth; everything else mirrors it.

When the canonical slate in `tools/presets.py` changes, three mirrors must
move with it or the runtime resolution lies:

- `agent/AGENTS.md` — the prose table the agent reads at runtime to decide
  which preset matches a user's "switch to X" request.
- `config/guest_config.json.template` — heartbeat task `Model:` resolution
  for non-hosted homer.
- `config/guest_config.hosted.json.template` — same, for hosted tenants.

If any of these drift, the test fails with a specific diff so it's clear
what to update. Don't paper over the failure — propagate the change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Importing `presets` directly mirrors how tools/* import it (sys.path
# includes tools/).
import sys
sys.path.insert(0, str(REPO_ROOT / "tools"))
from presets import PRESETS, model_presets_map  # noqa: E402


# Anchored at the start of a list item, captures the label backtick + the
# first whitespace-delimited token after the arrow. The `auto` row has a
# trailing parenthetical that we ignore on purpose.
_AGENTS_MD_ROW = re.compile(r"^- `([a-z0-9-]+)`\s*→\s*(\S+)", re.MULTILINE)


def _agents_md_table() -> dict[str, str]:
    """Parse the preset → model id rows out of AGENTS.md.

    Picks up only the rows in the form "- `<label>` → <model>". The
    `auto` row has a trailing parenthetical, so we match `\\S+` and treat
    anything after the first whitespace as commentary.
    """
    text = (REPO_ROOT / "agent" / "AGENTS.md").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for label, model in _AGENTS_MD_ROW.findall(text):
        if label in PRESETS:
            out[label] = model
    return out


def _template_model_presets(name: str) -> dict[str, str]:
    """Parse the modelPresets dict out of a guest_config template.

    The templates aren't valid JSON (they contain `${VAR}` placeholders),
    so we substring the modelPresets object and parse just that. The
    object's contents are pure string literals, so the slice is JSON.
    """
    text = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
    match = re.search(r'"modelPresets"\s*:\s*(\{[^}]*\})', text, re.DOTALL)
    assert match, f"modelPresets block missing from {name}"
    return json.loads(match.group(1))


def test_agents_md_table_matches_presets() -> None:
    got = _agents_md_table()
    want = {label: spec["model"] for label, spec in PRESETS.items()}
    # AGENTS.md intentionally omits `default-cheap` (an internal alias not
    # exposed to the agent). Drop it before comparing.
    want.pop("default-cheap", None)
    assert got == want, (
        "AGENTS.md preset table is out of sync with tools/presets.py.\n"
        f"  in AGENTS.md but not presets.py: {set(got) - set(want)}\n"
        f"  in presets.py but not AGENTS.md: {set(want) - set(got)}\n"
        f"  mismatched values: "
        f"{ {k: (got.get(k), want.get(k)) for k in (set(got) & set(want)) if got.get(k) != want.get(k)} }"
    )


@pytest.mark.parametrize("template", [
    "guest_config.json.template",
    "guest_config.hosted.json.template",
])
def test_template_model_presets_match(template: str) -> None:
    got = _template_model_presets(template)
    want = model_presets_map()
    assert got == want, (
        f"{template}'s modelPresets block is out of sync with tools/presets.py.\n"
        f"  in template but not presets.py: {set(got) - set(want)}\n"
        f"  in presets.py but not template: {set(want) - set(got)}\n"
        f"  mismatched values: "
        f"{ {k: (got.get(k), want.get(k)) for k in (set(got) & set(want)) if got.get(k) != want.get(k)} }"
    )

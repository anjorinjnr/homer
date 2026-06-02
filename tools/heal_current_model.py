#!/usr/bin/env python3
"""heal_current_model.py — drop a stale CURRENT_MODEL pin on boot.

``switch_model.py`` writes the active model id into the workspace
``CURRENT_MODEL`` file, and the container entrypoint honors it across
restarts (so a runtime model switch survives). But that pin is write-once:
when the preset slate moves on — a model retired or replaced upstream, e.g.
``deepseek/deepseek-v3.2`` → ``deepseek/deepseek-v4-flash`` — the stale id
just sits there, and every call routes to a model that may no longer exist
(silent 404s / dead default tier).

This tool, run by the entrypoint *before* it reads ``CURRENT_MODEL``, clears
the file when its id is no longer in the canonical preset slate
(``tools/presets.py``). The entrypoint then falls back to
``HOMER_DEFAULT_MODEL`` (the portal-provisioned default tier). A valid pin is
left untouched.

Design constraints:

* **Single source of truth** — the valid set is derived from ``presets.py``,
  never a hardcoded list here.
* **Clear, don't rewrite** — ``switch_model.py`` stays the sole *writer* of a
  model id; this tool only removes an invalid pin.
* **Fail-open** — only acts on a present, non-empty, definitely-stale id; any
  error (slate unimportable, unreadable file) leaves the file as-is and exits
  0, so it can never block container boot.

Usage (entrypoint):
    heal_current_model.py <CURRENT_MODEL_file_path>

Prints a one-line reason to stdout when it clears the file; silent otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Mirror how the other tools import the canonical slate: as ``tools.presets``
# when /opt/homer is on sys.path, or as ``presets`` when the script's own dir
# (tools/) is sys.path[0] (the entrypoint invokes it by absolute path).
try:
    from tools.presets import PRESETS
except ImportError:  # pragma: no cover - depends on invocation style
    from presets import PRESETS  # type: ignore


def valid_model_ids() -> set[str]:
    """The set of model ids the current preset slate can route to."""
    return {spec["model"] for spec in PRESETS.values()}


def heal(path: Path) -> str | None:
    """Clear *path* if it pins a model id not in the preset slate.

    Returns a human-readable reason when it clears the file, else ``None``
    (file absent, empty, still valid, or any I/O error — all no-ops).
    """
    try:
        if not path.is_file():
            return None
        pinned = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not pinned:
        return None
    if pinned in valid_model_ids():
        return None
    try:
        path.unlink()
    except OSError:
        return None
    return (
        f"CURRENT_MODEL '{pinned}' is no longer in the preset slate "
        f"(tools/presets.py) — cleared the pin; falling back to the "
        f"default-tier model (HOMER_DEFAULT_MODEL)."
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        # No path given — nothing to do. Never block boot.
        return 0
    reason = heal(Path(argv[1]))
    if reason:
        print(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

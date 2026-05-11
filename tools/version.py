#!/usr/bin/env python3
"""
version.py — Report Homer's current running versions.

Usage:
    python tools/version.py
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE = REPO_ROOT / "context" / ".nanobot_workspace"
NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"


def _resolve_active_model() -> str:
    """Resolve the active model.

    CURRENT_MODEL is only present when switch_model.py has been run; it is
    NOT auto-stamped on every boot (homer#247). Fall back to the nanobot
    config (source of truth at runtime), then HOMER_DEFAULT_MODEL.
    """
    cm = WORKSPACE / "CURRENT_MODEL"
    if cm.exists():
        val = cm.read_text().strip()
        if val:
            return val
    if NANOBOT_CONFIG_PATH.exists():
        try:
            cfg = json.loads(NANOBOT_CONFIG_PATH.read_text())
            val = cfg.get("agents", {}).get("defaults", {}).get("model", "")
            if val:
                return val
        except Exception:
            pass
    return os.environ.get("HOMER_DEFAULT_MODEL", "unknown")


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT)] + args,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


homer_commit = _git(["rev-parse", "--short", "HEAD"])
homer_date = _git(["log", "-1", "--format=%ci"])

nanobot_commit_file = WORKSPACE / "NANOBOT_FORK_COMMIT"
nanobot_commit = (
    nanobot_commit_file.read_text().strip()
    if nanobot_commit_file.exists()
    else "unknown"
)

active_model = _resolve_active_model()

try:
    import nanobot
    nanobot_version = getattr(nanobot, "__version__", "unknown")
except ImportError:
    nanobot_version = "not installed"

print(json.dumps({
    "homer_commit": homer_commit,
    "homer_commit_date": homer_date,
    "nanobot_fork_commit": nanobot_commit,
    "nanobot_version": nanobot_version,
    "active_model": active_model,
}, indent=2))

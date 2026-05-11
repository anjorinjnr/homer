"""Per-sender scope gating for guest-invocable tools.

Guest tools (event_manage.py, escalate.py, deliver_escalation.py) read
``current_sender_scopes.json`` — written per-turn by
``scope_store.render_scope_context_for_sender`` — to refuse operations on
scopes the current sender isn't a participant in. **No fallback** to the
global ``active_scopes.json``: an unscoped sender (or a heartbeat tick with
no resolved sender) gets an empty list and is refused. The earlier fallback
defeated the gate on prod — Adam (LID 38457841848414, no scope) was able
to read kemi_5th_bday because the global list contained every active scope.

Gate is triggered by ``HOMER_GUEST_WORKSPACE``; main-agent invocations leave
it unset and are not gated.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()

_HOMER_TOOLS = str(REPO_ROOT / "tools")
if _HOMER_TOOLS not in sys.path:
    sys.path.insert(0, _HOMER_TOOLS)

import scope_store  # noqa: E402


def _guest_workspace() -> Path:
    ws = os.environ.get("HOMER_GUEST_WORKSPACE")
    if ws:
        return Path(ws)
    return REPO_ROOT / "context" / ".guest_workspace"


def is_guest_mode() -> bool:
    """True only when the current process is the guest agent's subprocess.

    Checking ``HOMER_GUEST_WORKSPACE`` alone was too loose: the main nanobot
    config forwards that var through ``allowedEnvKeys`` so main-agent tool
    subprocesses see it too, and used to get falsely gated as guests
    (event_manage refused main-agent updates).

    The reliable signal is ``HOMER_WORKSPACE == HOMER_GUEST_WORKSPACE`` —
    only the guest nanobot launches with its HOMER_WORKSPACE overridden to
    the guest workspace dir. Main has HOMER_WORKSPACE pointing at the
    nanobot workspace, so the two differ.
    """
    guest_ws = os.environ.get("HOMER_GUEST_WORKSPACE")
    workspace = os.environ.get("HOMER_WORKSPACE")
    if not guest_ws or not workspace:
        return False
    try:
        return Path(guest_ws).resolve() == Path(workspace).resolve()
    except (OSError, ValueError):
        return False


def _read_ids(path: Path) -> Optional[list[str]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        return [s for s in data if isinstance(s, str)]
    return None


def sender_scope_ids() -> list[str]:
    """Return scope IDs the current sender is a participant in.

    Reads ``current_sender_scopes.json``, written per-turn by
    ``scope_store.render_scope_context_for_sender``. No fallback: a missing
    or unreadable file means we have no sender context, and callers
    should fail-closed.
    """
    ws = _guest_workspace()
    ids = _read_ids(ws / "current_sender_scopes.json")
    return ids if ids is not None else []


def _fail(error: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
    sys.exit(code)


def assert_scope_allowed(scope_id: str) -> None:
    """Exit with error JSON if scope_id is not one of the sender's active scopes.

    No-op outside guest mode (main agent has full access by design).
    """
    if not is_guest_mode():
        return
    allowed = sender_scope_ids()
    if not allowed:
        _fail("No active scopes for current sender; refusing scope operation.")
    if scope_id not in allowed:
        _fail(
            f"Scope '{scope_id}' is not one of the active scopes for this sender. "
            f"Allowed: {', '.join(allowed)}"
        )


def assert_event_allowed(event_id: str) -> None:
    """Exit with error JSON if event_id does not match any of the sender's scopes.

    A scope ``matches`` event_id when:
        env["context_source"]["ref"] == event_id, OR
        any env["task_tags"][*]["task_id"] == f"task_{event_id}"

    No-op outside guest mode.
    """
    if not is_guest_mode():
        return
    allowed_scope_ids = set(sender_scope_ids())
    if not allowed_scope_ids:
        _fail("No active scopes for current sender; refusing event operation.")
    for env in scope_store.list_active_scopes():
        if env.get("scope_id") not in allowed_scope_ids:
            continue
        ref = env.get("context_source", {}).get("ref", "")
        if ref == event_id:
            return
        for t in env.get("task_tags", []):
            if t.get("task_id") == f"task_{event_id}":
                return
    _fail(f"Event '{event_id}' is not reachable from the current sender's scopes.")

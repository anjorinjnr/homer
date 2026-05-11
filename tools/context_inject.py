#!/usr/bin/env python3
"""
context_inject.py — Populate scope envelopes with context from living documents.

Scopes with a `context_source` field get their `context_layers.injected` refreshed
from the referenced source on every build_context.py run. Scopes without
`context_source` keep their static injected content untouched.

Currently supports one provider: "event" (reads and scrubs status.md).
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
EVENTS_DIR = REPO_ROOT / "context" / "events"
HOMER_TOOLS = str(REPO_ROOT / "tools")

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, callable] = {}


def provider(name: str):
    """Decorator to register a context provider."""
    def decorator(fn):
        PROVIDERS[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Event provider
# ---------------------------------------------------------------------------

@provider("event")
def _provide_event_context(envelope: dict) -> list[dict]:
    """Read and scrub an event's status.md for guest consumption."""
    event_id = _resolve_event_id(envelope)
    if not event_id:
        return []

    status_path = EVENTS_DIR / event_id / "status.md"
    if not status_path.exists():
        return []

    raw = status_path.read_text(encoding="utf-8")
    scrubbed = _scrub_event_status(raw)
    if not scrubbed.strip():
        return []

    return [{"fragment_id": f"evt_{event_id}_status", "content": scrubbed}]


def _resolve_event_id(envelope: dict) -> str:
    """Extract event_id from context_source or fall back to task_tags."""
    src = envelope.get("context_source", {})
    ref = src.get("ref", "")
    if ref:
        return ref
    # Fallback: derive from first task_tag
    for tag in envelope.get("task_tags", []):
        tid = tag.get("task_id", "")
        if tid.startswith("task_"):
            return tid.removeprefix("task_")
    return ""


def _scrub_event_status(raw: str) -> str:
    """Filter event status.md to guest-safe content.

    Keeps everything except Activity Log (internal operational noise).
    """
    lines = raw.split("\n")
    output: list[str] = []
    skip_section = False

    for line in lines:
        if line.startswith("## "):
            section_name = line.lstrip("# ").strip().lower()
            skip_section = section_name == "activity log"
            if not skip_section:
                output.append(line)
            continue

        if skip_section:
            continue

        output.append(line)

    while output and not output[-1].strip():
        output.pop()

    return "\n".join(output) + "\n"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def inject_all(db_path: Optional[Path] = None) -> int:
    """Refresh injected context for all active scopes with a context_source.

    Scopes without context_source are skipped (static context preserved).
    Only writes to DB if content actually changed.
    Returns count of scopes updated.
    """
    sys.path.insert(0, HOMER_TOOLS)
    import scope_store

    scopes = scope_store.list_active_scopes(db_path)
    updated = 0

    for envelope in scopes:
        src = envelope.get("context_source")
        if not src:
            continue

        provider_type = src.get("type", "")
        provider_fn = PROVIDERS.get(provider_type)
        if not provider_fn:
            continue

        new_fragments = provider_fn(envelope)

        # Compare content hashes to avoid unnecessary DB writes
        old_fragments = envelope.get("context_layers", {}).get("injected", [])
        if _fragments_hash(new_fragments) == _fragments_hash(old_fragments):
            continue

        envelope.setdefault("context_layers", {})["injected"] = new_fragments
        scope_store.update_scope(envelope["scope_id"], envelope, db_path)
        updated += 1

    return updated


def _fragments_hash(fragments: list[dict]) -> str:
    """Stable hash of fragment contents for change detection."""
    content = "".join(f.get("content", "") for f in fragments)
    return hashlib.md5(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    count = inject_all()
    print(json.dumps({"scopes_updated": count}))

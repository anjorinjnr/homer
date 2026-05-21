#!/usr/bin/env python3
"""build_identity_map.py — Emit a nanobot-readable identity map.

Reads `context/users.yaml` (+ optionally `lid_map.json`) and writes
`identity_map.json` — a flat `"channel:identifier"` → `"person:<slug>"`
mapping that the nanobot analytics hook uses to collapse channel-scoped
distinct_ids back to a single canonical person
(see nanobot/analytics/identity.py).

Called from:
  - tools/build_context.py (on every workspace build)
  - tools/manage_users.py (after add/update/remove, so the map
    tracks live changes without waiting for the next build)

The map is consumed by nanobot via the HOMER_IDENTITY_MAP env var.

WhatsApp complication: a human's JID can appear as either a raw phone
(e.g. `15551234567`) or an anonymized LID (`246157477413033@lid`)
depending on which form WhatsApp's server uses for that conversation.
users.yaml records one form; we use `lid_map.json` (written by the
WhatsApp channel as it observes inbound MessageSource pairs) to emit
BOTH forms so the lookup hits regardless of which the runtime sees.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent.resolve()
CONTEXT_DIR = Path(os.environ.get("HOMER_CONTEXT_DIR") or (REPO_ROOT / "context"))
WORKSPACE_DIR = CONTEXT_DIR / ".nanobot_workspace"
USERS_YAML = CONTEXT_DIR / "users.yaml"
DEFAULT_OUTPUT = WORKSPACE_DIR / "identity_map.json"


def _default_lid_map_path() -> Path:
    """Resolve the lid_map.json path, preferring the persistent data dir.

    Mirrors nanobot's `get_persistent_data_dir()` semantics: when
    `NANOBOT_PERSISTENT_DATA_DIR` is set (hosted Docker layout), the
    WhatsApp channel writes lid_map there so it survives container
    recreation. Falls back to `~/.nanobot/lid_map.json` for bare-metal
    deployments where `~/.nanobot/` itself is persistent.
    """
    persistent = os.environ.get("NANOBOT_PERSISTENT_DATA_DIR", "").strip()
    if persistent:
        return Path(persistent).expanduser() / "lid_map.json"
    return Path.home() / ".nanobot" / "lid_map.json"


DEFAULT_LID_MAP = _default_lid_map_path()

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_LID_SUFFIX = "@lid"


def _slugify(name: str) -> str:
    """Lowercase, non-alphanum → underscore, trimmed. 'Alex Johnson' → 'alex_johnson'."""
    return _SLUG_RE.sub("_", name.lower()).strip("_")


def _load_lid_map(lid_map_path: Path) -> dict[str, str]:
    """Load nanobot's lid_map.json and flatten to `<lid_prefix>` → `<phone>`.

    File shape (written by the WhatsApp channel on inbound MessageSource
    where Sender ≠ SenderAlt):
        {"246157477413033": {"phone": "15551234567"}}
    """
    try:
        raw = json.loads(lid_map_path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    flat: dict[str, str] = {}
    if isinstance(raw, dict):
        for lid_prefix, info in raw.items():
            if isinstance(info, dict):
                phone = str(info.get("phone") or "").strip()
                if phone:
                    flat[str(lid_prefix).strip()] = phone
    return flat


def _expand_whatsapp_forms(identifier: str, lid_to_phone: dict[str, str]) -> list[str]:
    """Return every form of a WhatsApp identifier we want in the identity map.

    Given `246157477413033@lid` + lid_map {246157477413033: 15551234567}, we
    emit `246157477413033@lid`, `246157477413033`, and `15551234567`. Given
    a raw phone, we reverse the lid_map to also emit the LID forms.

    Dedup is the caller's problem (dict key uniqueness handles it).
    """
    ident = identifier.strip()
    forms = {ident}

    if ident.endswith(_LID_SUFFIX):
        bare_lid = ident[: -len(_LID_SUFFIX)]
        forms.add(bare_lid)
        phone = lid_to_phone.get(bare_lid)
        if phone:
            forms.add(phone)
    elif ident.isdigit():
        # Raw phone. Reverse-lookup the lid_map.
        for lid_prefix, phone in lid_to_phone.items():
            if phone == ident:
                forms.add(f"{lid_prefix}{_LID_SUFFIX}")
                forms.add(lid_prefix)
                break

    return sorted(forms)


def build_map(
    users_yaml_path: Path = USERS_YAML,
    lid_map_path: Path = DEFAULT_LID_MAP,
) -> dict[str, str]:
    """Return the identity map dict derived from users.yaml + lid_map.json.

    Empty-string channel identifiers are skipped (seen when a user is
    listed but hasn't been linked to a channel yet).
    """
    if not users_yaml_path.exists():
        return {}
    try:
        from tools.users_loader import iter_users, load_users
        data = load_users(users_yaml_path)
    except (ValueError, yaml.YAMLError):
        return {}
    lid_to_phone = _load_lid_map(lid_map_path)

    mapping: dict[str, str] = {}
    for _symbol, record in iter_users(data):
        name = (record.get("display_name") or "").strip()
        if not name:
            continue
        # Analytics keys slugify display_name (not symbol) so that renaming a
        # user breaks historical continuity — that's deliberate; the slug
        # tracks who they are now.
        person_key = f"person:{_slugify(name)}"
        channels = record.get("channels") or {}
        if not isinstance(channels, dict):
            continue
        for channel, identifier in channels.items():
            ident = str(identifier or "").strip()
            if not ident:
                continue
            if channel == "whatsapp":
                for form in _expand_whatsapp_forms(ident, lid_to_phone):
                    mapping[f"whatsapp:{form}".lower()] = person_key
            else:
                mapping[f"{channel}:{ident}".lower()] = person_key
    return mapping


def write_map(
    output_path: Path = DEFAULT_OUTPUT,
    users_yaml_path: Path = USERS_YAML,
    lid_map_path: Path = DEFAULT_LID_MAP,
) -> tuple[Path, int]:
    """Write the identity map to `output_path` atomically. Returns
    `(output_path, entry_count)` so callers can log the count without
    re-reading the file.

    Empty maps are still written so nanobot sees a file (simpler than
    teaching it about "file missing vs. empty map"). The output dir is
    created if needed.
    """
    mapping = build_map(users_yaml_path, lid_map_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    os.replace(tmp, output_path)
    return output_path, len(mapping)


def main() -> None:
    path, n = write_map()
    print(f"✓ identity_map.json → {path} ({n} channel entries)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
users_loader.py — Canonical reader/writer for context/users.yaml.

The household user registry is the single source of truth for *who* a
household member is. Every other layer (heartbeat dispatch, brief skill,
identity map, outbound scope, analytics) reads identity through this
loader so we have one consistent answer for "who is `primary`?" and "what
WhatsApp handle does Seun deliver on right now?"

See docs/identity-resolution.md for the design.

Schema versions
---------------

v1 (legacy):  list-of-records keyed by display name.
    users:
      - name: "Ebby Anjorin"
        role: admin
        channels: {whatsapp: ..., telegram: ...}
        briefing_style: ...

v2 (current): dict-of-records keyed by stable symbol.
    schema_version: 2
    users:
      primary:
        display_name: "Ebby Anjorin"
        role: admin
        channels: {whatsapp: ..., telegram: ...}
        briefing_style: ...

v1 on disk auto-converts to v2 in memory on read. Any write always emits
v2. The symbol is the stable key (`primary` for admin, lowercased first
name for members) — display_name can change without breaking references
from HEARTBEAT.md, session files, or the brief composer.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

import yaml

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_USERS_FILE = REPO_ROOT / "context" / "users.yaml"

CURRENT_SCHEMA_VERSION = 2
ADMIN_SYMBOL = "primary"

_SYMBOL_OK = re.compile(r"^[a-z][a-z0-9_]*$")
_NON_SYMBOL_CHARS = re.compile(r"[^a-z0-9_]")


def _users_file() -> Path:
    """Path to users.yaml. Overridable via HOMER_USERS_YAML for tests."""
    override = os.environ.get("HOMER_USERS_YAML")
    return Path(override) if override else DEFAULT_USERS_FILE


# ── Schema detection & normalisation ─────────────────────────────────────────

def is_v1(data: dict) -> bool:
    """v1 has `users:` as a list; v2 has it as a dict."""
    return isinstance(data.get("users"), list)


def _slug(text: str) -> str:
    """Lowercased first whitespace-separated token, [a-z0-9_]-only. Empty or
    whitespace-only input → empty. Uses the same character class as
    build_identity_map._slugify but takes only the first token — symbols
    stay short and stable across renames; analytics keys keep the full name."""
    parts = (text or "").strip().split()
    if not parts:
        return ""
    return _NON_SYMBOL_CHARS.sub("", parts[0].lower())


def derive_symbol(
    display_name: str,
    role: str,
    existing_symbols: Iterable[str],
) -> str:
    """Pick a stable symbol for a user.

    admin → 'primary' (unconditional — admin is a position, not a person,
    and HEARTBEAT.md already half-assumes this).

    member → slugified first name; on collision append _2, _3, ...
    """
    existing = set(existing_symbols)
    if role == "admin":
        return ADMIN_SYMBOL
    base = _slug(display_name) or "user"
    if base == ADMIN_SYMBOL:
        # An unfortunate member named "Primary" would otherwise shadow the
        # admin slot. Disambiguate.
        base = "primary_user"
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _v1_to_v2(data: dict) -> dict:
    """Convert a v1-shaped dict (users as a list) to v2 (users as a symbol
    dict). Pure — no I/O. Drops the `name` key, promotes it to
    `display_name`. Stable symbol assignment order: admin first (always
    `primary`), then list order.
    """
    raw_users = data.get("users") or []
    # Filter to dict entries before sorting — hand-edited files occasionally
    # contain stray strings; the legacy build_identity_map tolerated them.
    indexed = [(i, u) for i, u in enumerate(raw_users) if isinstance(u, dict)]
    # Stable sort: admin first, then original order for the rest.
    indexed.sort(key=lambda iu: 0 if iu[1].get("role") == "admin" else 1)

    out: dict[str, dict] = {}
    symbols: set[str] = set()
    for _, user in indexed:
        if not isinstance(user, dict):
            continue
        display_name = (user.get("name") or "").strip()
        if not display_name:
            continue
        role = user.get("role") or "member"
        symbol = derive_symbol(display_name, role, symbols)
        symbols.add(symbol)
        record: dict = {"display_name": display_name, "role": role}
        # Channels: coerce only when shaped like a dict. Hand-edited files
        # occasionally land with `channels: [telegram, whatsapp]` style
        # lists; the old per-tool readers silently skipped those, so the
        # loader preserves that tolerance instead of raising.
        channels = user.get("channels")
        if isinstance(channels, dict) and channels:
            record["channels"] = dict(channels)
        if (style := user.get("briefing_style")):
            record["briefing_style"] = style
        # Preserve any unrecognised keys so we don't silently drop data on
        # round-trip. Users.yaml is hand-editable; never lose user keystrokes.
        for k, v in user.items():
            if k in {"name", "role", "channels", "briefing_style"}:
                continue
            record.setdefault(k, v)
        out[symbol] = record
    return {"schema_version": CURRENT_SCHEMA_VERSION, "users": out}


def normalize(data: dict | None) -> dict:
    """Take any-version on-disk dict and return canonical v2."""
    if not data:
        return {"schema_version": CURRENT_SCHEMA_VERSION, "users": {}}
    if is_v1(data):
        return _v1_to_v2(data)
    # Already v2 (or close to it). Stamp schema_version and ensure users is a dict.
    users = data.get("users")
    if not isinstance(users, dict):
        users = {}
    return {"schema_version": CURRENT_SCHEMA_VERSION, "users": users}


# ── Load / save ──────────────────────────────────────────────────────────────

def load_users(path: Path | None = None) -> dict:
    """Read users.yaml and return canonical v2 dict.

    A missing file returns an empty v2 record (so callers don't need to
    branch on existence). A corrupt file raises — silent fallbacks mask
    regressions; the privileged-auth path in manage_users.py has its own
    fail-closed handler.
    """
    path = path or _users_file()
    if not path.exists():
        return {"schema_version": CURRENT_SCHEMA_VERSION, "users": {}}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"users.yaml is not a mapping at top level: {path}")
    return normalize(raw)


def save_users(data: dict, path: Path | None = None) -> None:
    """Write v2 dict to users.yaml. Always emits v2, regardless of input shape."""
    path = path or _users_file()
    canonical = normalize(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            canonical,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


# ── Lookup helpers ───────────────────────────────────────────────────────────

def iter_users(data: dict) -> Iterator[tuple[str, dict]]:
    """Yield (symbol, record) pairs in deterministic order: admin first, then
    members in insertion order. Insertion order is preserved by PyYAML and
    the in-memory dict from Python 3.7+."""
    users = data.get("users") or {}
    if not isinstance(users, dict):
        return
    items = list(users.items())
    items.sort(key=lambda kv: 0 if (kv[1] or {}).get("role") == "admin" else 1)
    yield from items


def find_by_display_name(data: dict, name: str) -> tuple[str | None, dict | None]:
    """Case-insensitive lookup. Returns (symbol, record) or (None, None)."""
    if not name:
        return None, None
    target = name.lower()
    for symbol, record in iter_users(data):
        if (record.get("display_name") or "").lower() == target:
            return symbol, record
    return None, None


def find_by_channel_handle(
    data: dict, channel: str, handle: str
) -> tuple[str | None, dict | None]:
    """Reverse lookup: which user has `channels[channel] == handle`?

    Comparison is string-equality on str(); callers that need fuzzy matches
    (JID prefix/suffix) implement that themselves.
    """
    if not handle:
        return None, None
    target = str(handle)
    for symbol, record in iter_users(data):
        if str((record.get("channels") or {}).get(channel) or "") == target:
            return symbol, record
    return None, None


def resolve_handle(symbol: str, channel: str, path: Path | None = None) -> str:
    """Return the current handle for ``(symbol, channel)`` from users.yaml.

    Raises KeyError on unknown symbol / channel. Callers that want a
    default should catch and provide one explicitly — no silent fallback
    here, because silent fallbacks hide drift.
    """
    data = load_users(path)
    users = data.get("users") or {}
    record = users.get(symbol)
    if record is None:
        raise KeyError(f"unknown symbol: {symbol!r}")
    channels = record.get("channels") or {}
    if channel not in channels:
        raise KeyError(f"user {symbol!r} has no {channel!r} channel configured")
    handle = channels[channel]
    if handle is None or str(handle) == "":
        raise KeyError(f"user {symbol!r} has empty {channel!r} handle")
    return str(handle)


# ── Backward-compat shim for v1 list-of-records consumers ────────────────────

def as_legacy_list(data: dict) -> list[dict]:
    """Render v2 data as a v1-shape list of records, with `name` as the key
    field. Lets `manage_users.py list` and direct in-process callers (the
    portal's household_user_service) continue to consume the old shape
    while the on-disk format moves to v2."""
    out: list[dict] = []
    for symbol, record in iter_users(data):
        legacy: dict = {
            "name": record.get("display_name") or "",
            "role": record.get("role") or "member",
            "channels": dict(record.get("channels") or {}),
        }
        if (style := record.get("briefing_style")):
            legacy["briefing_style"] = style
        # Round-trip any unrecognised fields back out (matches _v1_to_v2).
        for k, v in record.items():
            if k in {"display_name", "role", "channels", "briefing_style"}:
                continue
            legacy.setdefault(k, v)
        out.append(legacy)
    return out


# ── CLI for ad-hoc inspection ────────────────────────────────────────────────

def _main() -> int:
    import argparse, json
    p = argparse.ArgumentParser(description="Inspect users.yaml (v2-normalized)")
    p.add_argument("--path", default=None, help="users.yaml path (default: %s)" % DEFAULT_USERS_FILE)
    p.add_argument("--format", choices=("v2", "legacy"), default="v2")
    args = p.parse_args()
    data = load_users(Path(args.path) if args.path else None)
    if args.format == "legacy":
        json.dump(as_legacy_list(data), sys.stdout, indent=2)
    else:
        json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

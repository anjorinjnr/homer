#!/usr/bin/env python3
"""
manage_users.py — Manage the household user registry (context/users.yaml).

Used by Homer (via exec) and the portal (via API) to add, update, remove,
and list household users.

Shared logic lives in the pure-Python functions: list_users(), add_user(),
update_user(), remove_user().  The cmd_* functions are thin CLI wrappers.

All I/O and schema concerns delegate to users_loader.py; this module just
mutates the in-memory v2 dict and lets the loader handle persistence. See
docs/identity-resolution.md.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
USERS_FILE = REPO_ROOT / "context" / "users.yaml"
BUILD_CONTEXT = REPO_ROOT / "tools" / "build_context.py"

# Repo root on sys.path so `from tools.X import Y` resolves when this
# file runs as a script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.users_loader import (  # noqa: E402
    ADMIN_SYMBOL,
    as_legacy_list,
    derive_symbol,
    find_by_display_name,
    iter_users,
    load_users,
    save_users,
)

VALID_ROLES = ("admin", "member")


# ── Low-level helpers ────────────────────────────────────────────────────────

def _rebuild_context() -> None:
    """Re-run build_context.py so {PRIMARY_USER} resolves with updated data."""
    # NOTE: subprocess.run inherits the parent env, which includes
    # NANOBOT_SENDER_ID / NANOBOT_SENDER_CHANNEL when invoked from the
    # agent. build_context.py is read-only today so this is harmless, but
    # if a future change adds a privileged-tool call inside it, the child
    # would inherit the caller's admin authority. Sanitize the env there
    # if that happens.
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    result = subprocess.run([python, str(BUILD_CONTEXT)], capture_output=True, text=True)
    if result.returncode != 0:
        print(json.dumps({"warning": "Context rebuild failed", "detail": result.stderr.strip()}),
              file=sys.stderr)


def _apply_optional(container: dict, key: str, value: str | None) -> None:
    """Three-state update: None = skip, "" = clear, anything else = set."""
    if value is None:
        return
    if value == "":
        container.pop(key, None)
    else:
        container[key] = value


def _rebuild_with_symbols(users: dict, *, swap: dict[str, str] | None = None) -> dict:
    """Rebuild the users dict, renaming any keys present in `swap` (old → new).

    Preserves insertion order of the rest. Used by admin transfer where two
    symbols change in the same operation: old admin's symbol moves from
    'primary' to a slug, and the new admin's symbol moves to 'primary'.
    """
    swap = swap or {}
    out: dict = {}
    for sym, rec in users.items():
        new_sym = swap.get(sym, sym)
        out[new_sym] = rec
    return out


# ── Shared pure-Python functions ─────────────────────────────────────────────
# These raise ValueError / KeyError on failure.  No CLI I/O, no sys.exit().

def list_users() -> list[dict]:
    """Return all household users in legacy list-of-records shape.

    Kept backward-compatible until step 6 in docs/identity-resolution.md.
    The portal and any shell script that JSON-parses `manage_users.py list`
    sees `{name, role, channels, briefing_style}` — same as v1.
    """
    return as_legacy_list(load_users(USERS_FILE))


def add_user(
    name: str,
    role: str = "member",
    telegram: str | None = None,
    whatsapp: str | None = None,
    briefing_style: str | None = None,
) -> dict:
    """Add a household user.  Raises ValueError on conflicts."""
    data = load_users(USERS_FILE)
    users: dict = data.setdefault("users", {})

    # Duplicate display_name check (case-insensitive).
    existing_symbol, _ = find_by_display_name(data, name)
    if existing_symbol is not None:
        raise ValueError(f"User '{name}' already exists. Use update to modify.")

    if role == "admin":
        for sym, rec in users.items():
            if rec.get("role") == "admin":
                raise ValueError(
                    f"Admin already exists ({rec.get('display_name')}). "
                    "Remove or change their role first."
                )

    symbol = derive_symbol(name, role, users.keys())
    record: dict = {"display_name": name, "role": role, "channels": {}}
    if telegram:
        record["channels"]["telegram"] = telegram
    if whatsapp:
        record["channels"]["whatsapp"] = whatsapp
    if briefing_style:
        record["briefing_style"] = briefing_style
    if not record["channels"]:
        record.pop("channels")

    users[symbol] = record
    save_users(data, USERS_FILE)
    _rebuild_context()
    _emit_member_event("household_member_added", name, role, len(users))
    # Return the legacy-shape record so callers (portal) see `name`.
    return _legacy_view(symbol, record)


def update_user(
    name: str,
    rename: str | None = None,
    role: str | None = None,
    telegram: str | None = None,
    whatsapp: str | None = None,
    briefing_style: str | None = None,
) -> dict:
    """Update an existing household user.

    Admin transfer: promoting a member to admin automatically demotes the
    current admin to member in the same operation (atomic swap). When this
    happens the affected symbols are also rotated — the new admin takes
    'primary', the demoted admin gets a slug-based symbol. This is the
    whole point of stable symbols: HEARTBEAT.md `primary:whatsapp`
    continues to mean "the current admin" without rewriting any references.

    Raises KeyError if user not found, ValueError on validation failures.
    """
    data = load_users(USERS_FILE)
    users: dict = data.setdefault("users", {})
    symbol, user = find_by_display_name(data, name)
    if user is None:
        raise KeyError(f"User '{name}' not found.")

    # Snapshot what we may need to know before mutating.
    current_role = user.get("role")
    swap: dict[str, str] = {}

    if role is not None:
        if role == "admin" and current_role != "admin":
            # Atomic admin transfer.
            for other_sym, other in list(users.items()):
                if other_sym == symbol:
                    continue
                if other.get("role") == "admin":
                    other["role"] = "member"
                    # Other admin loses 'primary'; give them a slug-based symbol.
                    available = (set(users.keys()) | {symbol, ADMIN_SYMBOL}) - {other_sym}
                    new_other_sym = derive_symbol(
                        other.get("display_name") or "",
                        "member",
                        available,
                    )
                    if new_other_sym != other_sym:
                        swap[other_sym] = new_other_sym
            # Target user becomes admin → symbol moves to 'primary'.
            if symbol != ADMIN_SYMBOL:
                swap[symbol] = ADMIN_SYMBOL
        if role != "admin" and current_role == "admin":
            raise ValueError(
                "Cannot demote the only admin. Promote another user to admin first."
            )
        user["role"] = role

    if rename:
        # display_name rename. Symbol stays put — that's the durable id.
        collision_sym, collision = find_by_display_name(data, rename)
        if collision is not None and collision_sym != symbol:
            raise ValueError(f"User '{rename}' already exists.")
        user["display_name"] = rename

    if "channels" not in user or not isinstance(user.get("channels"), dict):
        user["channels"] = {}
    _apply_optional(user["channels"], "telegram", telegram)
    _apply_optional(user["channels"], "whatsapp", whatsapp)
    if not user["channels"]:
        user.pop("channels", None)
    _apply_optional(user, "briefing_style", briefing_style)

    if swap:
        data["users"] = _rebuild_with_symbols(users, swap=swap)
        # `user` reference still points at the same record dict; the swap
        # only renames keys, not values.
        symbol = swap.get(symbol, symbol)

    save_users(data, USERS_FILE)
    _rebuild_context()
    return _legacy_view(symbol, user)


def remove_user(name: str) -> dict:
    """Remove a household user.  Raises KeyError / ValueError."""
    data = load_users(USERS_FILE)
    users: dict = data.setdefault("users", {})
    symbol, user = find_by_display_name(data, name)
    if user is None:
        raise KeyError(f"User '{name}' not found.")

    if user.get("role") == "admin":
        raise ValueError("Cannot remove the admin user. Change their role first.")

    removed_role = user.get("role", "member")
    users.pop(symbol, None)
    save_users(data, USERS_FILE)
    _rebuild_context()
    _emit_member_event("household_member_removed", name, removed_role, len(users))
    return {"status": "removed", "name": name}


def _legacy_view(symbol: str, record: dict) -> dict:
    """Render a single v2 record as the legacy v1 shape (with `name`)."""
    view: dict = {
        "name": record.get("display_name") or "",
        "role": record.get("role") or "member",
        "channels": dict(record.get("channels") or {}),
    }
    if (style := record.get("briefing_style")):
        view["briefing_style"] = style
    return view


def _emit_member_event(
    event: str, name: str, role: str, member_count_after: int,
) -> None:
    """Fire PostHog household_member_added/removed. Fire-and-forget — never
    raises; analytics failures must not block a successful add/remove."""
    try:
        from tools.analytics.events import (
            track_household_member_added,
            track_household_member_removed,
        )
        from tools.analytics.identity import get_person_distinct_id
        distinct_id = get_person_distinct_id(name)
        if event == "household_member_added":
            track_household_member_added(
                distinct_id, member_count_after=member_count_after, role=role,
            )
        elif event == "household_member_removed":
            track_household_member_removed(
                distinct_id, member_count_after=member_count_after, role=role,
            )
    except Exception:
        pass


# ── Runtime requester check (CLI mode only) ─────────────────────────────────
# nanobot stamps NANOBOT_SENDER_ID + NANOBOT_SENDER_CHANNEL onto every exec
# subprocess from runtime state — the LLM cannot influence them. Privileged
# CLI commands resolve the requester from those env vars and refuse unless
# they map to an admin user. The portal calls add_user/update_user/remove_user
# directly in-process and has its own Supabase auth, so this gate sits only
# on the CLI entry points.
#
# Channel-format contract: NANOBOT_SENDER_CHANNEL must exactly match a key in
# users.yaml's `channels:` dict (lowercase, no suffixes — e.g. "telegram" or
# "whatsapp", not "Telegram" or "14155551234@s.whatsapp.net"). Drift on
# either side silently breaks auth for legitimate admins.


def _resolve_requester() -> dict | None:
    sender_id = os.environ.get("NANOBOT_SENDER_ID")
    channel = os.environ.get("NANOBOT_SENDER_CHANNEL")
    if not sender_id or not channel:
        return None
    try:
        data = load_users(USERS_FILE)
    except Exception:
        # Corrupt or unreadable users.yaml → fail closed.
        return None
    for _, record in iter_users(data):
        # str() coerce — channel IDs are stored as strings today, but a
        # hand-edit or future schema change could land an int. Env vars are
        # always strings, so normalise both sides.
        if str((record.get("channels") or {}).get(channel)) == sender_id:
            return record
    return None


def _require_admin_requester() -> None:
    """Refuse with a generic error unless the runtime-injected sender maps to
    an admin. Generic message avoids leaking whether the requester was
    unknown vs. known-but-not-admin."""
    user = _resolve_requester()
    if not user or user.get("role") != "admin":
        print(json.dumps({"error": "Not authorized."}))
        sys.exit(1)


# ── CLI wrappers ─────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> None:
    print(json.dumps(list_users(), indent=2))


def cmd_add(args: argparse.Namespace) -> None:
    _require_admin_requester()
    try:
        new_user = add_user(
            name=args.name,
            role=args.role,
            telegram=args.telegram,
            whatsapp=args.whatsapp,
            briefing_style=args.briefing_style,
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    print(json.dumps({"status": "added", "user": new_user}))


def cmd_update(args: argparse.Namespace) -> None:
    _require_admin_requester()
    try:
        user = update_user(
            name=args.name,
            rename=args.rename,
            role=args.role,
            telegram=args.telegram,
            whatsapp=args.whatsapp,
            briefing_style=args.briefing_style,
        )
    except (KeyError, ValueError) as e:
        print(json.dumps({"error": e.args[0]}))
        sys.exit(1)
    print(json.dumps({"status": "updated", "user": user}))


def cmd_remove(args: argparse.Namespace) -> None:
    _require_admin_requester()
    try:
        result = remove_user(args.name)
    except (KeyError, ValueError) as e:
        print(json.dumps({"error": e.args[0]}))
        sys.exit(1)
    print(json.dumps({"status": "removed", "name": result["name"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage household user registry")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all users")

    add_p = sub.add_parser("add", help="Add a user")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--role", choices=VALID_ROLES, default="member")
    add_p.add_argument("--telegram", default=None)
    add_p.add_argument("--whatsapp", default=None)
    add_p.add_argument("--briefing-style", dest="briefing_style", default=None,
                       help="Free-form morning briefing style hint (e.g. 'dry, no emoji')")

    upd_p = sub.add_parser("update", help="Update a user")
    upd_p.add_argument("--name", required=True, help="Current name (lookup key)")
    upd_p.add_argument("--rename", default=None, help="New name")
    upd_p.add_argument("--role", choices=VALID_ROLES, default=None)
    upd_p.add_argument("--telegram", default=None)
    upd_p.add_argument("--whatsapp", default=None)
    upd_p.add_argument("--briefing-style", dest="briefing_style", default=None,
                       help="Free-form briefing style hint. Pass '' to clear.")

    rm_p = sub.add_parser("remove", help="Remove a user")
    rm_p.add_argument("--name", required=True)

    args = parser.parse_args()
    {"list": cmd_list, "add": cmd_add, "update": cmd_update, "remove": cmd_remove}[args.command](args)


if __name__ == "__main__":
    main()

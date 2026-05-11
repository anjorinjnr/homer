#!/usr/bin/env python3
"""
manage_users.py — Manage the household user registry (context/users.yaml).

Used by Homer (via exec) and the portal (via API) to add, update, remove,
and list household users.

Shared logic lives in the pure-Python functions: list_users(), add_user(),
update_user(), remove_user().  The cmd_* functions are thin CLI wrappers.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent.resolve()
USERS_FILE = REPO_ROOT / "context" / "users.yaml"
BUILD_CONTEXT = REPO_ROOT / "tools" / "build_context.py"

# Repo root on sys.path so `from tools.X import Y` resolves when this
# file runs as a script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VALID_ROLES = ("admin", "member")
VALID_CHANNELS = ("telegram", "whatsapp")


# ── Low-level helpers ────────────────────────────────────────────────────────

def _load() -> dict:
    if not USERS_FILE.exists():
        return {"users": []}
    with open(USERS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not data.get("users"):
        data["users"] = []
    return data


def _save(data: dict) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


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


def _find_user(data: dict, name: str) -> tuple[int, dict | None]:
    for i, user in enumerate(data["users"]):
        if user.get("name", "").lower() == name.lower():
            return i, user
    return -1, None


def _apply_optional(container: dict, key: str, value: str | None) -> None:
    """Three-state update: None = skip, "" = clear, anything else = set."""
    if value is None:
        return
    if value == "":
        container.pop(key, None)
    else:
        container[key] = value


# ── Shared pure-Python functions ─────────────────────────────────────────────
# These raise ValueError / KeyError on failure.  No CLI I/O, no sys.exit().

def list_users() -> list[dict]:
    """Return all household users."""
    data = _load()
    return data.get("users", [])


def add_user(
    name: str,
    role: str = "member",
    telegram: str | None = None,
    whatsapp: str | None = None,
    briefing_style: str | None = None,
) -> dict:
    """Add a household user.  Raises ValueError on conflicts."""
    data = _load()
    _, existing = _find_user(data, name)
    if existing:
        raise ValueError(f"User '{name}' already exists. Use update to modify.")

    if role == "admin":
        for user in data["users"]:
            if user.get("role") == "admin":
                raise ValueError(
                    f"Admin already exists ({user['name']}). "
                    "Remove or change their role first."
                )

    new_user: dict = {"name": name, "role": role, "channels": {}}
    if telegram:
        new_user["channels"]["telegram"] = telegram
    if whatsapp:
        new_user["channels"]["whatsapp"] = whatsapp
    if briefing_style:
        new_user["briefing_style"] = briefing_style

    data["users"].append(new_user)
    _save(data)
    _rebuild_context()
    _emit_member_event("household_member_added", name, role, len(data["users"]))
    return new_user


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
    current admin to member in the same operation (atomic swap).

    Raises KeyError if user not found, ValueError on validation failures.
    """
    data = _load()
    idx, user = _find_user(data, name)
    if user is None:
        raise KeyError(f"User '{name}' not found.")

    if role is not None:
        if role == "admin" and user.get("role") != "admin":
            # Atomic admin transfer: demote the current admin first
            for other in data["users"]:
                if other.get("role") == "admin":
                    other["role"] = "member"
        if role != "admin" and user.get("role") == "admin":
            raise ValueError(
                "Cannot demote the only admin. Promote another user to admin first."
            )
        user["role"] = role

    if rename:
        collision_idx, collision = _find_user(data, rename)
        if collision and collision_idx != idx:
            raise ValueError(f"User '{rename}' already exists.")
        user["name"] = rename

    if not user.get("channels"):
        user["channels"] = {}
    _apply_optional(user["channels"], "telegram", telegram)
    _apply_optional(user["channels"], "whatsapp", whatsapp)
    _apply_optional(user, "briefing_style", briefing_style)

    data["users"][idx] = user
    _save(data)
    _rebuild_context()
    return user


def remove_user(name: str) -> dict:
    """Remove a household user.  Raises KeyError / ValueError."""
    data = _load()
    idx, user = _find_user(data, name)
    if user is None:
        raise KeyError(f"User '{name}' not found.")

    if user.get("role") == "admin":
        raise ValueError("Cannot remove the admin user. Change their role first.")

    removed_role = user.get("role", "member")
    data["users"].pop(idx)
    _save(data)
    _rebuild_context()
    _emit_member_event("household_member_removed", name, removed_role, len(data["users"]))
    return {"status": "removed", "name": name}


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
        users = _load().get("users", [])
    except Exception:
        # Corrupt or unreadable users.yaml → fail closed.
        return None
    for user in users:
        # str() coerce — channel IDs are stored as strings today, but a
        # hand-edit or future schema change could land an int. Env vars are
        # always strings, so normalise both sides.
        if str(user.get("channels", {}).get(channel)) == sender_id:
            return user
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

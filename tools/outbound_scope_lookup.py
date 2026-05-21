"""Outbound scope lookup — homer's plug-in for nanobot's scope_guard.

The nanobot fork calls ``resolve(channel, chat_id)`` for every outbound
message and refuses the send if the recipient is neither a household
member nor covered by an active scope-with-context.

Wired via the ``channels.scope_outbound_lookup`` config field
(``outbound_scope_lookup:resolve``). Set ``HOMER_OUTBOUND_SCOPE_GUARD=1``
to enable; entrypoint flips the config field on/off based on the env var.

Vanilla nanobot is preserved when this module is not installed — that's
handled on the nanobot side (no lookup → allow all).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

import yaml
from loguru import logger

from nanobot.channels.scope_guard import (
    REASON_ACTIVE_SCOPE,
    REASON_HOUSEHOLD_MEMBER,
    REASON_NO_REPLY_SCOPE,
    REASON_NO_SCOPE,
    REASON_SCOPE_NO_CONTEXT,
    ScopeLookupResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = str(REPO_ROOT / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
# `from tools.users_loader` needs REPO_ROOT on path too (sibling-script
# invocation puts only tools/ on path, not the parent).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scope_store as ss  # noqa: E402

USERS_YAML = Path(os.environ.get("HOMER_USERS_YAML", REPO_ROOT / "context" / "users.yaml"))
WHATSAPP_GROUP_SUFFIX = "@g.us"

# Module-level mtime-watched cache. Re-stat on each lookup; the syscall is
# microseconds, and rebuilds happen only when context/users.yaml actually
# changes. No daemon thread, no restart on `manage_users.py add`.
_cached_mtime: float | None = None
_cached_members: dict[str, set[str]] = {}  # channel -> set of canonical ids


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _empty_members() -> dict[str, set[str]]:
    return {"telegram": set(), "whatsapp": set(), "email": set()}


def _load_household_members() -> dict[str, set[str]]:
    """Read users.yaml and bucket canonicalized contact ids by channel.

    Telegram: digit string ("5550000001"). WhatsApp: digit-only phone
    ("14125550001") — both bare and JID forms compared at lookup time.
    Email: lowercased + Gmail-normalized (matches scope_store contract).
    """
    try:
        from tools.users_loader import iter_users, load_users
        # load_users returns an empty v2 record for a missing file, so no
        # pre-existence check needed — the mtime cache in _members_now still
        # short-circuits when nothing has changed.
        data = load_users(USERS_YAML)
    except (yaml.YAMLError, ValueError) as e:
        logger.warning("outbound_scope_lookup: failed to parse {}: {}", USERS_YAML, e)
        return _empty_members()

    members: dict[str, set[str]] = {
        "telegram": set(), "whatsapp": set(), "email": set(),
    }
    for _symbol, record in iter_users(data):
        channels = record.get("channels") or {}
        if not isinstance(channels, dict):
            continue
        if (tg := channels.get("telegram")):
            members["telegram"].add(_digits(str(tg)))
        if (wa := channels.get("whatsapp")):
            members["whatsapp"].add(_digits(str(wa)))
        for key in ("email", "gmail"):
            if (email := channels.get(key)):
                members["email"].add(ss.normalize_email(str(email)))
    return members


def _members_now() -> dict[str, set[str]]:
    """Return household members, reloading users.yaml when its mtime changed.

    A missing users.yaml is treated as "no members yet" with an mtime
    sentinel of ``None`` so the next stat that finds the file triggers a
    rebuild without re-reading on every call.
    """
    global _cached_mtime, _cached_members
    try:
        mtime: float | None = USERS_YAML.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    if mtime != _cached_mtime:
        _cached_members = _load_household_members()
        _cached_mtime = mtime
    return _cached_members


def _is_household_whatsapp(chat_id: str, members: Iterable[str]) -> bool:
    """Match a WA outbound chat_id (JID or bare digits) to digit-only members."""
    digits = _digits(chat_id)
    if not digits:
        return False
    return digits in members


def _is_household_member(channel: str, chat_id: str) -> bool:
    members = _members_now()
    if channel == "telegram":
        return chat_id.removeprefix("tg:") in members["telegram"]
    if channel == "whatsapp":
        return _is_household_whatsapp(chat_id, members["whatsapp"])
    if channel == "email":
        return ss.normalize_email(chat_id) in members["email"]
    return False


def _envelope_has_context(env: dict) -> bool:
    """An envelope has 'context' iff it can answer "why is Homer talking to this person?".

    The original failure case (2026-05-06) was specifically an interaction
    scope without a purpose — an ACL-only entry. Every other scope_type
    (event guest, family-historian, …) carries context implicitly via the
    scope_type itself: a family_history scope's purpose is "capture family
    history from this contributor"; a relationship scope's is "this is a
    guest of event X". Only an interaction scope needs an explicit
    injected purpose to count as in-context.
    """
    if env.get("scope_type") != ss.SCOPE_TYPE_INTERACTION:
        return True
    injected = env.get("context_layers", {}).get("injected", []) or []
    return any((frag.get("content") or "").strip() for frag in injected)


def resolve(channel: str, chat_id: str) -> ScopeLookupResult:
    """Authorize an outbound to ``(channel, chat_id)``.

    Order of checks:
      1. Household member (any role) → bypass.
      2. Active scope-with-context for this participant → allow.
      3. Active no-reply scope → allow (suppress_inbound=True).
      4. Otherwise → refuse with a `manage_interaction --create` remediation.
    """
    if _is_household_member(channel, chat_id):
        return ScopeLookupResult(authorized=True, reason=REASON_HOUSEHOLD_MEMBER)

    scopes = ss._lookup_scopes_for_sender(chat_id)
    if not scopes:
        return ScopeLookupResult(
            authorized=False,
            reason=REASON_NO_SCOPE,
            remediation=_remediation(channel, chat_id),
        )

    has_context: list[str] = []
    no_reply: list[str] = []
    for env in scopes:
        sid = env.get("scope_id", "")
        if env.get("mode") == ss.SCOPE_MODE_NO_REPLY:
            no_reply.append(sid)
            continue
        if _envelope_has_context(env):
            has_context.append(sid)

    if has_context:
        return ScopeLookupResult(
            authorized=True, reason=REASON_ACTIVE_SCOPE, scope_ids=has_context,
        )
    if no_reply:
        return ScopeLookupResult(
            authorized=True, reason=REASON_NO_REPLY_SCOPE,
            scope_ids=no_reply, suppress_inbound=True,
        )
    # A scope exists but has no context (ACL-only) — refuse.
    return ScopeLookupResult(
        authorized=False,
        reason=REASON_SCOPE_NO_CONTEXT,
        scope_ids=[env.get("scope_id", "") for env in scopes],
        remediation=_remediation(channel, chat_id),
    )


def _remediation(channel: str, chat_id: str) -> str:
    """Body-only remediation. OutboundScopeError prepends a 'Send refused...' header,
    so don't repeat it here.
    """
    if channel == "telegram":
        flag = f'--telegram-id "{chat_id.removeprefix("tg:")}"'
    elif channel == "email":
        flag = f'--channel email --email "{chat_id}"'
    elif channel == "whatsapp" and chat_id.endswith(WHATSAPP_GROUP_SUFFIX):
        flag = f'--whatsapp-group "{chat_id}"'
    else:
        flag = f'--phone "{chat_id}"'
    return (
        f'Before sending, create one:\n'
        f'  python tools/manage_interaction.py --create \\\n'
        f'    --name "<recipient>" {flag} \\\n'
        f'    --purpose "<one-line reason for this conversation>"\n'
        f'Then retry the send.'
    )

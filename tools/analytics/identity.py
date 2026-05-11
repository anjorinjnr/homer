"""Identity helpers — distinct_id hashing and household_id resolution.

Mirrors nanobot/analytics/identity.py for hashing rules. If the two drift,
events from nanobot (message_sent, agent_responded, user_onboarded) and
events from homer (household_member_added, guest_added, etc.) will attach
to different person records in PostHog even when they represent the same
human.
"""

from __future__ import annotations

import hashlib
import os
import re


def get_distinct_id(identifier: str, channel: str) -> str:
    """Return a stable SHA-256 hash for a channel identifier.

    Phone numbers, email addresses, and voice caller IDs each hash
    independently because the channel is mixed into the input.
    """
    raw = f"{channel}:{identifier.strip()}".lower().strip()
    return hashlib.sha256(raw.encode()).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_person(name: str) -> str:
    """Lowercase, non-alphanum → underscore. Must match tools/build_identity_map.py."""
    return _SLUG_RE.sub("_", name.lower()).strip("_")


def get_person_distinct_id(name: str) -> str:
    """Return the canonical person distinct_id for a household member.

    Matches the hash nanobot's identity map emits for `person:<slug>` so a
    household_member_added event fired from homer lands on the same person
    record PostHog already has (or will have) for their inbound messages.
    """
    key = f"person:{slugify_person(name)}"
    return hashlib.sha256(key.lower().strip().encode()).hexdigest()


def get_household_id() -> str:
    """Return the household UUID.

    Set during container provisioning by the portal as HOMER_HOUSEHOLD_ID.
    Falls back to empty string (caller should skip household-scoped events).
    """
    return os.environ.get("HOMER_HOUSEHOLD_ID", "")

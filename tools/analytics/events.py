"""Typed helpers for the 6 agent-side PostHog events.

All calls are fire-and-forget — capture() returns immediately.
"""

from __future__ import annotations

from tools.analytics.identity import get_household_id
from tools.analytics.posthog_client import get_client


def _base_props() -> dict:
    hid = get_household_id()
    return {"household_id": hid} if hid else {}


# ── 1. user_onboarded ────────────────────────────────────────────────────────

def track_user_onboarded(
    distinct_id: str,
    *,
    channel: str,
    is_new_household: bool,
    signup_source: str = "friends_launch",
) -> None:
    props = {
        **_base_props(),
        "channel": channel,
        "is_new_household": is_new_household,
        "signup_source": signup_source,
    }
    client = get_client()
    client.capture(distinct_id, "user_onboarded", props)
    # Set person properties on first identify
    client.identify(distinct_id, {
        "household_id": props.get("household_id", ""),
        "channel_first_seen": channel,
        "is_primary": is_new_household,
        "signup_source": signup_source,
    })


# ── 2. message_sent ──────────────────────────────────────────────────────────

def track_message_sent(
    distinct_id: str,
    *,
    channel: str,
    message_length: int,
    has_attachment: bool,
    use_case_tag: str,
    is_followup: bool,
) -> None:
    props = {
        **_base_props(),
        "channel": channel,
        "message_length": message_length,
        "has_attachment": has_attachment,
        "use_case_tag": use_case_tag,
        "is_followup": is_followup,
    }
    client = get_client()
    client.capture(distinct_id, "message_sent", props)
    # Group identify on every message_sent
    hid = get_household_id()
    if hid:
        client.group_identify("household", hid, {})


# ── 3. agent_responded ───────────────────────────────────────────────────────

def track_agent_responded(
    distinct_id: str,
    *,
    channel: str,
    latency_ms: int,
    tool_calls_count: int,
    tools_used: list[str],
    escalation_triggered: bool,
    response_length: int,
) -> None:
    props = {
        **_base_props(),
        "channel": channel,
        "latency_ms": latency_ms,
        "tool_calls_count": tool_calls_count,
        "tools_used": tools_used,
        "escalation_triggered": escalation_triggered,
        "response_length": response_length,
    }
    get_client().capture(distinct_id, "agent_responded", props)


# ── 4. use_case_completed ────────────────────────────────────────────────────

def track_use_case_completed(
    distinct_id: str,
    *,
    use_case_tag: str,
    turns_to_completion: int,
    outcome: str,  # completed | abandoned | error
) -> None:
    props = {
        **_base_props(),
        "use_case_tag": use_case_tag,
        "turns_to_completion": turns_to_completion,
        "outcome": outcome,
    }
    get_client().capture(distinct_id, "use_case_completed", props)


# ── 5. household_member_added / _removed ────────────────────────────────────
# Fired explicitly by tools/manage_users.add_user() / remove_user() — not by
# the nanobot analytics hook inferring from inbound traffic. The nanobot hook
# used to fire household_member_added on "first time we see a new distinct_id
# in an existing household," which also fired for the same human on a new
# channel (id format mismatches). Fire from the actual admin action instead.

def track_household_member_added(
    distinct_id: str,
    *,
    member_count_after: int,
    role: str,
) -> None:
    props = {
        **_base_props(),
        "member_count_after": member_count_after,
        "role": role,
    }
    get_client().capture(distinct_id, "household_member_added", props)


def track_household_member_removed(
    distinct_id: str,
    *,
    member_count_after: int,
    role: str,
) -> None:
    props = {
        **_base_props(),
        "member_count_after": member_count_after,
        "role": role,
    }
    get_client().capture(distinct_id, "household_member_removed", props)


# ── 6. guest_added / _removed ────────────────────────────────────────────────
# Fired when a guest (non-household person) is granted or revoked access to
# a scope (an event / trip / RSVP / etc.). `scope_id` lets dashboards filter
# by which event the guest was on.

def track_guest_added(
    distinct_id: str,
    *,
    scope_id: str,
    channel: str,
    scope_type: str = "event",
) -> None:
    props = {
        **_base_props(),
        "scope_id": scope_id,
        "channel": channel,
        "scope_type": scope_type,
    }
    get_client().capture(distinct_id, "guest_added", props)


def track_guest_removed(
    distinct_id: str,
    *,
    scope_id: str,
    channel: str,
    scope_type: str = "event",
) -> None:
    props = {
        **_base_props(),
        "scope_id": scope_id,
        "channel": channel,
        "scope_type": scope_type,
    }
    get_client().capture(distinct_id, "guest_removed", props)


# ── 7. feedback_submitted ────────────────────────────────────────────────────

def track_feedback_submitted(
    distinct_id: str,
    *,
    sentiment: str,  # positive | negative | neutral
    trigger: str,    # emoji_reaction | keyword | explicit_command
) -> None:
    props = {
        **_base_props(),
        "sentiment": sentiment,
        "trigger": trigger,
    }
    get_client().capture(distinct_id, "feedback_submitted", props)

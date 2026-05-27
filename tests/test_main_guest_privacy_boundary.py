"""Architecture-level regression tests for the main/guest privacy boundary.

Homer runs two nanobot processes per tenant — a privileged ``main`` agent
with full USER.md context loaded, and a scoped ``guest`` agent with
per-scope context only. Both share the same channel account (WhatsApp
multi-device pairing) so every inbound message is delivered to both
processes; each one independently decides whether to accept.

The privacy invariants those two processes maintain:

  1. A guest's inbound — even with an active outbound scope — must NOT
     reach the main agent. Main's static ``allow_from`` is the household
     list; the outbound scope ACL must not silently re-authorize guests
     at the inbound layer.

  2. A household member's inbound must NOT reach the guest agent (the
     guest workspace doesn't have the household's private context, so
     household members talking to the guest is wrong by routing even
     if it would technically work).

  3. The main agent, mid-turn, must NEVER ``message(chat_id=guest_handle)``
     even if it has the guest's chat_id in its memory or hallucinates
     it. The MessageTool recipient gate refuses any send outside the
     turn's scope.

These tests name the invariants so a future PR that violates them fails
loudly rather than silently re-introducing the production incident shape
of 2026-05-27. That incident required two simultaneous breaks: a guest
session existing on main (the upstream collapsed the inbound ACL onto
the outbound scope lookup; reverted in nanobot #108), and heartbeat
dispatch ignoring the ``Recipients:`` line on non-prompt-file tasks
(closed in nanobot #109). This file verifies all three invariants at
the homer↔nanobot interop seam.
"""

from __future__ import annotations

import importlib

import pytest
import yaml

import tools.scope_store as ss


# ── Test placeholders ────────────────────────────────────────────────────────
#
# Homer is OSS-public; no real phone numbers, names, emails, or event
# specifics belong in tests, fixtures, or PRs. Use these generic
# placeholders throughout. The 555-area-code pattern is the conventional
# "unallocated, will never route" set.

_RESIDENT_PHONE = "15550000001"
_GUEST_PHONE = "15550000002"
_RESIDENT_HANDLE = "resident@example.local"
_GUEST_HANDLE = "guest@example.local"


# ── Fixture: realistic two-channel + scope_guard wiring ──────────────────────


@pytest.fixture()
def two_channel_setup(tmp_path, monkeypatch):
    """Provision:
      - A users.yaml with one household member (``resident`` on WhatsApp).
      - A scope_store with an active two-way scope for one guest
        (``guest_a`` on WhatsApp) — mirrors the shape a real guest
        scope would have if they were invited to coordinate something.
      - homer's ``outbound_scope_lookup.resolve`` installed into
        nanobot's ``scope_guard`` as the host lookup.

    Returns a dict with the two BaseChannel-derived test channels:
      - ``main``: ``allow_from=[_RESIDENT_PHONE]`` — household only.
      - ``guest``: ``allow_from=[_GUEST_PHONE]`` — the way
        ``build_context.update_guest_config_allow_from`` would populate
        it from scope-store participant IDs at runtime.
    """
    # Isolated state — every test gets fresh users.yaml + scope DB so
    # module-level caches (mtime, members, schema-initialised) don't bleed.
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text(yaml.safe_dump({
        "schema_version": 2,
        "users": {
            "resident": {
                "display_name": "Resident",
                "role": "primary",
                "channels": {"whatsapp": _RESIDENT_PHONE},
            },
        },
    }))
    db_path = tmp_path / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db_path))
    monkeypatch.setenv("HOMER_USERS_YAML", str(users_yaml))

    # Reset homer's cached lookup module so HOMER_USERS_YAML re-binds.
    for mod_name in ("tools.outbound_scope_lookup", "outbound_scope_lookup"):
        if mod_name in importlib.sys.modules:
            del importlib.sys.modules[mod_name]
    lookup_mod = importlib.import_module("tools.outbound_scope_lookup")
    ss._SCHEMA_INITIALISED.clear()

    # Give the guest an active two-way scope, mirroring the shape a
    # real guest scope takes when someone is invited to coordinate.
    env = ss.make_interaction_envelope(
        scope_id="int_guest_a",
        name="Guest A",
        participant_id=f"{_GUEST_PHONE}@s.whatsapp.net",
        channel="whatsapp",
        purpose="Coordinate event details",
        mode="two_way",
    )
    ss.create_scope(env)

    # Wire homer's resolver into nanobot's scope_guard. Same wiring the
    # entrypoint does in production (when HOMER_OUTBOUND_SCOPE_GUARD=1).
    from nanobot.channels import scope_guard
    scope_guard.set_scope_lookup(lookup_mod.resolve)

    # Build two minimal BaseChannel-derived test channels with the
    # household/guest split mirrored in production.
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.base import BaseChannel
    from nanobot.bus.events import OutboundMessage

    class _TestChannel(BaseChannel):
        """Minimal BaseChannel — we only need `is_allowed`, which lives
        on the base class. Stubs cover the abstract surface so the
        instance constructs cleanly. Both ``main`` and ``guest`` test
        instances share ``name = "whatsapp"`` to mirror production,
        where one WhatsApp account is paired to two devices (one per
        nanobot process)."""
        name = "whatsapp"

        async def start(self): pass
        async def stop(self): pass
        async def send(self, msg: OutboundMessage): pass

    main = _TestChannel({"allow_from": [_RESIDENT_PHONE]}, MessageBus())
    guest = _TestChannel({"allow_from": [_GUEST_PHONE]}, MessageBus())

    yield {
        "main": main,
        "guest": guest,
        "resident_phone": _RESIDENT_PHONE,
        "guest_phone": _GUEST_PHONE,
        "lookup_mod": lookup_mod,
    }

    # Tear down the scope_guard registration so other test files start
    # clean — the lookup is process-global state. Matches the
    # set/unset dance in tests/test_outbound_scope_lookup.py.
    scope_guard.set_scope_lookup(None)


# ── Invariant 1: main rejects scope-authorized guests ────────────────────────


def test_main_rejects_guest_with_active_scope(two_channel_setup):
    """The cross-process privacy boundary.

    The guest has an active two-way scope, so homer's
    ``outbound_scope_lookup.resolve`` would authorize OUTBOUND to them.
    That MUST NOT re-authorize them at INBOUND on the main agent — the
    main agent owns the household's private context (USER.md,
    family-internal events, finance), and any guest reaching it is a
    leak vector even before the LLM speaks. The upstream regression
    that triggered the 2026-05-27 incident routed scope-authorized
    guests to main; nanobot #108 reverted it. This test locks the
    boundary so a future "additive ACL convergence" PR can't slide it
    back in.
    """
    setup = two_channel_setup
    assert setup["main"].is_allowed(setup["guest_phone"]) is False, (
        "main agent accepted a guest's inbound — the outbound scope "
        "ACL is leaking into inbound authorization. Revert any changes "
        "to BaseChannel.is_allowed that consult check_inbound_authorized "
        "or the host scope lookup."
    )


def test_guest_accepts_authorized_guest(two_channel_setup):
    """The complementary positive: the guest agent's dynamically-populated
    ``allow_from`` lets the guest in. (In production, ``build_context.
    update_guest_config_allow_from`` writes scope-derived participant IDs
    into guest_config.json at startup; here we pre-seed the equivalent
    list on the test channel.)"""
    setup = two_channel_setup
    assert setup["guest"].is_allowed(setup["guest_phone"]) is True


def test_main_accepts_household_member(two_channel_setup):
    """Baseline sanity: the household member ALWAYS reaches main."""
    setup = two_channel_setup
    assert setup["main"].is_allowed(setup["resident_phone"]) is True


def test_guest_rejects_household_member(two_channel_setup):
    """The reverse boundary: a household member's inbound must NOT reach
    the guest agent — the guest workspace doesn't have the household's
    private context, and routing household traffic through guest would
    be wrong even if it accidentally "worked." Guest's allow_from has
    only guest participants; the household is not in that list."""
    setup = two_channel_setup
    assert setup["guest"].is_allowed(setup["resident_phone"]) is False


def test_unknown_sender_rejected_by_both(two_channel_setup):
    """Bootstrap path: no scope, not in either allow_from → reject."""
    setup = two_channel_setup
    assert setup["main"].is_allowed("15559999999") is False
    assert setup["guest"].is_allowed("15559999999") is False


# ── Invariant 2: MessageTool recipient gate prevents cross-recipient sends ──


@pytest.mark.asyncio
async def test_message_tool_refuses_guest_chat_id_in_household_turn():
    """The kernel guarantee: an interactive turn from the household member
    pins the MessageTool's allowed_recipients to (whatsapp, resident).
    The agent's tool call ``message(chat_id=guest_handle)`` must be
    refused at the tool layer — the message never reaches the channel
    or the scope guard. Even if the LLM has a guest's chat_id in its
    memory and tries to override, the kernel refuses."""
    from nanobot.agent.tools.message import MessageTool
    from nanobot.bus.events import OutboundMessage

    sent: list[OutboundMessage] = []

    async def _send(msg):
        sent.append(msg)
        if msg._delivery_future and not msg._delivery_future.done():
            msg._delivery_future.set_result(None)

    tool = MessageTool(send_callback=_send)
    # Simulate the agent loop pinning the turn to the resident.
    tool.start_turn(channel="whatsapp", chat_id=_RESIDENT_HANDLE)

    # The LLM hallucinates a guest's chat_id mid-turn.
    result = await tool.execute(
        content="here's some private household info",
        channel="whatsapp",
        chat_id=_GUEST_HANDLE,
    )
    assert "is not permitted" in result
    assert _GUEST_HANDLE in result
    assert sent == [], "MessageTool let a cross-recipient send through — kernel gate broken"


@pytest.mark.asyncio
async def test_heartbeat_dispatch_pin_refuses_off_recipient_send():
    """The heartbeat-dispatch shape of the same guarantee. The dispatcher
    wraps each per-recipient call in ``MessageTool.scoped(
    allowed_recipients=[(channel, handle)])`` so an LLM attempt to
    ``message(chat_id=somebody_else)`` during that dispatch is refused
    — this is precisely the production failure shape: heartbeat
    dispatch passed no target=, the agent guessed a different chat_id,
    the `message` tool happily sent it."""
    from nanobot.agent.tools.message import MessageTool

    tool = MessageTool(send_callback=lambda msg: None)

    # Simulate heartbeat scoping for one task addressed to the resident.
    resident_target = ("whatsapp", _RESIDENT_HANDLE)
    with tool.scoped(allowed_recipients={resident_target}):
        # LLM tries to override with a guest's chat_id.
        result = await tool.execute(
            content="some scheduled-task content",
            channel="whatsapp",
            chat_id=_GUEST_HANDLE,
        )
    assert "is not permitted" in result
    assert _GUEST_HANDLE in result


@pytest.mark.asyncio
async def test_recipient_pin_releases_after_turn():
    """Sanity: an interactive turn's pin is per-turn. After ``start_turn``
    with no args, the pin clears so a separate flow (e.g. a heartbeat
    dispatch on the same MessageTool instance) starts from a clean
    state. Without this property, a stale pin could either accidentally
    block a valid send or accidentally allow one."""
    from nanobot.agent.tools.message import MessageTool
    from nanobot.bus.events import OutboundMessage

    sent: list[OutboundMessage] = []

    async def _send(msg):
        sent.append(msg)
        if msg._delivery_future and not msg._delivery_future.done():
            msg._delivery_future.set_result(None)

    tool = MessageTool(send_callback=_send)

    # Turn 1: pinned to the resident.
    tool.start_turn(channel="whatsapp", chat_id=_RESIDENT_HANDLE)
    bad = await tool.execute(content="leak", channel="whatsapp", chat_id=_GUEST_HANDLE)
    assert "is not permitted" in bad

    # Turn 2: open. (E.g., a heartbeat using scoped() instead, or a
    # context that hasn't yet pinned.)
    tool.start_turn()
    ok = await tool.execute(content="hi", channel="whatsapp", chat_id=_GUEST_HANDLE)
    assert ok.startswith("Message sent"), (
        "stale pin from Turn 1 leaked into Turn 2 — start_turn() with no "
        "args must reset allowed_recipients to None"
    )


@pytest.mark.asyncio
async def test_scoped_inside_a_pinned_turn_restores_turn_pin_on_exit():
    """Interleaving invariant: ``scoped(allowed_recipients=...)`` nested
    inside a pinned turn must save and restore the turn's pin, not
    clear it. Without this, a sub-scope (e.g., a delegated dispatch
    inside an interactive turn) would silently widen the gate when it
    exited. The ContextVar reset-token pattern gives us this for free;
    the test locks the property so a future refactor that switches to
    plain `.set()` instead of save+restore can't silently regress it.
    """
    from nanobot.agent.tools.message import MessageTool
    from nanobot.bus.events import OutboundMessage

    async def _send(msg: OutboundMessage) -> None:
        if msg._delivery_future and not msg._delivery_future.done():
            msg._delivery_future.set_result(None)

    tool = MessageTool(send_callback=_send)

    # Turn pinned to the resident.
    tool.start_turn(channel="whatsapp", chat_id=_RESIDENT_HANDLE)

    # During scoped(): tighter pin to a different recipient. The guest is
    # allowed here, resident is not.
    with tool.scoped(allowed_recipients={("whatsapp", _GUEST_HANDLE)}):
        ok = await tool.execute(
            content="x", channel="whatsapp", chat_id=_GUEST_HANDLE,
        )
        assert ok.startswith("Message sent"), (
            "scoped() did not override the turn's pin during the with-block"
        )
        bad = await tool.execute(
            content="leak", channel="whatsapp", chat_id=_RESIDENT_HANDLE,
        )
        assert "is not permitted" in bad

    # After scoped() exits: the turn's pin (resident) must be restored.
    bad_after = await tool.execute(
        content="leak", channel="whatsapp", chat_id=_GUEST_HANDLE,
    )
    assert "is not permitted" in bad_after, (
        "scoped() did not restore the turn's pin on exit — the gate is "
        "open when it should be back to the turn's resident-only pin"
    )
    ok_after = await tool.execute(
        content="ok", channel="whatsapp", chat_id=_RESIDENT_HANDLE,
    )
    assert ok_after.startswith("Message sent")

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

  3. The main agent, mid-turn, must NEVER ``message(chat_id=guest_lid)``
     even if it has the guest's chat_id in its memory or hallucinates
     it. The MessageTool recipient gate refuses any send outside the
     turn's scope.

These tests name the invariants so a future PR that violates them fails
loudly rather than silently re-introducing the 2026-05-27 leak shape.
The 2026-05-27 incident required two simultaneous breaks: (1) guest
session existed on main (nanobot #81 collapsed the inbound ACL onto
the outbound scope lookup), and (2) heartbeat dispatch ignored the
``Recipients:`` line on non-prompt-file tasks. nanobot #108 reverted
(1); nanobot #109 closed (2) and added the MessageTool kernel guarantee.
This file verifies all three at the homer↔nanobot interop seam.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

import tools.scope_store as ss


# ── Fixture: realistic two-channel + scope_guard wiring ──────────────────────


@pytest.fixture()
def two_channel_setup(tmp_path, monkeypatch):
    """Provision:
      - A users.yaml with one household member (``ebby`` on WhatsApp).
      - A scope_store with an active two-way scope for one guest
        (``emeka`` on WhatsApp, the same shape Emeka had on 2026-05-27).
      - homer's ``outbound_scope_lookup.resolve`` installed into
        nanobot's ``scope_guard`` as the host lookup.

    Returns a dict with the two BaseChannel-derived test channels:
      - ``main``: ``allow_from=["14126920720"]`` — Ebby's number only.
      - ``guest``: ``allow_from=["14129739891"]`` — Emeka's number only,
        the way ``build_context.update_guest_config_allow_from`` would
        populate it at runtime.
    """
    # Isolated state — every test gets fresh users.yaml + scope DB so
    # module-level caches (mtime, members, schema-initialised) don't bleed.
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text(yaml.safe_dump({
        "schema_version": 2,
        "users": {
            "ebby": {
                "display_name": "Ebby",
                "role": "primary",
                "channels": {"whatsapp": "14126920720"},
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

    # Give the guest (Emeka) an active two-way scope, matching the
    # 2026-05-27 state: Emeka was an invited guest on the Denver MTB
    # Trip event, which created a scope-with-context for him.
    env = ss.make_interaction_envelope(
        scope_id="int_emeka_denver",
        name="Emeka (Denver trip guest)",
        participant_id="14129739891@s.whatsapp.net",
        channel="whatsapp",
        purpose="Coordinate the Denver MTB Trip dates",
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
        instance constructs cleanly."""
        name = "whatsapp"

        async def start(self): pass
        async def stop(self): pass
        async def send(self, msg: OutboundMessage): pass

    main = _TestChannel({"allow_from": ["14126920720"]}, MessageBus())
    guest = _TestChannel({"allow_from": ["14129739891"]}, MessageBus())

    yield {
        "main": main,
        "guest": guest,
        "ebby_phone": "14126920720",
        "emeka_phone": "14129739891",
        "lookup_mod": lookup_mod,
    }

    # Tear down the scope_guard registration so other test files start clean.
    scope_guard.set_scope_lookup(None)


# ── Invariant 1: main rejects scope-authorized guests ────────────────────────


def test_main_rejects_guest_with_active_scope(two_channel_setup):
    """The 2026-05-27-prevention invariant.

    Emeka has an active two-way scope, so homer's ``outbound_scope_lookup
    .resolve`` would authorize OUTBOUND to him. That MUST NOT
    re-authorize him at INBOUND on the main agent — the main agent owns
    the household's private context (USER.md, kids' events, finance),
    and any guest reaching it is a leak vector even before the LLM
    speaks. Pre-#108 this returned True (regression). #108 restored
    static-allow_from-only inbound semantics; this test locks it in.
    """
    setup = two_channel_setup
    assert setup["main"].is_allowed(setup["emeka_phone"]) is False, (
        "main agent accepted a guest's inbound — the outbound scope "
        "ACL is leaking into inbound authorization (#81-shape regression). "
        "Revert any changes to BaseChannel.is_allowed that consult "
        "check_inbound_authorized or the host scope lookup."
    )


def test_guest_accepts_authorized_guest(two_channel_setup):
    """The complementary positive: the guest agent's dynamically-populated
    ``allow_from`` lets the guest in. (In production, ``build_context.
    update_guest_config_allow_from`` writes scope-derived participant IDs
    into guest_config.json at startup; here we pre-seed the equivalent
    list on the test channel.)"""
    setup = two_channel_setup
    assert setup["guest"].is_allowed(setup["emeka_phone"]) is True


def test_main_accepts_household_member(two_channel_setup):
    """Baseline sanity: the household member ALWAYS reaches main."""
    setup = two_channel_setup
    assert setup["main"].is_allowed(setup["ebby_phone"]) is True


def test_guest_rejects_household_member(two_channel_setup):
    """The reverse boundary: a household member's inbound must NOT reach
    the guest agent — the guest workspace doesn't have the household's
    private context, and routing household traffic through guest would
    be wrong even if it accidentally "worked." Guest's allow_from has
    only guest participants; the household is not in that list."""
    setup = two_channel_setup
    assert setup["guest"].is_allowed(setup["ebby_phone"]) is False


def test_unknown_sender_rejected_by_both(two_channel_setup):
    """Bootstrap path: no scope, not in either allow_from → reject."""
    setup = two_channel_setup
    assert setup["main"].is_allowed("15551112222") is False
    assert setup["guest"].is_allowed("15551112222") is False


# ── Invariant 2: MessageTool recipient gate prevents cross-recipient sends ──


@pytest.mark.asyncio
async def test_message_tool_refuses_guest_chat_id_in_household_turn():
    """The kernel guarantee: an interactive turn from Ebby pins the
    MessageTool's allowed_recipients to (whatsapp, Ebby). The agent's
    tool call ``message(chat_id=guest_lid)`` must be refused at the
    tool layer — the message never reaches the channel or the scope
    guard. This is the 2026-05-27-shape leak prevention at the tool
    level: even if the LLM has a guest's chat_id in its memory and
    tries to override, the kernel refuses."""
    from nanobot.agent.tools.message import MessageTool
    from nanobot.bus.events import OutboundMessage

    sent: list[OutboundMessage] = []

    async def _send(msg):
        sent.append(msg)
        if msg._delivery_future and not msg._delivery_future.done():
            msg._delivery_future.set_result(None)

    tool = MessageTool(send_callback=_send)
    # Simulate the agent loop pinning the turn to Ebby.
    tool.start_turn(channel="whatsapp", chat_id="ebby_lid")

    # The LLM hallucinates a guest's chat_id mid-turn.
    result = await tool.execute(
        content="here's some private household info",
        channel="whatsapp",
        chat_id="emeka_lid",
    )
    assert "is not permitted" in result
    assert "emeka_lid" in result
    assert sent == [], "MessageTool let a cross-recipient send through — kernel gate broken"


@pytest.mark.asyncio
async def test_heartbeat_dispatch_pin_refuses_off_recipient_send():
    """The heartbeat-dispatch shape of the same guarantee. The dispatcher
    wraps each per-recipient call in ``MessageTool.scoped(
    allowed_recipients=[(channel, handle)])`` so an LLM attempt to
    ``message(chat_id=somebody_else)`` during that dispatch is refused
    — this is precisely the path that leaked on 2026-05-27 (Gmail scan
    dispatch passed no target=, the agent guessed Emeka's chat_id, the
    `message` tool happily sent it)."""
    from nanobot.agent.tools.message import MessageTool

    tool = MessageTool(send_callback=lambda msg: None)

    # Simulate heartbeat scoping for one task addressed to primary.
    primary_target = ("whatsapp", "ebby_lid")
    with tool.scoped(allowed_recipients={primary_target}):
        # LLM tries to override with the guest's chat_id (the 2026-05-27 shape).
        result = await tool.execute(
            content="The music academy says ...",  # mirrors the actual leak content shape
            channel="whatsapp",
            chat_id="emeka_lid",
        )
    assert "is not permitted" in result
    assert "emeka_lid" in result


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

    # Turn 1: pinned to Ebby.
    tool.start_turn(channel="whatsapp", chat_id="ebby_lid")
    bad = await tool.execute(content="leak", channel="whatsapp", chat_id="emeka_lid")
    assert "is not permitted" in bad

    # Turn 2: open. (E.g., a heartbeat using scoped() instead, or a
    # context that hasn't yet pinned.)
    tool.start_turn()
    ok = await tool.execute(content="hi", channel="whatsapp", chat_id="emeka_lid")
    assert ok.startswith("Message sent"), (
        "stale pin from Turn 1 leaked into Turn 2 — start_turn() with no "
        "args must reset allowed_recipients to None"
    )

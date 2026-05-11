"""Tests for manage_event_guest.py — event-specific guest enrollment, removal, ACL management."""

import json
from pathlib import Path

import pytest

import tools.manage_event_guest as gm
# Use the SAME manage_guest module that manage_event_guest imported
# (avoid dual-import of tools.manage_guest vs manage_guest)
mg = gm.mg


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Set up isolated environment for guest_manage tests."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    # Main nanobot config — primary users only, unchanged by guest operations
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "channels": {
            "whatsapp": {
                "allow_from": ["owner@s.whatsapp.net"]
            },
            "telegram": {
                "allowFrom": []
            }
        }
    }))
    # Guest nanobot config — all guests (WhatsApp + Telegram) are added here
    guest_config_path = tmp_path / "guest_config.json"
    guest_config_path.write_text(json.dumps({
        "channels": {
            "telegram": {
                "allowFrom": []
            },
            "whatsapp": {
                "allow_from": []
            }
        }
    }))

    # Patch manage_guest (where infrastructure lives)
    monkeypatch.setattr(mg, "ACL_FILE", events_dir / "guest_agent_acl.json")
    monkeypatch.setattr(mg, "NANOBOT_CONFIG_PATH", config_path)
    monkeypatch.setattr(mg, "GUEST_NANOBOT_CONFIG_PATH", guest_config_path)
    monkeypatch.setattr(mg, "rebuild_context", lambda: None)
    monkeypatch.setattr(mg, "restart_service", lambda service="homer-guest": True)

    # Patch manage_event_guest (event-specific)
    monkeypatch.setattr(gm, "EVENTS_DIR", events_dir)

    # Point scope_store at a temp DB so all scope operations use isolated storage.
    # manage_guest.add_guest calls scope_store without db_path; HOMER_SCOPE_DB redirects it.
    (tmp_path / "context").mkdir(exist_ok=True)
    db_path = tmp_path / "context" / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db_path))
    monkeypatch.setattr(gm, "REPO_ROOT", tmp_path)

    # Point event_store at a temp DB
    events_db = tmp_path / "state" / "events.db"
    monkeypatch.setenv("HOMER_EVENTS_DB", str(events_db))

    return tmp_path


@pytest.fixture()
def restarted(monkeypatch):
    """Capture restart_service calls; yields a list of service names called."""
    calls = []
    monkeypatch.setattr(mg, "restart_service", lambda service="homer-guest": calls.append(service) or True)
    return calls


@pytest.fixture()
def event(env):
    """Create a test event directory with status.md."""
    events_dir = env / "events"
    edir = events_dir / "mtb_colorado"
    edir.mkdir()
    (edir / "status.md").write_text("""\
# MTB Colorado
Status: Coordinating
Dates: TBD

## Guests
| Name | Phone | JID | Status | Added |
|------|-------|-----|--------|-------|

## Open Items

## Confirmed Details

## Budget

## Activity Log
| Date | What |
|------|------|
""")
    return "mtb_colorado"


# ── phone_to_jid ─────────────────────────────────────────────────────────────

class TestPhoneToJid:
    def test_standard_phone(self):
        assert mg.phone_to_jid("+15551234567") == "15551234567@s.whatsapp.net"

    def test_phone_with_dashes(self):
        assert mg.phone_to_jid("+1-555-123-4567") == "15551234567@s.whatsapp.net"

    def test_phone_with_spaces(self):
        assert mg.phone_to_jid("+1 555 123 4567") == "15551234567@s.whatsapp.net"

    def test_phone_with_parens(self):
        assert mg.phone_to_jid("+1 (555) 123-4567") == "15551234567@s.whatsapp.net"

    def test_phone_no_plus(self):
        assert mg.phone_to_jid("15551234567") == "15551234567@s.whatsapp.net"

    def test_invalid_phone_raises(self):
        with pytest.raises(ValueError):
            mg.phone_to_jid("abc")


# ── ACL management ────────────────────────────────────────────────────────────

class TestAcl:
    def test_load_acl_empty(self, env):
        assert mg.load_acl() == {}

    def test_save_and_load_acl(self, env):
        acl = {"15551234567@s.whatsapp.net": {"name": "Jake", "event_id": "trip"}}
        mg.save_acl(acl)
        assert mg.load_acl() == acl

    def test_load_acl_corrupt_file(self, env):
        acl_path = env / "events" / "guest_agent_acl.json"
        acl_path.write_text("not json")
        assert mg.load_acl() == {}


# ── do_add ────────────────────────────────────────────────────────────────────

class TestDoAdd:
    def test_add_guest(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert out["name"] == "Jake"
        assert out["contact"] == "15551234567@s.whatsapp.net"

    def test_add_guest_updates_acl(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        acl = mg.load_acl()
        assert "15551234567@s.whatsapp.net" in acl
        assert acl["15551234567@s.whatsapp.net"]["name"] == "Jake"
        assert acl["15551234567@s.whatsapp.net"]["event_id"] == "mtb_colorado"
        assert acl["15551234567@s.whatsapp.net"]["channel"] == "whatsapp"

    def test_add_guest_updates_summary(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        content = (env / "events" / "mtb_colorado" / "status.md").read_text()
        assert "## Guests (1)" in content

    def test_add_whatsapp_guest_restarts_homer_guest(self, env, event, restarted, capsys):
        """WhatsApp guest add must restart homer-guest, not homer."""
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        assert "homer-guest" in restarted
        assert "homer" not in restarted

    def test_add_telegram_guest_restarts_homer_guest(self, env, event, restarted, capsys):
        """Telegram guest add must restart homer-guest (guest agent), not homer (main agent)."""
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()
        assert "homer-guest" in restarted
        assert "homer" not in restarted

    def test_add_duplicate_guest_fails(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        with pytest.raises(SystemExit):
            gm.do_add("mtb_colorado", "Jake", "+15551234567")

    def test_add_guest_nonexistent_event_fails(self, env, capsys):
        with pytest.raises(SystemExit):
            gm.do_add("nonexistent", "Jake", "+15551234567")

    def test_add_guest_with_expires(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567", expires="2026-08-01")
        capsys.readouterr()
        acl = mg.load_acl()
        assert acl["15551234567@s.whatsapp.net"]["expires"] == "2026-08-01"

    def test_add_telegram_guest(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"
        assert out["channel"] == "telegram"
        assert out["contact"] == "tg:987654321"

    def test_add_telegram_guest_updates_acl(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()
        acl = mg.load_acl()
        assert "tg:987654321" in acl
        assert acl["tg:987654321"]["channel"] == "telegram"
        assert acl["tg:987654321"]["telegram_id"] == "987654321"

    def test_add_telegram_guest_updates_summary(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()
        content = (env / "events" / "mtb_colorado" / "status.md").read_text()
        assert "## Guests (1)" in content

    def test_add_includes_event_id(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        out = json.loads(capsys.readouterr().out)
        assert out["event_id"] == "mtb_colorado"


# ── do_remove ─────────────────────────────────────────────────────────────────

class TestDoRemove:
    def test_remove_by_name(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", name="Jake")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"
        assert mg.load_acl() == {}

    def test_remove_by_phone(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", phone="+15551234567")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"

    def test_remove_whatsapp_guest_restarts_homer_guest(self, env, event, restarted, capsys):
        """Removing a WhatsApp guest restarts homer-guest, not homer."""
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        restarted.clear()
        gm.do_remove("mtb_colorado", name="Jake")
        capsys.readouterr()
        assert "homer-guest" in restarted
        assert "homer" not in restarted

    def test_remove_nonexistent_guest_fails(self, env, event, capsys):
        with pytest.raises(SystemExit):
            gm.do_remove("mtb_colorado", name="Nobody")

    def test_remove_telegram_guest_by_name(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", name="Sam")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"
        assert mg.load_acl() == {}

    def test_remove_telegram_guest_restarts_homer_guest(self, env, event, restarted, capsys):
        """Removing a Telegram guest restarts homer-guest, not homer."""
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()
        restarted.clear()
        gm.do_remove("mtb_colorado", telegram_id="987654321")
        capsys.readouterr()
        assert "homer-guest" in restarted
        assert "homer" not in restarted

    def test_remove_includes_event_id(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", name="Jake")
        out = json.loads(capsys.readouterr().out)
        assert out["event_id"] == "mtb_colorado"


# ── do_list ───────────────────────────────────────────────────────────────────

class TestDoList:
    def test_list_empty(self, env, event, capsys):
        gm.do_list("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert out == []

    def test_list_with_guests(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("mtb_colorado", "Mike", "+15559876543")
        capsys.readouterr()
        gm.do_list("mtb_colorado")
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2
        names = {g["name"] for g in out}
        assert names == {"Jake", "Mike"}


# ── do_expire_check ──────────────────────────────────────────────────────────

class TestExpireCheck:
    def test_no_expired_guests(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567", expires="2099-12-31")
        capsys.readouterr()
        gm.do_expire_check()
        out = json.loads(capsys.readouterr().out)
        assert out["expired_count"] == 0
        assert mg.load_acl() != {}

    def test_expired_guest_removed(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567", expires="2020-01-01")
        capsys.readouterr()
        gm.do_expire_check()
        out = json.loads(capsys.readouterr().out)
        assert out["expired_count"] == 1
        assert mg.load_acl() == {}

    def test_expired_telegram_guest_removed(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Sam", channel="telegram",
                  telegram_id="987654321", expires="2020-01-01")
        capsys.readouterr()
        gm.do_expire_check()
        out = json.loads(capsys.readouterr().out)
        assert out["expired_count"] == 1

    def test_expire_telegram_guest_restarts_homer_guest(self, env, event, restarted, capsys):
        """Expiring a Telegram guest restarts homer-guest, not homer."""
        gm.do_add("mtb_colorado", "Sam", channel="telegram",
                  telegram_id="987654321", expires="2020-01-01")
        capsys.readouterr()
        restarted.clear()
        gm.do_expire_check()
        capsys.readouterr()
        assert "homer-guest" in restarted
        assert "homer" not in restarted

    def test_no_expiry_not_removed(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")  # no expires
        capsys.readouterr()
        gm.do_expire_check()
        out = json.loads(capsys.readouterr().out)
        assert out["expired_count"] == 0


# ── shared event scope ───────────────────────────────────────────────────────

class TestSharedEventScope:
    """Two guests for the same event share one scope with both as participants."""

    def test_two_guests_share_one_scope(self, env, event, capsys):
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"

        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("mtb_colorado", "Mike", "+15559876543")
        capsys.readouterr()

        scopes = ss.list_active_scopes(db_path)
        assert len(scopes) == 1, f"Expected 1 shared scope, got {len(scopes)}"
        assert scopes[0]["scope_id"] == "mtb_colorado"
        party_ids = {p["party_id"] for p in scopes[0]["participants"]}
        assert "15551234567@s.whatsapp.net" in party_ids
        assert "15559876543@s.whatsapp.net" in party_ids

    def test_both_participants_routable(self, env, event, capsys):
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"

        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("mtb_colorado", "Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()

        jake_scopes = ss.get_scopes_for_participant("15551234567@s.whatsapp.net", db_path)
        seun_scopes = ss.get_scopes_for_participant("tg:987654321", db_path)
        assert len(jake_scopes) == 1
        assert len(seun_scopes) == 1
        assert jake_scopes[0]["scope_id"] == seun_scopes[0]["scope_id"] == "mtb_colorado"

    def test_remove_one_guest_keeps_scope_for_others(self, env, event, capsys):
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"

        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("mtb_colorado", "Mike", "+15559876543")
        capsys.readouterr()

        # Remove Jake — Mike should still be in the active shared scope
        gm.do_remove("mtb_colorado", phone="+15551234567")
        capsys.readouterr()

        scopes = ss.list_active_scopes(db_path)
        assert len(scopes) == 1, "Scope should remain active for Mike"
        assert scopes[0]["scope_id"] == "mtb_colorado"
        party_ids = {p["party_id"] for p in scopes[0]["participants"]}
        assert "15551234567@s.whatsapp.net" not in party_ids
        assert "15559876543@s.whatsapp.net" in party_ids

    def test_remove_last_guest_terminates_scope(self, env, event, capsys):
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"

        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()

        gm.do_remove("mtb_colorado", phone="+15551234567")
        capsys.readouterr()

        scopes = ss.list_active_scopes(db_path)
        assert len(scopes) == 0, "Scope should be terminated when last participant removed"

    def test_readd_guest_after_full_removal_reactivates_scope(self, env, event, capsys):
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"

        # Add then fully remove
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", phone="+15551234567")
        capsys.readouterr()

        # Add a new guest — scope should be reactivated, not stuck as terminated
        gm.do_add("mtb_colorado", "Mike", "+15559876543")
        capsys.readouterr()

        scopes = ss.list_active_scopes(db_path)
        assert len(scopes) == 1, "Scope should be active after re-adding a guest"
        assert scopes[0]["scope_id"] == "mtb_colorado"
        party_ids = {p["party_id"] for p in scopes[0]["participants"]}
        assert "15559876543@s.whatsapp.net" in party_ids
        # The previously removed guest must not be resurrected in the envelope
        assert "15551234567@s.whatsapp.net" not in party_ids
        assert len(party_ids) == 1

    def test_remove_multi_event_guest_by_name_from_second_event(self, env, capsys):
        """Removing a guest by name from their second event must work even though
        the global ACL entry still carries the first event's event_id."""
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"
        events_dir = env / "events"

        for eid in ("mtb_colorado", "denver_mtb"):
            edir = events_dir / eid
            edir.mkdir(exist_ok=True)
            (edir / "status.md").write_text(f"""\
# {eid}
Status: Coordinating

## Guests
| Name | Phone | JID | Status | Added |
|------|-------|-----|--------|-------|

## Activity Log
| Date | What |
|------|------|
""")

        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("denver_mtb", "Jake", "+15551234567")
        capsys.readouterr()

        # Remove Jake from denver_mtb by name — ACL still says event_id=mtb_colorado
        gm.do_remove("denver_mtb", name="Jake")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"

        # Jake should still be in the ACL (mtb_colorado scope still active)
        acl = mg.load_acl()
        assert "15551234567@s.whatsapp.net" in acl

        # denver_mtb scope should be terminated; mtb_colorado still active
        denver_scopes = ss.get_scopes_for_participant("15551234567@s.whatsapp.net", db_path)
        assert all(s["scope_id"] != "denver_mtb" for s in denver_scopes)
        mtb_scopes = [s for s in denver_scopes if s["scope_id"] == "mtb_colorado"]
        assert len(mtb_scopes) == 1

    def test_guest_in_two_events_keeps_acl_when_removed_from_one(self, env, capsys):
        """Removing a guest from one event must not evict them from the global ACL
        if they're still a participant in another active event scope."""
        import tools.scope_store as ss
        db_path = env / "context" / "scopes.db"
        events_dir = env / "events"

        # Create two event directories
        for eid in ("mtb_colorado", "denver_mtb"):
            edir = events_dir / eid
            edir.mkdir(exist_ok=True)
            (edir / "status.md").write_text(f"""\
# {eid}
Status: Coordinating

## Guests
| Name | Phone | JID | Status | Added |
|------|-------|-----|--------|-------|

## Activity Log
| Date | What |
|------|------|
""")

        # Jake joins both events
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("denver_mtb", "Jake", "+15551234567")
        capsys.readouterr()

        jake_key = "15551234567@s.whatsapp.net"

        # Sanity: Jake is in ACL and in both scopes
        acl = mg.load_acl()
        assert jake_key in acl
        jake_scopes = ss.get_scopes_for_participant(jake_key, db_path)
        assert len(jake_scopes) == 2

        # Remove Jake from mtb_colorado only
        gm.do_remove("mtb_colorado", phone="+15551234567")
        capsys.readouterr()

        # Jake must still be in the global ACL (still in denver_mtb)
        acl_after = mg.load_acl()
        assert jake_key in acl_after, "Jake should remain in ACL while still in denver_mtb"

        # mtb_colorado scope should be terminated (no participants left)
        mtb_scope = ss.get_scope("mtb_colorado", db_path)
        assert mtb_scope is None or mtb_scope.get("_status") == "terminated"

        # denver_mtb scope should still be active with Jake in it
        denver_scopes = ss.get_scopes_for_participant(jake_key, db_path)
        assert len(denver_scopes) == 1
        assert denver_scopes[0]["scope_id"] == "denver_mtb"


# ── event_store integration ──────────────────────────────────────────────────

class TestEventStoreIntegration:
    def test_add_guest_writes_to_event_store(self, env, event, capsys):
        import tools.event_store as es
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        guests = es.list_guests("mtb_colorado")
        assert len(guests) == 1
        assert guests[0]["name"] == "Jake"
        assert guests[0]["participant_id"] == "15551234567@s.whatsapp.net"
        assert guests[0]["rsvp_status"] == "enrolled"

    def test_remove_guest_deletes_from_event_store(self, env, event, capsys):
        import tools.event_store as es
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", name="Jake")
        capsys.readouterr()
        guests = es.list_guests("mtb_colorado")
        assert len(guests) == 0

    def test_guest_summary_updates_after_add(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("mtb_colorado", "Mike", "+15559876543")
        capsys.readouterr()
        content = (env / "events" / "mtb_colorado" / "status.md").read_text()
        assert "## Guests (2)" in content

    def test_guest_summary_updates_after_remove(self, env, event, capsys):
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        gm.do_add("mtb_colorado", "Mike", "+15559876543")
        capsys.readouterr()
        gm.do_remove("mtb_colorado", name="Jake")
        capsys.readouterr()
        content = (env / "events" / "mtb_colorado" / "status.md").read_text()
        assert "## Guests (1)" in content


# ── Analytics wiring ─────────────────────────────────────────────────────────

class TestAnalyticsEvents:
    def test_add_event_guest_fires_guest_added(self, env, event, capsys):
        from unittest.mock import patch
        with patch("tools.analytics.events.track_guest_added") as mock:
            gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        assert mock.called
        _, kwargs = mock.call_args
        assert kwargs["scope_id"] == "mtb_colorado"
        assert kwargs["channel"] == "whatsapp"

    def test_remove_event_guest_fires_guest_removed(self, env, event, capsys):
        from unittest.mock import patch
        gm.do_add("mtb_colorado", "Jake", "+15551234567")
        capsys.readouterr()
        with patch("tools.analytics.events.track_guest_removed") as mock:
            gm.do_remove("mtb_colorado", name="Jake")
        capsys.readouterr()
        assert mock.called
        _, kwargs = mock.call_args
        assert kwargs["scope_id"] == "mtb_colorado"
        assert kwargs["channel"] == "whatsapp"

    def test_analytics_failure_does_not_block_add(self, env, event, capsys):
        from unittest.mock import patch
        with patch(
            "tools.analytics.events.track_guest_added",
            side_effect=RuntimeError("posthog down"),
        ):
            gm.do_add("mtb_colorado", "Jake", "+15551234567")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "added"

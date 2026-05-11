"""Tests for manage_guest.py — generic guest management (no event knowledge)."""

import json

import pytest

import tools.manage_guest as mg


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Set up isolated environment for manage_guest tests."""
    acl_dir = tmp_path / "events"
    acl_dir.mkdir()
    guest_config_path = tmp_path / "guest_config.json"
    guest_config_path.write_text(json.dumps({
        "channels": {
            "telegram": {"allowFrom": []},
            "whatsapp": {"allow_from": []},
        }
    }))
    main_config_path = tmp_path / "config.json"
    main_config_path.write_text(json.dumps({
        "channels": {
            "whatsapp": {"allow_from": ["owner@s.whatsapp.net"]},
            "telegram": {"allowFrom": []},
        }
    }))

    monkeypatch.setattr(mg, "ACL_FILE", acl_dir / "guest_agent_acl.json")
    monkeypatch.setattr(mg, "NANOBOT_CONFIG_PATH", main_config_path)
    monkeypatch.setattr(mg, "GUEST_NANOBOT_CONFIG_PATH", guest_config_path)
    monkeypatch.setattr(mg, "rebuild_context", lambda: None)
    monkeypatch.setattr(mg, "restart_service", lambda service="homer-guest": True)

    return tmp_path


@pytest.fixture()
def restarted(monkeypatch):
    calls = []
    monkeypatch.setattr(mg, "restart_service", lambda service="homer-guest": calls.append(service) or True)
    return calls


# ── resolve_participant_id ───────────────────────────────────────────────────

class TestResolveParticipantId:
    def test_whatsapp(self):
        assert mg.resolve_participant_id(phone="+15551234567") == "15551234567@s.whatsapp.net"

    def test_telegram(self):
        assert mg.resolve_participant_id(channel="telegram", telegram_id="123") == "tg:123"

    def test_whatsapp_missing_phone(self):
        with pytest.raises(ValueError):
            mg.resolve_participant_id()

    def test_telegram_missing_id(self):
        with pytest.raises(ValueError):
            mg.resolve_participant_id(channel="telegram")


# ── add_guest ────────────────────────────────────────────────────────────────

class TestAddGuest:
    def test_add_whatsapp_guest(self, env, capsys):
        result = mg.add_guest("Jake", phone="+15551234567")
        assert result["status"] == "added"
        assert result["contact"] == "15551234567@s.whatsapp.net"
        assert result["channel"] == "whatsapp"

    def test_add_telegram_guest(self, env, capsys):
        result = mg.add_guest("Sam", channel="telegram", telegram_id="987654321")
        assert result["status"] == "added"
        assert result["contact"] == "tg:987654321"

    def test_add_guest_writes_acl(self, env):
        mg.add_guest("Jake", phone="+15551234567")
        acl = mg.load_acl()
        assert "15551234567@s.whatsapp.net" in acl
        assert acl["15551234567@s.whatsapp.net"]["name"] == "Jake"

    def test_add_guest_with_extra_acl(self, env):
        mg.add_guest("Jake", phone="+15551234567", extra_acl={"event_id": "trip"})
        acl = mg.load_acl()
        assert acl["15551234567@s.whatsapp.net"]["event_id"] == "trip"

    def test_add_duplicate_fails(self, env):
        mg.add_guest("Jake", phone="+15551234567")
        with pytest.raises(SystemExit):
            mg.add_guest("Jake", phone="+15551234567")

    def test_add_guest_restarts_homer_guest(self, env, restarted):
        mg.add_guest("Jake", phone="+15551234567")
        assert "homer-guest" in restarted
        assert "homer" not in restarted


# ── remove_guest ─────────────────────────────────────────────────────────────

class TestRemoveGuest:
    def test_remove_whatsapp_guest(self, env):
        mg.add_guest("Jake", phone="+15551234567")
        result = mg.remove_guest("15551234567@s.whatsapp.net", "whatsapp")
        assert result["status"] == "removed"
        assert result["name"] == "Jake"
        assert mg.load_acl() == {}

    def test_remove_telegram_guest(self, env):
        mg.add_guest("Sam", channel="telegram", telegram_id="987654321")
        result = mg.remove_guest("tg:987654321", "telegram")
        assert result["status"] == "removed"
        assert mg.load_acl() == {}

    def test_remove_nonexistent_fails(self, env):
        with pytest.raises(SystemExit):
            mg.remove_guest("nobody@s.whatsapp.net", "whatsapp")

    def test_remove_guest_restarts_homer_guest(self, env, restarted):
        mg.add_guest("Jake", phone="+15551234567")
        restarted.clear()
        mg.remove_guest("15551234567@s.whatsapp.net", "whatsapp")
        assert "homer-guest" in restarted
        assert "homer" not in restarted


# ── expire_guests ────────────────────────────────────────────────────────────

class TestExpireGuests:
    def test_expire_returns_expired(self, env):
        mg.add_guest("Jake", phone="+15551234567", expires="2020-01-01")
        expired = mg.expire_guests()
        assert len(expired) == 1
        assert expired[0]["name"] == "Jake"
        assert mg.load_acl() == {}

    def test_expire_skips_future(self, env):
        mg.add_guest("Jake", phone="+15551234567", expires="2099-12-31")
        expired = mg.expire_guests()
        assert len(expired) == 0
        assert mg.load_acl() != {}

    def test_expire_skips_no_expiry(self, env):
        mg.add_guest("Jake", phone="+15551234567")
        expired = mg.expire_guests()
        assert len(expired) == 0

    def test_expire_does_not_rebuild(self, env, monkeypatch):
        """expire_guests() must NOT call rebuild — caller's responsibility."""
        rebuild_called = []
        monkeypatch.setattr(mg, "rebuild_context", lambda: rebuild_called.append(True))
        mg.add_guest("Jake", phone="+15551234567", expires="2020-01-01")
        rebuild_called.clear()
        mg.expire_guests()
        assert rebuild_called == []

    def test_expire_preserves_extra_acl(self, env):
        mg.add_guest("Jake", phone="+15551234567", expires="2020-01-01",
                     extra_acl={"event_id": "trip"})
        expired = mg.expire_guests()
        assert expired[0]["event_id"] == "trip"


# ── do_list ──────────────────────────────────────────────────────────────────

class TestDoList:
    def test_list_empty(self, env, capsys):
        mg.do_list()
        out = json.loads(capsys.readouterr().out)
        assert out == []

    def test_list_all_guests(self, env, capsys):
        mg.add_guest("Jake", phone="+15551234567")
        mg.add_guest("Sam", channel="telegram", telegram_id="987654321")
        capsys.readouterr()
        mg.do_list()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2
        names = {g["name"] for g in out}
        assert names == {"Jake", "Sam"}


# ── update_lid ──────────────────────────────────────────────────────────────

class TestUpdateLid:
    def test_update_lid_stores_in_acl(self, env):
        mg.add_guest("Emeka", phone="+14125550002")
        result = mg.update_lid("+14125550002", "914125550002")
        assert result["status"] == "lid_updated"
        assert result["name"] == "Emeka"
        assert result["lid"] == "914125550002"

        acl = mg.load_acl()
        assert acl["14125550002@s.whatsapp.net"]["lid"] == "914125550002"

    def test_update_lid_adds_to_allow_from(self, env):
        mg.add_guest("Emeka", phone="+14125550002")
        result = mg.update_lid("+14125550002", "914125550002")
        assert result["allow_from_updated"] is True

        config = json.loads((env / "guest_config.json").read_text())
        assert "914125550002" in config["channels"]["whatsapp"]["allow_from"]

    def test_update_lid_idempotent(self, env):
        mg.add_guest("Emeka", phone="+14125550002")
        mg.update_lid("+14125550002", "914125550002")
        # Second call should succeed without error
        result = mg.update_lid("+14125550002", "914125550002")
        assert result["status"] == "lid_updated"

    def test_update_lid_unknown_phone_exits(self, env):
        with pytest.raises(SystemExit):
            mg.update_lid("+19999999999", "123456")

    def test_update_lid_strips_non_digits(self, env):
        mg.add_guest("Wale", phone="+14125550003")
        result = mg.update_lid("+14125550003", "914-125-550003@lid")
        assert result["lid"] == "914125550003"

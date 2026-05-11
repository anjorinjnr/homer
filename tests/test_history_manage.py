"""Tests for history_manage.py — scope mutations and identifier resolution."""

import json
import uuid

import pytest

import tools.history_manage as hm


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def household_env(monkeypatch):
    monkeypatch.setenv("HOMER_HOUSEHOLD_ID", _uuid())


@pytest.fixture(autouse=True)
def stub_rebuild(monkeypatch):
    """Avoid spawning build_context during scope mutations."""
    monkeypatch.setattr(hm, "_rebuild_guest_config", lambda: None)


# ── _resolve_contributor_id ────────────────────────────────────────────────────

class TestResolveContributorId:
    def test_uuid_passes_through(self):
        cid = _uuid()
        resolved, err = hm._resolve_contributor_id(cid)
        assert resolved == cid
        assert err is None

    def test_uuid_uppercase_passes_through(self):
        cid = _uuid().upper()
        resolved, err = hm._resolve_contributor_id(cid)
        assert resolved == cid
        assert err is None

    def test_empty_string_returns_error(self):
        resolved, err = hm._resolve_contributor_id("")
        assert resolved is None
        assert err and "empty" in err

    def test_jid_resolves_via_phone_lookup(self, monkeypatch):
        cid = _uuid()
        captured = {}

        def fake_lookup(hid, phone):
            captured["phone"] = phone
            return {"id": cid, "display_name": "Mom"}

        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", fake_lookup)
        resolved, err = hm._resolve_contributor_id("14125551234@s.whatsapp.net")
        assert resolved == cid
        assert err is None
        assert captured["phone"] == "14125551234"

    def test_jid_with_ten_digit_phone_normalised(self, monkeypatch):
        cid = _uuid()
        captured = {}

        def fake_lookup(hid, phone):
            captured["phone"] = phone
            return {"id": cid}

        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", fake_lookup)
        resolved, err = hm._resolve_contributor_id("4125551234@s.whatsapp.net")
        assert resolved == cid
        # 10-digit phone gets prepended '1' before lookup
        assert captured["phone"] == "14125551234"

    def test_jid_unknown_phone_returns_error(self, monkeypatch):
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: None)
        resolved, err = hm._resolve_contributor_id("19999999999@s.whatsapp.net")
        assert resolved is None
        assert err and "19999999999" in err
        assert "history_invite.py --list" in err

    def test_lid_resolves_via_scope_participants(self, monkeypatch):
        cid = _uuid()
        envelope = {
            "scope_id": "family_history_abc",
            "participants": [
                {"party_id": "209650423185503@lid", "contributor_id": cid, "name": "Helen"},
                {"party_id": "14125559999@s.whatsapp.net", "contributor_id": _uuid(), "name": "Dad"},
            ],
        }
        monkeypatch.setattr(hm.scope_store, "get_scope", lambda sid: envelope)
        resolved, err = hm._resolve_contributor_id("209650423185503@lid")
        assert resolved == cid
        assert err is None

    def test_lid_unknown_returns_helpful_error(self, monkeypatch):
        envelope = {"scope_id": "family_history_abc", "participants": []}
        monkeypatch.setattr(hm.scope_store, "get_scope", lambda sid: envelope)
        resolved, err = hm._resolve_contributor_id("999999999999999@lid")
        assert resolved is None
        assert err and "999999999999999@lid" in err
        assert "history_invite.py --list" in err

    def test_lid_with_no_scope_returns_error(self, monkeypatch):
        monkeypatch.setattr(hm.scope_store, "get_scope", lambda sid: None)
        resolved, err = hm._resolve_contributor_id("209650423185503@lid")
        assert resolved is None
        assert err and "Pass the contributor UUID" in err

    def test_raw_phone_resolves(self, monkeypatch):
        cid = _uuid()
        captured = {}

        def fake_lookup(hid, phone):
            captured["phone"] = phone
            return {"id": cid}

        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", fake_lookup)
        resolved, err = hm._resolve_contributor_id("14125551234")
        assert resolved == cid
        assert captured["phone"] == "14125551234"

    def test_formatted_phone_resolves(self, monkeypatch):
        cid = _uuid()
        captured = {}

        def fake_lookup(hid, phone):
            captured["phone"] = phone
            return {"id": cid}

        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", fake_lookup)
        resolved, err = hm._resolve_contributor_id("+1 (412) 555-1234")
        assert resolved == cid
        assert captured["phone"] == "14125551234"

    def test_unknown_phone_returns_error(self, monkeypatch):
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: None)
        resolved, err = hm._resolve_contributor_id("14125551234")
        assert resolved is None
        assert err and "14125551234" in err

    def test_unparseable_input_returns_error(self):
        resolved, err = hm._resolve_contributor_id("not-a-uuid-or-phone")
        assert resolved is None
        assert err  # exact phrasing varies; just confirm we got one


# ── do_context ────────────────────────────────────────────────────────────────

class TestDoContext:
    def test_resolves_jid_then_returns_context(self, monkeypatch, capsys):
        cid = _uuid()
        contributor = {
            "id": cid, "display_name": "Helen", "relationship": "Aunt",
            "status": "active",
        }
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: contributor)
        monkeypatch.setattr(hm.hs, "get_contributor_by_id", lambda cid: contributor)
        monkeypatch.setattr(hm.hs, "list_recent_fragments", lambda hid, contributor_id, limit: [])
        monkeypatch.setattr(hm.hs, "list_open_threads", lambda hid, cid, limit: [])
        monkeypatch.setattr(hm.hs, "get_era_coverage", lambda hid, cid: [])
        monkeypatch.setattr(hm.hs, "build_reentry_preamble", lambda hid, cid: None)

        hm.do_context("14126364194@s.whatsapp.net")
        out = json.loads(capsys.readouterr().out)
        assert out["contributor"]["id"] == cid
        assert out["contributor"]["display_name"] == "Helen"
        # Field is always present in the output, null when no nudge.
        assert out["reentry_preamble"] is None

    def test_surfaces_reentry_preamble_when_set(self, monkeypatch, capsys):
        cid = _uuid()
        contributor = {
            "id": cid, "display_name": "Helen", "relationship": "Aunt",
            "status": "active",
        }
        preamble = {"count": 2, "message": "I went back through our last chat — added 2 contributions to your record. You can see them when you have a moment."}
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: contributor)
        monkeypatch.setattr(hm.hs, "get_contributor_by_id", lambda cid: contributor)
        monkeypatch.setattr(hm.hs, "list_recent_fragments", lambda hid, contributor_id, limit: [])
        monkeypatch.setattr(hm.hs, "list_open_threads", lambda hid, cid, limit: [])
        monkeypatch.setattr(hm.hs, "get_era_coverage", lambda hid, cid: [])
        monkeypatch.setattr(hm.hs, "build_reentry_preamble", lambda hid, cid: preamble)

        hm.do_context("14126364194@s.whatsapp.net")
        out = json.loads(capsys.readouterr().out)
        assert out["reentry_preamble"] == preamble

    def test_unresolvable_id_exits_with_error(self, monkeypatch, capsys):
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: None)
        with pytest.raises(SystemExit):
            hm.do_context("19999999999@s.whatsapp.net")
        out = json.loads(capsys.readouterr().out)
        assert "error" in out


# ── do_write_artifact ─────────────────────────────────────────────────────────

class TestDoWriteArtifact:
    def test_resolves_phone_then_writes(self, monkeypatch, capsys):
        cid = _uuid()
        contributor = {"id": cid, "status": "active", "display_name": "Helen"}
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: contributor)
        monkeypatch.setattr(hm.hs, "get_contributor_by_id", lambda cid: contributor)
        captured = {}

        def fake_insert(**kw):
            captured.update(kw)
            return {"id": _uuid()}

        monkeypatch.setattr(hm.hs, "insert_artifact", fake_insert)
        hm.do_write_artifact("14125551234", "text", "Hi!", None, None, "whatsapp")
        out = json.loads(capsys.readouterr().out)
        assert out["contributor_id"] == cid
        assert captured["contributor_id"] == cid
        assert captured["body"] == "Hi!"

    def test_activates_pending_contributor(self, monkeypatch, capsys):
        cid = _uuid()
        pending = {"id": cid, "status": "pending", "display_name": "Helen"}
        activated_calls = []
        monkeypatch.setattr(hm.hs, "get_contributor_by_id", lambda cid: pending)
        monkeypatch.setattr(
            hm.hs, "activate_contributor",
            lambda cid: activated_calls.append(cid),
        )
        monkeypatch.setattr(hm.hs, "insert_artifact", lambda **kw: {"id": _uuid()})

        hm.do_write_artifact(cid, "text", "Hello", None, None, "whatsapp")
        assert activated_calls == [cid]

    def test_unknown_contributor_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(hm.hs, "get_contributor_by_phone", lambda hid, phone: None)
        with pytest.raises(SystemExit):
            hm.do_write_artifact("14125551234", "text", "Hi", None, None, "whatsapp")
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

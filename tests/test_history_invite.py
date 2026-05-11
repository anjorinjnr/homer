"""Tests for history_invite.py — contributor invite/verify/lookup flows."""

import json
import uuid

import pytest

import tools.history_invite as hi
# Patch through hi.hs (the module reference history_invite holds), not a
# separately-imported tools.history_store, which would be a different object.


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def household_env(monkeypatch):
    monkeypatch.setenv("HOMER_HOUSEHOLD_ID", _uuid())


@pytest.fixture(autouse=True)
def stub_rebuild(monkeypatch):
    """Prevent do_archive's scope-cleanup path from invoking build_context."""
    monkeypatch.setattr(hi, "_rebuild_guest_config", lambda: None)


# ── _normalise_phone ──────────────────────────────────────────────────────────

class TestNormalisePhone:
    def test_eleven_digit_with_leading_one_passes_through(self):
        digits, warning = hi._normalise_phone("14125551234")
        assert digits == "14125551234"
        assert warning is None

    def test_strips_formatting_from_e164(self):
        digits, warning = hi._normalise_phone("+1 (412) 555-1234")
        assert digits == "14125551234"
        assert warning is None

    def test_ten_digit_us_assumed_and_prepended(self):
        digits, warning = hi._normalise_phone("4125551234")
        assert digits == "14125551234"
        assert warning is not None
        assert "10-digit US" in warning
        assert "1" + "4125551234" in warning

    def test_strips_formatting_from_ten_digit(self):
        digits, warning = hi._normalise_phone("(412) 555-1234")
        assert digits == "14125551234"
        assert warning is not None

    def test_unrecognised_format_returns_warning(self):
        digits, warning = hi._normalise_phone("447911123456")
        # 12 digits → not 10, not 11-with-leading-1
        assert digits == "447911123456"
        assert warning is not None
        assert "country code" in warning

    def test_short_number_returns_warning(self):
        digits, warning = hi._normalise_phone("12345")
        assert digits == "12345"
        assert warning is not None
        assert "5 digits" in warning

    def test_empty_string_returns_warning(self):
        digits, warning = hi._normalise_phone("")
        assert digits == ""
        assert warning is not None


# ── do_invite ─────────────────────────────────────────────────────────────────

class TestDoInvite:
    def test_invite_by_phone_creates_pending_contributor(self, monkeypatch, capsys):
        hid = "test-household"
        monkeypatch.setenv("HOMER_HOUSEHOLD_ID", hid)

        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)
        created = {"id": _uuid(), "display_name": "Mom", "status": "pending"}
        monkeypatch.setattr(hi.hs, "create_contributor", lambda **kw: created)

        hi.do_invite("Mom", phone="14125551234", email=None, relationship="Mom")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "invited"
        assert out["phone"] == "14125551234"
        assert "contributor_id" in out

    def test_invite_by_email_when_no_phone(self, monkeypatch, capsys):
        monkeypatch.setattr(hi.hs, "create_contributor",
                           lambda **kw: {"id": _uuid(), "display_name": "Web User", "status": "pending"})

        hi.do_invite("Web User", phone=None, email="web@example.com", relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "invited"
        assert out["email"] == "web@example.com"

    def test_invite_requires_phone_or_email(self, capsys):
        with pytest.raises(SystemExit):
            hi.do_invite("Nobody", phone=None, email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_duplicate_phone_returns_already_exists(self, monkeypatch, capsys):
        existing = {"id": _uuid(), "display_name": "Mom", "status": "pending"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: existing)

        hi.do_invite("Mom", phone="14125551234", email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "already_exists"

    def test_archived_phone_returns_error(self, monkeypatch, capsys):
        existing = {"id": _uuid(), "display_name": "Old", "status": "archived"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: existing)

        with pytest.raises(SystemExit):
            hi.do_invite("Old", phone="14125550000", email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_ten_digit_phone_normalised_and_warning_surfaced(self, monkeypatch, capsys):
        captured: dict = {}

        def fake_create(**kw):
            captured.update(kw)
            return {"id": _uuid(), "display_name": kw["display_name"], "status": "pending"}

        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)
        monkeypatch.setattr(hi.hs, "create_contributor", fake_create)

        hi.do_invite("Mom", phone="4125551234", email=None, relationship="Mom")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "invited"
        assert out["phone"] == "14125551234"
        assert "phone_warning" in out
        assert "10-digit US" in out["phone_warning"]
        # Lookup and storage should both use the normalised value
        assert captured["phone"] == "14125551234"

    def test_formatted_phone_normalised_no_warning(self, monkeypatch, capsys):
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)
        monkeypatch.setattr(
            hi.hs, "create_contributor",
            lambda **kw: {"id": _uuid(), "display_name": kw["display_name"], "status": "pending"},
        )
        hi.do_invite("Mom", phone="+1 (412) 555-1234", email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert out["phone"] == "14125551234"
        assert "phone_warning" not in out

    def test_unrecognised_phone_warning_in_already_exists(self, monkeypatch, capsys):
        existing = {"id": _uuid(), "display_name": "Mom", "status": "pending"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: existing)
        hi.do_invite("Mom", phone="447911123456", email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "already_exists"
        assert "phone_warning" in out

    def test_invite_surfaces_redeemable_url(self, monkeypatch, capsys):
        token = "x" * 32
        created = {
            "id": _uuid(),
            "display_name": "Mom",
            "status": "pending",
            "invite_token": token,
        }
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)
        monkeypatch.setattr(hi.hs, "create_contributor", lambda **kw: created)
        monkeypatch.setenv("HISTORY_FRONTEND_URL", "https://history.example.com")

        hi.do_invite("Mom", phone="14125551234", email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert out["invite_token"] == token
        assert out["invite_url"] == f"https://history.example.com/invite/{token}"

    def test_invite_omits_url_when_token_absent(self, monkeypatch, capsys):
        # Old rows / mocks without invite_token: don't fabricate a URL.
        created = {"id": _uuid(), "display_name": "Mom", "status": "pending"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)
        monkeypatch.setattr(hi.hs, "create_contributor", lambda **kw: created)
        hi.do_invite("Mom", phone="14125551234", email=None, relationship=None)
        out = json.loads(capsys.readouterr().out)
        assert "invite_token" not in out
        assert "invite_url" not in out


# ── do_verify ─────────────────────────────────────────────────────────────────

class TestDoVerify:
    def test_activates_pending_contributor(self, monkeypatch, capsys):
        cid = _uuid()
        contributor = {"id": cid, "display_name": "Mom", "status": "pending"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: contributor)
        activated = {"id": cid, "display_name": "Mom", "status": "active"}
        monkeypatch.setattr(hi.hs, "activate_contributor", lambda cid: activated)

        hi.do_verify("14125551234")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "activated"
        assert out["contributor_id"] == cid

    def test_already_active_is_idempotent(self, monkeypatch, capsys):
        contributor = {"id": _uuid(), "display_name": "Mom", "status": "active"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: contributor)

        hi.do_verify("14125551234")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "already_active"

    def test_archived_contributor_is_error(self, monkeypatch, capsys):
        contributor = {"id": _uuid(), "display_name": "Old", "status": "archived"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: contributor)

        with pytest.raises(SystemExit):
            hi.do_verify("14125550000")
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_not_found_is_error(self, monkeypatch, capsys):
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)

        with pytest.raises(SystemExit):
            hi.do_verify("99999999999")
        out = json.loads(capsys.readouterr().out)
        assert "error" in out


# ── do_list ───────────────────────────────────────────────────────────────────

class TestDoList:
    def test_returns_all_contributors(self, monkeypatch, capsys):
        contributors = [
            {"id": _uuid(), "display_name": "Mom", "relationship": "Mom",
             "phone": "14125551234", "email": None, "status": "active",
             "role": "contributor", "verified_at": "2026-01-01T00:00:00Z"},
            {"id": _uuid(), "display_name": "Grandpa", "relationship": "Grandpa",
             "phone": "14125555678", "email": None, "status": "pending",
             "role": "contributor", "verified_at": None},
        ]
        monkeypatch.setattr(hi.hs, "list_contributors", lambda hid: contributors)

        hi.do_list()
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2
        assert out[0]["display_name"] == "Mom"
        assert out[1]["status"] == "pending"

    def test_empty_list(self, monkeypatch, capsys):
        monkeypatch.setattr(hi.hs, "list_contributors", lambda hid: [])
        hi.do_list()
        out = json.loads(capsys.readouterr().out)
        assert out == []


# ── do_archive ────────────────────────────────────────────────────────────────

class TestDoArchive:
    def test_archives_contributor(self, monkeypatch, capsys):
        cid = _uuid()
        monkeypatch.setattr(hi.hs, "archive_contributor", lambda cid: {"id": cid, "status": "archived"})
        monkeypatch.setattr(hi.scope_store, "get_scope", lambda sid: None)
        hi.do_archive(cid)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "archived"
        assert out["scope_participant_removed"] is False

    def test_not_found_is_error(self, monkeypatch, capsys):
        monkeypatch.setattr(hi.hs, "archive_contributor", lambda cid: {})
        with pytest.raises(SystemExit):
            hi.do_archive(_uuid())

    def test_removes_participant_and_rebuilds_when_in_scope(self, monkeypatch, capsys):
        cid = _uuid()
        envelope = {
            "scope_id": "family_history_abc",
            "participants": [
                {"party_id": "14125551234@s.whatsapp.net", "contributor_id": cid, "name": "Mom"},
                {"party_id": "14125559999@s.whatsapp.net", "contributor_id": _uuid(), "name": "Dad"},
            ],
        }
        updated_envelopes: list[dict] = []
        rebuild_calls: list[bool] = []

        monkeypatch.setattr(hi.hs, "archive_contributor", lambda cid: {"id": cid, "status": "archived"})
        monkeypatch.setattr(hi.scope_store, "get_scope", lambda sid: envelope)
        monkeypatch.setattr(
            hi.scope_store, "update_scope",
            lambda sid, env: updated_envelopes.append(env),
        )
        monkeypatch.setattr(hi, "_rebuild_guest_config", lambda: rebuild_calls.append(True))

        hi.do_archive(cid)
        out = json.loads(capsys.readouterr().out)
        assert out["scope_participant_removed"] is True
        assert len(updated_envelopes) == 1
        assert all(p["contributor_id"] != cid for p in updated_envelopes[0]["participants"])
        assert rebuild_calls == [True]

    def test_no_rebuild_when_not_in_scope(self, monkeypatch, capsys):
        cid = _uuid()
        envelope = {
            "scope_id": "family_history_abc",
            "participants": [
                {"party_id": "14125559999@s.whatsapp.net", "contributor_id": _uuid(), "name": "Dad"},
            ],
        }
        rebuild_calls: list[bool] = []

        monkeypatch.setattr(hi.hs, "archive_contributor", lambda cid: {"id": cid, "status": "archived"})
        monkeypatch.setattr(hi.scope_store, "get_scope", lambda sid: envelope)
        monkeypatch.setattr(hi.scope_store, "update_scope", lambda sid, env: None)
        monkeypatch.setattr(hi, "_rebuild_guest_config", lambda: rebuild_calls.append(True))

        hi.do_archive(cid)
        out = json.loads(capsys.readouterr().out)
        assert out["scope_participant_removed"] is False
        assert rebuild_calls == []


# ── do_lookup ─────────────────────────────────────────────────────────────────

class TestDoLookup:
    def test_found(self, monkeypatch, capsys):
        contributor = {"id": _uuid(), "display_name": "Mom", "relationship": "Mom",
                       "status": "active", "role": "contributor"}
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: contributor)
        hi.do_lookup("14125551234")
        out = json.loads(capsys.readouterr().out)
        assert out["display_name"] == "Mom"
        assert out["status"] == "active"

    def test_not_found(self, monkeypatch, capsys):
        monkeypatch.setattr(hi.hs, "get_contributor_by_phone", lambda hid, phone: None)
        hi.do_lookup("00000000000")
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "not_found"

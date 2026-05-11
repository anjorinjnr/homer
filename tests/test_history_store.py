"""Tests for history_store.py — unit tests with mocked Supabase REST calls."""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

import tools.history_store as hs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _mock_client(rows: list | None = None, raise_for_status: bool = False):
    """Return a mock SupabaseClient that returns `rows` from any call."""
    client = MagicMock()
    if rows is None:
        rows = []
    client.select.return_value = rows
    client.insert.side_effect = lambda table, data: data
    client.update.return_value = rows
    client.upsert.side_effect = lambda table, data, **kw: data
    return client


# ── SupabaseClient init ────────────────────────────────────────────────────────

class TestSupabaseClientInit:
    def test_requires_supabase_url(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SUPABASE_URL"):
            hs.SupabaseClient()

    def test_requires_service_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_KEY"):
            hs.SupabaseClient()

    def test_init_sets_headers(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "tok_test")
        c = hs.SupabaseClient()
        assert "Bearer tok_test" in c._headers["Authorization"]
        assert c._headers["apikey"] == "tok_test"


# ── Phone normalization ────────────────────────────────────────────────────────

class TestNormalisePhone:
    def test_strips_non_digits_e164(self):
        digits, warning = hs.normalise_phone("+1 (412) 555-1234")
        assert digits == "14125551234"
        assert warning is None

    def test_already_e164_digits(self):
        digits, warning = hs.normalise_phone("14125551234")
        assert digits == "14125551234"
        assert warning is None

    def test_ten_digit_us_prepends_country_code(self):
        digits, warning = hs.normalise_phone("4125551234")
        assert digits == "14125551234"
        assert warning is not None

    def test_empty_string_returns_warning(self):
        digits, warning = hs.normalise_phone("")
        assert digits == ""
        assert warning is not None


# ── Contributors ──────────────────────────────────────────────────────────────

class TestCreateContributor:
    def test_creates_row_with_required_fields(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hid = _uuid()
        row = hs.create_contributor(household_id=hid, display_name="Grandma Helen")
        assert mock.insert.called
        called_data = mock.insert.call_args[0][1]
        assert called_data["household_id"] == hid
        assert called_data["display_name"] == "Grandma Helen"
        assert called_data["status"] == "pending"
        assert called_data["role"] == "contributor"

    def test_normalizes_phone(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.create_contributor(household_id=_uuid(), display_name="Mom", phone="+1 412 555 1234")
        called_data = mock.insert.call_args[0][1]
        assert called_data["phone"] == "14125551234"

    def test_lowercase_email(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.create_contributor(household_id=_uuid(), display_name="Dad", email="Dad@Example.COM")
        called_data = mock.insert.call_args[0][1]
        assert called_data["email"] == "dad@example.com"

    def test_sets_invite_token_for_redemption(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.create_contributor(household_id=_uuid(), display_name="Mom", phone="14125551234")
        called_data = mock.insert.call_args[0][1]
        token = called_data["invite_token"]
        assert isinstance(token, str)
        assert len(token) == 32
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in token)

    def test_invite_token_unique_per_call(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.create_contributor(household_id=_uuid(), display_name="Mom", phone="14125551234")
        first = mock.insert.call_args[0][1]["invite_token"]
        hs.create_contributor(household_id=_uuid(), display_name="Dad", phone="14125559876")
        second = mock.insert.call_args[0][1]["invite_token"]
        assert first != second

    def test_synthesizes_auth_email_when_email_missing(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        monkeypatch.delenv("HISTORY_INVITE_EMAIL_DOMAIN", raising=False)
        hs.create_contributor(household_id=_uuid(), display_name="Mom", phone="14125551234")
        called_data = mock.insert.call_args[0][1]
        assert called_data["auth_email"] == f"inv-{called_data['id']}@invite.history.example.com"
        # `email` (display) stays unset; only auth_email is synthesized.
        assert "email" not in called_data

    def test_uses_real_email_for_auth_email(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.create_contributor(household_id=_uuid(), display_name="Dad", email="Dad@Example.COM")
        called_data = mock.insert.call_args[0][1]
        assert called_data["auth_email"] == "dad@example.com"
        assert called_data["email"] == "dad@example.com"

    def test_invite_email_domain_env_override(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        monkeypatch.setenv("HISTORY_INVITE_EMAIL_DOMAIN", "invite.staging.example")
        hs.create_contributor(household_id=_uuid(), display_name="Mom", phone="14125551234")
        called_data = mock.insert.call_args[0][1]
        assert called_data["auth_email"].endswith("@invite.staging.example")


class TestActivateContributor:
    def test_sets_status_active(self, monkeypatch):
        cid = _uuid()
        mock = _mock_client(rows=[{"id": cid, "status": "active"}])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.activate_contributor(cid)
        assert mock.update.called
        called_data = mock.update.call_args[0][2]
        assert called_data["status"] == "active"
        assert "verified_at" in called_data

    def test_returns_empty_dict_when_no_rows(self, monkeypatch):
        mock = _mock_client(rows=[])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.activate_contributor(_uuid())
        assert result == {}


class TestGetContributorByPhone:
    def test_returns_contributor_when_found(self, monkeypatch):
        hid = _uuid()
        cid = _uuid()
        mock = _mock_client(rows=[{"id": cid, "phone": "14125551234"}])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.get_contributor_by_phone(hid, "14125551234")
        assert result == {"id": cid, "phone": "14125551234"}

    def test_returns_none_when_not_found(self, monkeypatch):
        mock = _mock_client(rows=[])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.get_contributor_by_phone(_uuid(), "14125550000")
        assert result is None


# ── Artifacts ─────────────────────────────────────────────────────────────────

class TestInsertArtifact:
    def test_inserts_with_required_fields(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hid, cid = _uuid(), _uuid()
        hs.insert_artifact(
            household_id=hid,
            contributor_id=cid,
            channel="whatsapp",
            kind="text",
            body="My grandmother made jollof rice every Sunday.",
        )
        called_data = mock.insert.call_args[0][1]
        assert called_data["household_id"] == hid
        assert called_data["channel"] == "whatsapp"
        assert called_data["kind"] == "text"
        assert "id" in called_data
        assert called_data["source_metadata"] == {}

    def test_optional_fields_omitted_when_none(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.insert_artifact(
            household_id=_uuid(),
            contributor_id=_uuid(),
            channel="web",
            kind="image",
        )
        called_data = mock.insert.call_args[0][1]
        assert "body" not in called_data
        assert "caption" not in called_data
        assert "storage_path" not in called_data


class TestGetArtifact:
    def test_returns_artifact(self, monkeypatch):
        aid = _uuid()
        mock = _mock_client(rows=[{"id": aid, "kind": "text"}])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.get_artifact(aid)
        assert result == {"id": aid, "kind": "text"}

    def test_returns_none_when_missing(self, monkeypatch):
        mock = _mock_client(rows=[])
        monkeypatch.setattr(hs, "_client", mock)
        assert hs.get_artifact(_uuid()) is None


# ── Fragments ─────────────────────────────────────────────────────────────────

class TestInsertFragment:
    def test_inserts_with_correct_fields(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.insert_fragment(
            household_id=_uuid(),
            artifact_id=_uuid(),
            kind="person",
            payload={"name": "Helen", "birth_year": 1932},
            confidence=0.9,
            attribution="verbatim",
        )
        called_data = mock.insert.call_args[0][1]
        assert called_data["kind"] == "person"
        assert called_data["status"] == "pending"
        assert called_data["confidence"] == 0.9

    def test_entity_id_optional(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.insert_fragment(
            household_id=_uuid(),
            artifact_id=_uuid(),
            kind="place",
            payload={"name": "Lagos"},
        )
        called_data = mock.insert.call_args[0][1]
        assert "entity_id" not in called_data


# ── Era coverage ──────────────────────────────────────────────────────────────

class TestUpsertEraCoverage:
    def test_upserts_with_correct_fields(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hid, cid = _uuid(), _uuid()
        hs.upsert_era_coverage(
            household_id=hid,
            contributor_id=cid,
            era_label="childhood",
            fragment_count=5,
            richness_score=6.5,
        )
        assert mock.upsert.called
        called_data = mock.upsert.call_args[0][1]
        assert called_data["era_label"] == "childhood"
        assert called_data["fragment_count"] == 5
        assert called_data["richness_score"] == 6.5


# ── Threads ───────────────────────────────────────────────────────────────────

class TestInsertThread:
    def test_inserts_with_defaults(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hid, cid = _uuid(), _uuid()
        hs.insert_thread(
            household_id=hid,
            contributor_id=cid,
            prompt="Ask about Aunt Mary's wedding",
        )
        called_data = mock.insert.call_args[0][1]
        assert called_data["status"] == "open"
        assert called_data["priority"] == 5
        assert called_data["context"] == {}

    def test_custom_priority(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hs.insert_thread(
            household_id=_uuid(),
            contributor_id=_uuid(),
            prompt="Urgent follow-up",
            priority=9,
        )
        called_data = mock.insert.call_args[0][1]
        assert called_data["priority"] == 9


# ── Share links ───────────────────────────────────────────────────────────────

class TestShareLinks:
    def test_random_code_is_8_chars(self):
        code = hs._random_code()
        assert len(code) == 8
        # Only unambiguous chars
        allowed = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
        assert all(c in allowed for c in code)

    def test_upsert_share_link(self, monkeypatch):
        mock = _mock_client()
        monkeypatch.setattr(hs, "_client", mock)
        hid = _uuid()
        hs.upsert_share_link(hid)
        called_data = mock.upsert.call_args[0][1]
        assert called_data["household_id"] == hid
        assert len(called_data["code"]) == 8

    def test_get_share_link_by_code_uppercases(self, monkeypatch):
        mock = _mock_client(rows=[{"household_id": _uuid(), "code": "ABCD1234"}])
        monkeypatch.setattr(hs, "_client", mock)
        hs.get_share_link_by_code("abcd1234")
        called_filters = mock.select.call_args[1]["filters"]
        assert "ABCD1234" in called_filters.get("code", "")


# ── ALL_ERAS constant ─────────────────────────────────────────────────────────

class TestAllEras:
    def test_all_eight_eras_present(self):
        assert len(hs.ALL_ERAS) == 8
        assert "childhood" in hs.ALL_ERAS
        assert "late-life" in hs.ALL_ERAS
        assert "extended-family" in hs.ALL_ERAS


# ── Reentry preamble ──────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone


class TestLastUserMessageTs:
    def test_returns_none_when_no_prior_turns(self, monkeypatch):
        mock = _mock_client(rows=[])
        monkeypatch.setattr(hs, "_client", mock)
        ts = hs.last_user_message_ts(_uuid(), _uuid())
        assert ts is None

    def test_parses_iso8601_z(self, monkeypatch):
        mock = _mock_client(rows=[{"created_at": "2026-05-05T10:00:00Z"}])
        monkeypatch.setattr(hs, "_client", mock)
        ts = hs.last_user_message_ts(_uuid(), _uuid())
        assert ts is not None
        assert ts.tzinfo is not None  # tz-aware
        assert ts.year == 2026 and ts.hour == 10

    def test_filters_by_role_user(self, monkeypatch):
        mock = _mock_client(rows=[{"created_at": "2026-05-05T10:00:00Z"}])
        monkeypatch.setattr(hs, "_client", mock)
        hs.last_user_message_ts("hh-1", "c-1")
        filters = mock.select.call_args[1]["filters"]
        assert filters.get("role") == "eq.user"


class TestCountArtifactsSince:
    def test_passes_gt_filter_with_iso_timestamp(self, monkeypatch):
        mock = _mock_client(rows=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
        monkeypatch.setattr(hs, "_client", mock)
        since = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
        count = hs.count_artifacts_since("hh-1", "c-1", since)
        assert count == 3
        filters = mock.select.call_args[1]["filters"]
        assert filters.get("captured_at") == "gt.2026-05-05T10:00:00Z"

    def test_returns_zero_on_empty(self, monkeypatch):
        mock = _mock_client(rows=[])
        monkeypatch.setattr(hs, "_client", mock)
        count = hs.count_artifacts_since(
            _uuid(), _uuid(), datetime.now(timezone.utc),
        )
        assert count == 0


class TestBuildReentryPreamble:
    def test_returns_none_when_no_prior_turns(self, monkeypatch):
        mock = _mock_client(rows=[])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is None

    def test_returns_none_when_gap_too_recent(self, monkeypatch):
        # Last user turn 5 minutes ago — under the 30-minute default gap.
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        iso = recent.strftime("%Y-%m-%dT%H:%M:%SZ")
        mock = _mock_client(rows=[{"created_at": iso}])
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is None

    def test_returns_none_when_no_artifacts_since_last_turn(self, monkeypatch):
        # Last turn 1 hour ago, but extractor produced nothing.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        iso = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        mock = MagicMock()
        # First select: hist_chat_messages returns last user turn
        # Second select: hist_artifacts returns empty
        mock.select.side_effect = [
            [{"created_at": iso}],
            [],
        ]
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is None

    def test_returns_singular_phrasing_for_one(self, monkeypatch):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        iso = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        mock = MagicMock()
        mock.select.side_effect = [
            [{"created_at": iso}],
            [{"id": "a"}],
        ]
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is not None
        assert result["count"] == 1
        assert "1 contribution" in result["message"]
        # Singular only — must NOT be the plural form.
        assert "1 contributions" not in result["message"]

    def test_returns_plural_phrasing_for_many(self, monkeypatch):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        iso = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        mock = MagicMock()
        mock.select.side_effect = [
            [{"created_at": iso}],
            [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        ]
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is not None
        assert result["count"] == 3
        assert "3 contributions" in result["message"]

    def test_returns_none_on_lookup_failure(self, monkeypatch):
        # If counting throws, never block the caller — return None and let
        # the agent reply normally without a preamble.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        iso = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        mock = MagicMock()
        mock.select.side_effect = [
            [{"created_at": iso}],
            Exception("supabase down"),
        ]
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is None

    def test_env_override_for_min_minutes(self, monkeypatch):
        # Set a 5-minute gap; a 6-minutes-ago last turn should now qualify.
        monkeypatch.setenv("HISTORIAN_REENTRY_MIN_MINUTES", "5")
        old = datetime.now(timezone.utc) - timedelta(minutes=6)
        iso = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        mock = MagicMock()
        mock.select.side_effect = [
            [{"created_at": iso}],
            [{"id": "a"}],
        ]
        monkeypatch.setattr(hs, "_client", mock)
        result = hs.build_reentry_preamble(_uuid(), _uuid())
        assert result is not None
        assert result["count"] == 1

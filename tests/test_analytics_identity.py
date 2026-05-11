"""Tests for tools/analytics/identity.py."""

import hashlib
import os

import tools.analytics.identity as ident


class TestGetDistinctId:
    def test_returns_sha256_hex(self):
        result = ident.get_distinct_id("14125551234", "whatsapp")
        assert len(result) == 64  # SHA-256 hex length
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        a = ident.get_distinct_id("user@example.com", "email")
        b = ident.get_distinct_id("user@example.com", "email")
        assert a == b

    def test_channel_mixed_in(self):
        """Same identifier on different channels produces different hashes."""
        wa = ident.get_distinct_id("14125551234", "whatsapp")
        voice = ident.get_distinct_id("14125551234", "voice")
        assert wa != voice

    def test_case_insensitive(self):
        a = ident.get_distinct_id("User@Example.COM", "email")
        b = ident.get_distinct_id("user@example.com", "email")
        assert a == b

    def test_matches_raw_sha256(self):
        raw = "whatsapp:14125551234"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert ident.get_distinct_id("14125551234", "whatsapp") == expected

    def test_strips_whitespace(self):
        a = ident.get_distinct_id("  14125551234  ", "whatsapp")
        b = ident.get_distinct_id("14125551234", "whatsapp")
        assert a == b


class TestGetHouseholdId:
    def test_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("HOMER_HOUSEHOLD_ID", "abc-123")
        assert ident.get_household_id() == "abc-123"

    def test_returns_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("HOMER_HOUSEHOLD_ID", raising=False)
        assert ident.get_household_id() == ""


class TestPersonDistinctId:
    def test_slugify_basic(self):
        assert ident.slugify_person("Alex") == "alex"
        assert ident.slugify_person("Alex Johnson") == "alex_johnson"
        assert ident.slugify_person("  Alex  ") == "alex"
        assert ident.slugify_person("O'Brien") == "o_brien"

    def test_person_hash_matches_build_identity_map_shape(self):
        """Must match the hash nanobot computes for `person:<slug>` —
        otherwise homer's household_member_added and nanobot's
        message_sent attach to different person records in PostHog."""
        expected = hashlib.sha256(b"person:alex_johnson").hexdigest()
        assert ident.get_person_distinct_id("Alex Johnson") == expected

    def test_person_hash_case_insensitive(self):
        a = ident.get_person_distinct_id("ALEX")
        b = ident.get_person_distinct_id("alex")
        assert a == b

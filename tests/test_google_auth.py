"""
Tests for google_auth.has_google_token — the existence-only token gate
that gmail_fetch / calendar_fetch / morning_briefing use to SKIP cleanly
on tenants that haven't linked Google.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import google_auth


@pytest.fixture
def isolated_token_dirs(tmp_path, monkeypatch):
    """Redirect TOKENS_DIR + LEGACY_TOKEN into tmp_path so each test runs
    against an empty filesystem, regardless of the dev's actual secrets/."""
    tokens_dir = tmp_path / "tokens"
    legacy = tmp_path / "google_token.pickle"
    monkeypatch.setattr(google_auth, "TOKENS_DIR", tokens_dir)
    monkeypatch.setattr(google_auth, "LEGACY_TOKEN", legacy)
    return tokens_dir, legacy


def test_has_google_token_returns_false_when_nothing_exists(isolated_token_dirs):
    assert google_auth.has_google_token() is False
    assert google_auth.has_google_token("homer") is False
    assert google_auth.has_google_token("ad-hoc-tenant") is False


def test_has_google_token_returns_true_when_canonical_token_exists(isolated_token_dirs):
    tokens_dir, _ = isolated_token_dirs
    tokens_dir.mkdir(parents=True)
    (tokens_dir / "primary.pickle").write_bytes(b"fake-pickle")

    assert google_auth.has_google_token("primary") is True
    # Other accounts unaffected.
    assert google_auth.has_google_token("homer") is False


def test_has_google_token_default_account_is_primary(isolated_token_dirs):
    """No-arg call must check the same account as `--account primary`."""
    tokens_dir, _ = isolated_token_dirs
    tokens_dir.mkdir(parents=True)
    (tokens_dir / "primary.pickle").write_bytes(b"fake-pickle")

    assert google_auth.has_google_token() is True


def test_has_google_token_legacy_fallback_only_for_primary(isolated_token_dirs):
    """The legacy single-token file at secrets/google_token.pickle predates
    the per-account layout and is treated as the primary account's token —
    but only for `primary`. An ad-hoc account name should not pick it up."""
    _, legacy = isolated_token_dirs
    legacy.write_bytes(b"legacy-pickle")

    assert google_auth.has_google_token("primary") is True
    assert google_auth.has_google_token() is True
    assert google_auth.has_google_token("homer") is False
    assert google_auth.has_google_token("kemi") is False


def test_has_google_token_does_not_validate_or_open_pickle(isolated_token_dirs, monkeypatch):
    """Existence check only — must not import pickle, refresh, or hit the
    network. We assert by replacing pickle.load with a poison pill: if the
    helper ever tries to read the file, the test blows up."""
    tokens_dir, _ = isolated_token_dirs
    tokens_dir.mkdir(parents=True)
    (tokens_dir / "primary.pickle").write_bytes(b"not-a-real-pickle")

    def _explode(*args, **kwargs):
        raise AssertionError("has_google_token must not open the pickle")

    monkeypatch.setattr(google_auth.pickle, "load", _explode)
    assert google_auth.has_google_token("primary") is True

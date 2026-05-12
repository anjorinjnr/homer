"""
Tests for tools/accounts.py — the multi-account discovery primitive.

These pin two invariants:

1. The tool surfaces ENOUGH metadata for the agent to fan out over
   accounts (name, scopes, validity).
2. The tool surfaces NO token material — refresh_token, access_token,
   and client_secret must never appear in any output field, on any
   code path including malformed/expired tokens.
"""

import json
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import accounts  # noqa: E402
import google_auth  # noqa: E402


@pytest.fixture
def isolated_tokens(tmp_path, monkeypatch):
    """Redirect TOKENS_DIR + LEGACY_TOKEN in both google_auth and accounts to tmp_path."""
    tokens_dir = tmp_path / "tokens"
    legacy = tmp_path / "google_token.pickle"
    monkeypatch.setattr(google_auth, "TOKENS_DIR", tokens_dir)
    monkeypatch.setattr(google_auth, "LEGACY_TOKEN", legacy)
    monkeypatch.setattr(accounts, "TOKENS_DIR", tokens_dir)
    monkeypatch.setattr(accounts, "LEGACY_TOKEN", legacy)
    return tokens_dir, legacy


def _fake_creds(
    scopes: list[str],
    *,
    expired: bool = False,
    refresh_token: str | None = "rt-secret-DO-NOT-LEAK",
    token: str = "at-secret-DO-NOT-LEAK",
    client_secret: str = "cs-secret-DO-NOT-LEAK",
    expiry: datetime | None = None,
) -> SimpleNamespace:
    """Build a stand-in Credentials object covering only the attributes
    accounts.py reads. Uses SimpleNamespace so pickling round-trips cleanly
    and we don't need google-auth installed in the test env."""
    return SimpleNamespace(
        scopes=scopes,
        expired=expired,
        refresh_token=refresh_token,
        token=token,
        client_secret=client_secret,
        expiry=expiry,
    )


def _write_token(tokens_dir: Path, name: str, creds) -> None:
    tokens_dir.mkdir(parents=True, exist_ok=True)
    with open(tokens_dir / f"{name}.pickle", "wb") as f:
        pickle.dump(creds, f)


# ── Discovery ─────────────────────────────────────────────────────────────────


def test_discover_empty_directory(isolated_tokens):
    assert accounts._discover_account_names() == []


def test_discover_lists_all_pickles_sorted(isolated_tokens):
    tokens_dir, _ = isolated_tokens
    _write_token(tokens_dir, "personal", _fake_creds(google_auth.SCOPES))
    _write_token(tokens_dir, "primary", _fake_creds(google_auth.SCOPES))
    _write_token(tokens_dir, "homer", _fake_creds(google_auth.SCOPES))

    assert accounts._discover_account_names() == ["homer", "personal", "primary"]


def test_discover_legacy_token_counts_as_primary(isolated_tokens):
    _, legacy = isolated_tokens
    legacy.write_bytes(b"fake-legacy-pickle")

    assert accounts._discover_account_names() == ["primary"]


def test_discover_legacy_does_not_double_count_with_canonical(isolated_tokens):
    tokens_dir, legacy = isolated_tokens
    legacy.write_bytes(b"fake-legacy-pickle")
    _write_token(tokens_dir, "primary", _fake_creds(google_auth.SCOPES))

    assert accounts._discover_account_names() == ["primary"]


# ── Metadata shape ────────────────────────────────────────────────────────────


def test_metadata_valid_token_with_full_scopes(isolated_tokens):
    tokens_dir, _ = isolated_tokens
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    _write_token(
        tokens_dir,
        "primary",
        _fake_creds(google_auth.SCOPES, expired=False, expiry=expiry),
    )

    record = accounts._account_metadata("primary")
    assert record["name"] == "primary"
    assert record["linked"] is True
    assert record["valid"] is True
    assert record["expired"] is False
    assert record["scopes_count"] == len(google_auth.SCOPES)
    assert record["missing_scopes"] == []
    assert "expiry" in record


def test_metadata_expired_but_refreshable_is_valid(isolated_tokens):
    tokens_dir, _ = isolated_tokens
    _write_token(
        tokens_dir,
        "personal",
        _fake_creds(google_auth.SCOPES, expired=True, refresh_token="rt-secret"),
    )

    record = accounts._account_metadata("personal")
    assert record["expired"] is True
    assert record["valid"] is True  # next API call auto-refreshes
    assert "reason" not in record


def test_metadata_expired_no_refresh_token_is_invalid(isolated_tokens):
    tokens_dir, _ = isolated_tokens
    _write_token(
        tokens_dir,
        "personal",
        _fake_creds(google_auth.SCOPES, expired=True, refresh_token=None),
    )

    record = accounts._account_metadata("personal")
    assert record["expired"] is True
    assert record["valid"] is False
    assert "re-link" in record["reason"].lower()


def test_metadata_unreadable_pickle_reports_broken_not_raises(isolated_tokens):
    tokens_dir, _ = isolated_tokens
    tokens_dir.mkdir(parents=True)
    (tokens_dir / "kemi.pickle").write_bytes(b"this is not a valid pickle")

    record = accounts._account_metadata("kemi")
    assert record["name"] == "kemi"
    assert record["linked"] is True
    assert record["valid"] is False
    assert "unreadable" in record["reason"].lower()


def test_metadata_missing_scopes_reported(isolated_tokens):
    tokens_dir, _ = isolated_tokens
    partial_scopes = google_auth.SCOPES[:2]
    _write_token(tokens_dir, "primary", _fake_creds(partial_scopes))

    record = accounts._account_metadata("primary")
    assert record["scopes"] == partial_scopes
    assert record["scopes_count"] == 2
    assert len(record["missing_scopes"]) == len(google_auth.SCOPES) - 2


# ── Secret containment (the critical invariant) ───────────────────────────────


@pytest.mark.parametrize("expired,refresh_token", [
    (False, "rt-secret-DO-NOT-LEAK"),
    (True, "rt-secret-DO-NOT-LEAK"),
    (True, None),
])
def test_no_secret_material_in_metadata(isolated_tokens, expired, refresh_token):
    """Across every code path (valid, expired-refreshable, expired-dead),
    the JSON output must not include refresh_token, access_token, or
    client_secret bytes."""
    tokens_dir, _ = isolated_tokens
    _write_token(
        tokens_dir,
        "primary",
        _fake_creds(
            google_auth.SCOPES,
            expired=expired,
            refresh_token=refresh_token,
            token="at-secret-DO-NOT-LEAK",
            client_secret="cs-secret-DO-NOT-LEAK",
        ),
    )

    record = accounts._account_metadata("primary")
    blob = json.dumps(record)
    assert "DO-NOT-LEAK" not in blob, f"Secret material leaked into output: {blob}"
    # Also verify key names don't appear (defense in depth — even if the
    # value were redacted, having the field present would tempt callers).
    for forbidden in ("refresh_token", "access_token", "client_secret", "token"):
        assert forbidden not in record, f"Forbidden field '{forbidden}' in record"


def test_no_secret_material_in_unreadable_pickle_path(isolated_tokens):
    """The error path for malformed pickles must not leak file bytes."""
    tokens_dir, _ = isolated_tokens
    tokens_dir.mkdir(parents=True)
    (tokens_dir / "kemi.pickle").write_bytes(b"DO-NOT-LEAK-this-is-pickle-content")

    record = accounts._account_metadata("kemi")
    blob = json.dumps(record)
    assert "DO-NOT-LEAK" not in blob, f"Pickle bytes leaked into error path: {blob}"


# ── CLI plumbing ──────────────────────────────────────────────────────────────


def test_cli_list_emits_json_array(isolated_tokens, capsys):
    tokens_dir, _ = isolated_tokens
    _write_token(tokens_dir, "primary", _fake_creds(google_auth.SCOPES))

    with patch.object(sys, "argv", ["accounts.py", "--list"]):
        rc = accounts.main()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["name"] == "primary"


def test_cli_list_empty_emits_empty_array(isolated_tokens, capsys):
    with patch.object(sys, "argv", ["accounts.py", "--list"]):
        rc = accounts.main()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == []


def test_cli_show_unknown_account_returns_error(isolated_tokens, capsys):
    with patch.object(sys, "argv", ["accounts.py", "--show", "nonexistent"]):
        rc = accounts.main()

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload


def test_cli_show_known_account_returns_object(isolated_tokens, capsys):
    tokens_dir, _ = isolated_tokens
    _write_token(tokens_dir, "personal", _fake_creds(google_auth.SCOPES))

    with patch.object(sys, "argv", ["accounts.py", "--show", "personal"]):
        rc = accounts.main()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "personal"
    assert payload["valid"] is True

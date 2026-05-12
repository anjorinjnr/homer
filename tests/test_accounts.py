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
    """Build a stand-in Credentials object covering the FULL attribute
    surface of a real google.oauth2.credentials.Credentials, with
    DO-NOT-LEAK sentinel values on every sensitive field. The leak tests
    grep the JSON output for those sentinels; if the discovery tool
    ever starts emitting one of these fields (intentionally or via a
    refactor that dumps creds.__dict__), the assertion fires."""
    return SimpleNamespace(
        # Fields accounts.py legitimately reads:
        scopes=scopes,
        expired=expired,
        refresh_token=refresh_token,
        expiry=expiry,
        # Sensitive fields that real Credentials objects carry. None of
        # these should ever appear in the JSON output. If a future refactor
        # adds a new "helpful" field, expand both the sentinel set and the
        # _ALLOWED_KEYS allowlist in tools/accounts.py.
        token=token,
        client_secret=client_secret,
        client_id="cid-secret-DO-NOT-LEAK",
        id_token="idt-secret-DO-NOT-LEAK",
        rapt_token="rapt-secret-DO-NOT-LEAK",
        quota_project_id="qp-secret-DO-NOT-LEAK",
        granted_scopes=["https://DO-NOT-LEAK.example/scope"],
        account="DO-NOT-LEAK-account",
        token_uri="https://oauth2.googleapis.com/token",
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
    (tokens_dir / "maya.pickle").write_bytes(b"this is not a valid pickle")

    record = accounts._account_metadata("maya")
    assert record["name"] == "maya"
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
    (tokens_dir / "maya.pickle").write_bytes(b"DO-NOT-LEAK-this-is-pickle-content")

    record = accounts._account_metadata("maya")
    blob = json.dumps(record)
    assert "DO-NOT-LEAK" not in blob, f"Pickle bytes leaked into error path: {blob}"


def test_metadata_keys_are_strictly_allowlisted(isolated_tokens):
    """Defense-in-depth: even if a future refactor stamps an extra field
    onto the record (via the pickle's __dict__ or whatever), the final
    output must only contain fields explicitly in _ALLOWED_KEYS. This
    test fails the moment someone introduces a new top-level key without
    expanding the allowlist."""
    tokens_dir, _ = isolated_tokens
    _write_token(tokens_dir, "primary", _fake_creds(google_auth.SCOPES))

    record = accounts._account_metadata("primary")

    extra_keys = set(record.keys()) - accounts._ALLOWED_KEYS
    assert extra_keys == set(), (
        f"Unexpected keys in metadata output: {extra_keys}. "
        f"If this is intentional, add to _ALLOWED_KEYS in accounts.py."
    )


def test_metadata_rejects_unreadable_pickle_via_broad_except(isolated_tokens):
    """The broadened exception catch must cover non-pickle errors too —
    a pickle whose class can't be imported in this Python should
    fail-soft, not crash the whole discovery. The previous narrower
    except (UnpicklingError/EOFError/OSError/AttributeError) would have
    let ModuleNotFoundError propagate.

    Construct a hand-rolled pickle stream that references a module name
    that doesn't exist (`__nope__.bad`), forcing find_class to raise
    ModuleNotFoundError at unpickle time."""
    tokens_dir, _ = isolated_tokens
    tokens_dir.mkdir(parents=True)
    # Pickle protocol 2 STACK_GLOBAL: `\x80\x02c<module>\n<name>\n` is the
    # canonical "build instance of <module>.<name>" prologue. We use it
    # with a nonexistent module so the import fails.
    bad = b"\x80\x02c__nope__\nbad\nq\x00.\x80\x02."
    (tokens_dir / "broken.pickle").write_bytes(bad)

    record = accounts._account_metadata("broken")
    assert record["valid"] is False
    assert "unreadable" in record["reason"].lower()


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

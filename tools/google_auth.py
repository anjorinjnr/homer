#!/usr/bin/env python3
"""
google_auth.py — Google OAuth setup for Homer (multi-account).

Grants Homer access to Gmail (read+send), Google Calendar, Google Drive,
and Google Sheets.  Each account gets its own token under secrets/tokens/.

Built-in accounts:
    homer    — Homer's own email identity (set HOMER_EMAIL_ADDRESS)
    primary  — the household's main email (e.g. household@example.com)

Ad-hoc accounts can be registered with any name the user chooses.

Usage:
    python tools/google_auth.py                       # auth primary (default)
    python tools/google_auth.py --account homer       # auth Homer's own email
    python tools/google_auth.py --account maya        # auth an ad-hoc account

Requires:
    secrets/google_credentials.json  — OAuth client credentials from GCP
    (download from GCP Console → APIs & Services → Credentials → OAuth 2.0 Client)
"""

import argparse
import json
import pickle
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
CREDENTIALS_FILE = REPO_ROOT / "secrets" / "google_credentials.json"
TOKENS_DIR = REPO_ROOT / "secrets" / "tokens"
LEGACY_TOKEN = REPO_ROOT / "secrets" / "google_token.pickle"

DEFAULT_ACCOUNT = "primary"

SERVICE_SPECS = {
    "gmail": ("gmail", "v1"),
    "drive": ("drive", "v3"),
    "calendar": ("calendar", "v3"),
    "sheets": ("sheets", "v4"),
}

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://mail.google.com/",  # Full mail access — required for IMAP/SMTP OAuth2 (XOAUTH2)
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/contacts.readonly",
]


def get_token_path(account: str = DEFAULT_ACCOUNT) -> Path:
    """Return the token pickle path for the given account."""
    return TOKENS_DIR / f"{account}.pickle"


def resolve_token_path(account: str = DEFAULT_ACCOUNT) -> Path | None:
    """Return the on-disk path for ``account``'s token, or None if no
    token exists. Honors the legacy fallback for ``primary`` so callers
    don't have to re-implement that branch.
    """
    canonical = get_token_path(account)
    if canonical.exists():
        return canonical
    if account == DEFAULT_ACCOUNT and LEGACY_TOKEN.exists():
        return LEGACY_TOKEN
    return None


def has_google_token(account: str = DEFAULT_ACCOUNT) -> bool:
    """Check whether ``account`` is plausibly connected to Google.

    Returns True iff a token pickle exists at the canonical path (or the
    legacy fallback for ``primary``). Does NOT validate or refresh — a
    stale token still returns True; the caller's first API call is what
    proves the token is live. The point of this helper is the early
    short-circuit: skip subprocess spawns + HTTP retries for tenants
    that obviously haven't connected Google yet.
    """
    return resolve_token_path(account) is not None


def _migrate_legacy_token(account: str, token_path: Path) -> bool:
    """Copy legacy single-token file to new per-account path if needed. Returns True if migrated."""
    if account == "primary" and not token_path.exists() and LEGACY_TOKEN.exists():
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_TOKEN, token_path)
        return True
    return False


def load_google_credentials(account: str = DEFAULT_ACCOUNT):
    """Load and auto-refresh OAuth credentials for a Google account.

    Handles legacy migration from secrets/google_token.pickle for the
    primary account.  Returns a google.oauth2.credentials.Credentials
    object ready to use with googleapiclient.discovery.build() for any
    Google API (Gmail, Drive, Calendar, Sheets, Docs).
    """
    from google.auth.transport.requests import Request

    token_path = get_token_path(account)
    _migrate_legacy_token(account, token_path)

    if not token_path.exists():
        raise FileNotFoundError(
            f"Token not found for account '{account}' at {token_path}. "
            f"Run: python tools/google_auth.py --account {account}"
        )

    with open(token_path, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return creds


# Tests and older callers import this name.
load_gmail_credentials = load_google_credentials


def require_scopes(creds, account: str, *required: str) -> None:
    """Raise PermissionError if creds lack any of the required OAuth scopes.

    Tokens issued before a new scope was added to SCOPES will be missing it.
    Callers should run this before invoking any API that requires the scope, so
    Homer surfaces an actionable "re-link your Google account" message instead
    of an opaque 403 from deeper in the stack.
    """
    granted = set(creds.scopes or [])
    missing = [s for s in required if s not in granted]
    if missing:
        readable = ", ".join(s.rsplit("/", 1)[-1] for s in missing)
        raise PermissionError(
            f"Account '{account}' is missing OAuth scope(s): {readable}. "
            f"Ask the user to re-link their Google account to grant access."
        )


def _build_service(kind: str, account: str):
    from googleapiclient.discovery import build

    api, version = SERVICE_SPECS[kind]
    return build(api, version, credentials=load_google_credentials(account))


def build_service_or_exit(kind: str, account: str = DEFAULT_ACCOUNT, *, json_errors: bool = True):
    """Build a Google API service, printing a friendly error and exiting on
    missing deps or missing token.

    json_errors=True  → print `{"error": "..."}` (Homer tool-output convention)
    json_errors=False → print `ERROR: ...` (for tools with plain-text output)
    """
    def _fail(msg: str) -> None:
        print(json.dumps({"error": msg}) if json_errors else f"ERROR: {msg}")
        sys.exit(1)

    try:
        return _build_service(kind, account)
    except ImportError:
        _fail("Missing deps. Run: pip install google-auth google-api-python-client")
    except FileNotFoundError as e:
        _fail(str(e))


def get_gmail_service(account: str = DEFAULT_ACCOUNT):
    return _build_service("gmail", account)


def get_drive_service(account: str = DEFAULT_ACCOUNT):
    return _build_service("drive", account)


def get_calendar_service(account: str = DEFAULT_ACCOUNT):
    return _build_service("calendar", account)


def get_sheets_service(account: str = DEFAULT_ACCOUNT):
    return _build_service("sheets", account)


def authenticate(account: str = DEFAULT_ACCOUNT) -> None:
    try:
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependencies. Run:")
        print("  pip install google-auth google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    if not CREDENTIALS_FILE.exists():
        print(f"ERROR: {CREDENTIALS_FILE} not found.")
        print("Download OAuth 2.0 client credentials from:")
        print("  GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs")
        print(f"Save as: {CREDENTIALS_FILE}")
        sys.exit(1)

    token_path = get_token_path(account)
    creds = None

    if _migrate_legacy_token(account, token_path):
        print(f"Migrated legacy token → {token_path}")

    # Load existing token if present
    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        print("Refreshing existing token...")
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"Refresh failed ({e}), starting fresh OAuth flow...")
            creds = None

    if not creds or not creds.valid:
        print(f"Starting OAuth flow for account '{account}'...")
        print("A browser window will open — sign in with the correct Google account")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=8080)

    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    with open(token_path, "wb") as f:
        pickle.dump(creds, f)
    token_path.chmod(0o600)

    print(f"✓ Token saved to {token_path}")
    print(f"  Account: {account}")
    print(f"  Scopes: {', '.join(creds.scopes or [])}")
    print(f"  Valid: {creds.valid}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google OAuth setup for Homer")
    parser.add_argument(
        "--account",
        default=DEFAULT_ACCOUNT,
        help=f"Account name (default: {DEFAULT_ACCOUNT}). Token saved to secrets/tokens/<account>.pickle",
    )
    args = parser.parse_args()
    authenticate(args.account)

#!/usr/bin/env python3
"""
accounts.py — List linked Google accounts (sanitized metadata only).

Discovery primitive for Homer's multi-account features. Returns the set
of accounts that have a token on disk, with just enough metadata for
the agent to decide what to fan out over (name, scope count, validity).

Token material (refresh_token, access_token, client_secret) NEVER leaves
this process — the pickle is loaded in-process via load_google_credentials
and only sanitized fields are printed.

Usage:
    python tools/accounts.py --list            # JSON array of all accounts
    python tools/accounts.py --show <name>     # JSON object for one account

Exit codes:
    0 — success
    1 — usage error or no tokens directory
"""

import argparse
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from google_auth import LEGACY_TOKEN, SCOPES, TOKENS_DIR, resolve_token_path

# Explicit allowlist of metadata fields. The pickle stays a black box to
# this module; we only ever emit fields we've named here. A future refactor
# that tries to e.g. include `creds.__dict__` will produce keys outside the
# allowlist and get filtered, preventing accidental leak of refresh_token /
# access_token / id_token / rapt_token / client_secret / quota_project_id.
_ALLOWED_KEYS = frozenset({
    "name",
    "linked",
    "valid",
    "expired",
    "expiry",
    "scopes",
    "scopes_count",
    "missing_scopes",
    "reason",
})


def list_valid_accounts() -> list[str]:
    """Public discovery for callers that need the names of usable accounts.

    Returns linked accounts whose token is valid (not expired, or expired
    but refreshable). Stale/broken accounts are dropped so a single bad
    token can't break a downstream fan-out (briefing, digest, etc.).
    """
    return [n for n in _discover_account_names() if _account_metadata(n).get("valid")]


def _discover_account_names() -> list[str]:
    """Return the set of account names that have a token on disk.

    Includes legacy primary fallback at secrets/google_token.pickle.
    Sorted for stable output across runs.
    """
    names: set[str] = set()
    if TOKENS_DIR.exists():
        for path in TOKENS_DIR.glob("*.pickle"):
            names.add(path.stem)
    if LEGACY_TOKEN.exists():
        names.add("primary")
    return sorted(names)


def _account_metadata(name: str) -> dict:
    """Load one account's pickle in-process and emit sanitized metadata.

    Returns a dict with only non-sensitive fields — token bytes never
    leave this function. On unreadable or malformed pickles, returns a
    record with linked=true and a reason, so the caller still sees the
    account exists even if its token is corrupt.
    """
    token_path = resolve_token_path(name) or (TOKENS_DIR / f"{name}.pickle")

    record: dict = {"name": name, "linked": True}

    # Intentionally raw pickle.load — load_google_credentials() would
    # refresh-and-rewrite the token file, which violates the discovery
    # tool's read-only contract. Broad except: discovery should fail-soft
    # on any unpickle failure (UnpicklingError, EOFError, OSError,
    # ImportError, ModuleNotFoundError, ValueError, TypeError, AttributeError —
    # all reachable for truncated or version-skewed pickles). A bad pickle
    # for one account must not crash the whole discovery.
    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    except Exception as e:
        record["valid"] = False
        record["reason"] = f"Token file unreadable: {type(e).__name__}"
        return {k: v for k, v in record.items() if k in _ALLOWED_KEYS}

    granted = list(getattr(creds, "scopes", None) or [])
    record["scopes"] = granted
    record["scopes_count"] = len(granted)
    record["missing_scopes"] = [s for s in SCOPES if s not in granted]

    expiry = getattr(creds, "expiry", None)
    if expiry is not None:
        record["expiry"] = expiry.isoformat()

    expired = bool(getattr(creds, "expired", False))
    refreshable = bool(getattr(creds, "refresh_token", None))
    record["expired"] = expired
    # A token is "valid" if it's not expired, OR it's expired but has a
    # refresh token (next API call will auto-refresh). Anything else
    # means the user needs to re-link.
    record["valid"] = (not expired) or refreshable
    if expired and not refreshable:
        record["reason"] = "Token expired and no refresh_token — re-link required"

    return {k: v for k, v in record.items() if k in _ALLOWED_KEYS}


def cmd_list() -> int:
    names = _discover_account_names()
    print(json.dumps([_account_metadata(n) for n in names], indent=2))
    return 0


def cmd_show(name: str) -> int:
    # resolve_token_path is the same predicate _discover_account_names uses
    # to enumerate, but it costs one stat per name vs a glob over the whole
    # tokens dir. Skip the directory scan and check the canonical path
    # directly.
    if resolve_token_path(name) is None:
        print(json.dumps({"error": f"Account '{name}' is not linked"}))
        return 1
    print(json.dumps(_account_metadata(name), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List linked Google accounts (sanitized metadata only).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List all linked accounts as a JSON array")
    group.add_argument("--show", metavar="NAME", help="Show metadata for one account as a JSON object")
    args = parser.parse_args()

    if args.list:
        return cmd_list()
    return cmd_show(args.show)


if __name__ == "__main__":
    sys.exit(main())

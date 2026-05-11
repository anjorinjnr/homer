"""short_links.py — Generic portal URL shortener.

Any tool that emits a long portal URL can call `shorten()` to mint a
`{PORTAL_BASE_URL}/s/{code}` form. The portal resolves `/s/{code}` by
looking up the row in the Supabase `short_links` table and 302'ing to
`target_url`. Codes are scoped to a household so the same long URL
shortened for two different tenants returns two different codes; the
table itself is a single global namespace so the portal can resolve
`/s/{code}` without knowing which household it belongs to.

Idempotent: re-shortening the same `(household_id, target_url)` returns
the existing code rather than minting a new one. Soft-fail: if Supabase
is unreachable, callers can decide to surface the long URL only.

Schema (Supabase, owned by portal repo):
    short_links(
        code         text primary key,
        household_id text not null,
        target_url   text not null,
        kind         text,
        created_at   timestamptz not null default now(),
        unique (household_id, target_url)
    )
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOOLS_DIR = str(REPO_ROOT / "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# `history_store` pulls in `requests` (transitive Supabase dep). Defer the
# import so callers who stub out `_client()` (tests, dry-run paths) don't
# need that dep available.

DEFAULT_BASE_URL = ""  # set PORTAL_BASE_URL in env
TABLE = "short_links"
CODE_LENGTH = 8
# Unambiguous alphabet — no 0/O/1/I/L. ~32^8 = 1.1e12 codes.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_MAX_INSERT_RETRIES = 5


def _portal_base_url() -> str:
    return os.environ.get("PORTAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _random_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def build_short_url(code: str) -> str:
    return f"{_portal_base_url()}/s/{code}"


def _client():
    """Return the Supabase client. Indirected so tests can monkeypatch."""
    import history_store as _hs
    return _hs.client()


def _existing_code(household_id: str, target_url: str) -> Optional[str]:
    rows = _client().select(
        TABLE,
        filters={
            "household_id": f"eq.{household_id}",
            "target_url": f"eq.{target_url}",
        },
        columns="code",
        limit=1,
    )
    return rows[0]["code"] if rows else None


def shorten(
    target_url: str,
    *,
    household_id: str,
    kind: Optional[str] = None,
) -> str:
    """Return a short URL for `target_url`.

    Idempotent on `(household_id, target_url)`. Raises on Supabase failure
    so callers can fall back to the long URL.
    """
    if not target_url:
        raise ValueError("target_url is required")
    if not household_id:
        raise ValueError("household_id is required")

    existing = _existing_code(household_id, target_url)
    if existing:
        return build_short_url(existing)

    # Retry on the rare PK collision; if our random space ever fills up
    # this turns into a real error rather than an infinite loop.
    last_err: Exception | None = None
    for _ in range(_MAX_INSERT_RETRIES):
        code = _random_code()
        row = {
            "code": code,
            "household_id": household_id,
            "target_url": target_url,
        }
        if kind:
            row["kind"] = kind
        try:
            _client().insert(TABLE, row)
            return build_short_url(code)
        except Exception as e:
            last_err = e
            # Another writer may have inserted the same (household_id, target_url)
            # between our SELECT and INSERT — re-check before retrying with a new code.
            existing = _existing_code(household_id, target_url)
            if existing:
                return build_short_url(existing)
            continue

    raise RuntimeError(
        f"failed to mint short link after {_MAX_INSERT_RETRIES} attempts"
    ) from last_err


def shorten_or_none(
    target_url: str,
    *,
    household_id: str,
    kind: Optional[str] = None,
) -> Optional[str]:
    """Soft-fail variant: returns None on any error so callers can degrade."""
    try:
        return shorten(target_url, household_id=household_id, kind=kind)
    except Exception as e:
        print(f"short_links: degrading to long URL ({e})", file=sys.stderr)
        return None

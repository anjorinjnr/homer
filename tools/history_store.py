#!/usr/bin/env python3
"""history_store.py — Supabase REST wrapper for family history data.

All reads/writes use the service-role key (bypasses RLS) so the agent
container can write artifacts and fragments on behalf of contributors.

Environment variables required:
  SUPABASE_URL          https://<project>.supabase.co
  SUPABASE_SERVICE_KEY  service_role JWT

This module is imported by other history_*.py tools; it is NOT an exec tool.
"""

import json
import os
import secrets
import string
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

# Domain for synthesized auth_email placeholders on phone-only contributors.
# Must match the portal's HISTORY_INVITE_EMAIL_DOMAIN so portal-side redeem
# (`backend/services/history_service.py:redeem_invite`) reads back what we wrote.
_INVITE_EMAIL_DOMAIN_DEFAULT = "invite.history.example.com"  # override via HISTORY_INVITE_EMAIL_DOMAIN
_INVITE_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


def _invite_email_domain() -> str:
    return os.environ.get("HISTORY_INVITE_EMAIL_DOMAIN", _INVITE_EMAIL_DOMAIN_DEFAULT).strip() \
        or _INVITE_EMAIL_DOMAIN_DEFAULT


def _gen_invite_token(n: int = 32) -> str:
    return "".join(secrets.choice(_INVITE_TOKEN_ALPHABET) for _ in range(n))

# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return str(uuid.uuid4())


def household_id() -> str:
    """Read HOMER_HOUSEHOLD_ID from env; exit with JSON error if missing."""
    hid = os.environ.get("HOMER_HOUSEHOLD_ID", "").strip()
    if not hid:
        print(json.dumps({"error": "HOMER_HOUSEHOLD_ID is not set"}))
        sys.exit(1)
    return hid


def seconds_since(ts_str: str | None) -> float:
    """Seconds elapsed since an ISO-8601 UTC timestamp string; inf for None/invalid."""
    if not ts_str:
        return float("inf")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


class SupabaseClient:
    """Thin Supabase REST (PostgREST) client using service-role key."""

    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
                "Add them to secrets/.env."
            )
        self._base = f"{url}/rest/v1"
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def select(
        self,
        table: str,
        filters: Optional[dict[str, str]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        columns: str = "*",
    ) -> list[dict]:
        params: dict[str, Any] = {"select": columns}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)
        if offset:
            params["offset"] = str(offset)
        r = requests.get(
            f"{self._base}/{table}",
            headers=self._headers,
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, data: dict) -> dict:
        r = requests.post(
            f"{self._base}/{table}",
            headers={**self._headers, "Prefer": "return=representation"},
            json=data,
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else data

    def update(self, table: str, filters: dict[str, str], data: dict) -> list[dict]:
        r = requests.patch(
            f"{self._base}/{table}",
            headers={**self._headers, "Prefer": "return=representation"},
            params=filters,
            json=data,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def upsert(self, table: str, data: dict, on_conflict: str = "id") -> dict:
        r = requests.post(
            f"{self._base}/{table}",
            headers={
                **self._headers,
                "Prefer": f"return=representation,resolution=merge-duplicates",
            },
            params={"on_conflict": on_conflict},
            json=data,
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else data


# ---------------------------------------------------------------------------
# Module-level client (lazy init so import doesn't fail without env vars)
# ---------------------------------------------------------------------------

_client: Optional[SupabaseClient] = None


def client() -> SupabaseClient:
    global _client
    if _client is None:
        _client = SupabaseClient()
    return _client


# ---------------------------------------------------------------------------
# Contributors
# ---------------------------------------------------------------------------

def create_contributor(
    *,
    household_id: str,
    display_name: str,
    role: str = "contributor",
    relationship: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    invited_by: Optional[str] = None,
) -> dict:
    # invite_token + auth_email are populated for every new row so a contributor
    # invited on WhatsApp can later redeem `/invite/<token>` to claim a web
    # session — the portal's redeem flow stamps `auth_user_id` back on the same
    # row, preserving a single contributor identity across channels.
    row_id = _new_id()
    normalized_email = email.strip().lower() if email else None
    auth_email = normalized_email or f"inv-{row_id}@{_invite_email_domain()}"
    row = {
        "id": row_id,
        "household_id": household_id,
        "role": role,
        "display_name": display_name,
        "status": "pending",
        "auth_email": auth_email,
        "invite_token": _gen_invite_token(),
        "created_at": _now_utc(),
    }
    if relationship:
        row["relationship"] = relationship
    if phone:
        row["phone"] = normalise_phone(phone)[0]
    if normalized_email:
        row["email"] = normalized_email
    if invited_by:
        row["invited_by"] = invited_by
    return client().insert("hist_contributors", row)


def activate_contributor(contributor_id: str) -> dict:
    rows = client().update(
        "hist_contributors",
        {"id": f"eq.{contributor_id}"},
        {"status": "active", "verified_at": _now_utc()},
    )
    return rows[0] if rows else {}


def get_contributor_by_phone(household_id: str, phone: str) -> Optional[dict]:
    normalized = normalise_phone(phone)[0]
    rows = client().select(
        "hist_contributors",
        filters={
            "household_id": f"eq.{household_id}",
            "phone": f"eq.{normalized}",
        },
    )
    return rows[0] if rows else None


def get_contributor_by_id(contributor_id: str) -> Optional[dict]:
    rows = client().select(
        "hist_contributors",
        filters={"id": f"eq.{contributor_id}"},
    )
    return rows[0] if rows else None


def list_contributors(
    household_id: str,
    status: Optional[str] = None,
) -> list[dict]:
    filters: dict[str, str] = {"household_id": f"eq.{household_id}"}
    if status:
        filters["status"] = f"eq.{status}"
    return client().select("hist_contributors", filters=filters, order="created_at")


def archive_contributor(contributor_id: str) -> dict:
    rows = client().update(
        "hist_contributors",
        {"id": f"eq.{contributor_id}"},
        {"status": "archived"},
    )
    return rows[0] if rows else {}


def normalise_phone(raw: str) -> tuple[str, str | None]:
    """Normalise a phone number to E.164-ish digits, with a curator warning.

    Canonical form is digits-only (no leading ``+``) so it matches the
    ``allow_from`` keys used by the WhatsApp bridge.

    Rules (digits only, after stripping ``+``/spaces/dashes/parens):
    - 11 digits starting with ``1`` → already E.164, keep as-is.
    - 10 digits                    → assume US/Canada, prepend ``1``.
    - anything else                → return as-is with a warning.

    The warning string is intended to be surfaced to the curator so they
    can confirm the country code; storage callers can discard it.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits, None
    if len(digits) == 10:
        return "1" + digits, (
            f"Phone '{raw}' looked like a 10-digit US number — stored as 1{digits}. "
            "If this is a non-US number, archive and re-invite with the full country code."
        )
    return digits, (
        f"Phone '{raw}' has {len(digits)} digits and an unrecognised format. "
        "Make sure to include the country code (e.g. 14155551234 for US, 447911123456 for UK)."
    )


# ---------------------------------------------------------------------------
# Artifacts (Layer 1 — immutable)
# ---------------------------------------------------------------------------

def insert_artifact(
    *,
    household_id: str,
    contributor_id: str,
    channel: str,
    kind: str,
    body: Optional[str] = None,
    caption: Optional[str] = None,
    storage_path: Optional[str] = None,
    captured_at: Optional[str] = None,
    source_metadata: Optional[dict] = None,
) -> dict:
    row = {
        "id": _new_id(),
        "household_id": household_id,
        "contributor_id": contributor_id,
        "channel": channel,
        "kind": kind,
        "source_metadata": source_metadata or {},
        "captured_at": captured_at or _now_utc(),
        "created_at": _now_utc(),
    }
    if body is not None:
        row["body"] = body
    if caption is not None:
        row["caption"] = caption
    if storage_path is not None:
        row["storage_path"] = storage_path
    return client().insert("hist_artifacts", row)


def get_artifact(artifact_id: str) -> Optional[dict]:
    rows = client().select("hist_artifacts", filters={"id": f"eq.{artifact_id}"})
    return rows[0] if rows else None


def update_artifact_body(artifact_id: str, body: str) -> dict:
    """Update the body (e.g. after async Whisper transcription). Body is still layer-1 raw."""
    rows = client().update(
        "hist_artifacts",
        {"id": f"eq.{artifact_id}"},
        {"body": body},
    )
    return rows[0] if rows else {}


def list_artifacts(
    household_id: str,
    contributor_id: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    filters: dict[str, str] = {"household_id": f"eq.{household_id}"}
    if contributor_id:
        filters["contributor_id"] = f"eq.{contributor_id}"
    if kind:
        filters["kind"] = f"eq.{kind}"
    return client().select(
        "hist_artifacts",
        filters=filters,
        order="captured_at.desc",
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Fragments (Layer 2)
# ---------------------------------------------------------------------------

def insert_fragment(
    *,
    household_id: str,
    artifact_id: str,
    kind: str,
    payload: dict,
    confidence: float = 0.8,
    attribution: str = "paraphrased",
    entity_id: Optional[str] = None,
    contributor_id: Optional[str] = None,
) -> dict:
    row = {
        "id": _new_id(),
        "household_id": household_id,
        "artifact_id": artifact_id,
        "kind": kind,
        "payload": payload,
        "confidence": confidence,
        "attribution": attribution,
        "status": "pending",
        "created_at": _now_utc(),
    }
    if entity_id:
        row["entity_id"] = entity_id
    # NOTE: hist_fragments has no contributor_id column in the prod schema —
    # the link runs through hist_artifacts.contributor_id. Keep the kwarg in
    # the signature for callers' convenience but don't write it. The
    # contributor_id parameter is unused on insert; queries that need
    # per-contributor filtering go through list_recent_fragments which joins
    # via hist_artifacts.
    _ = contributor_id  # explicitly unused
    return client().insert("hist_fragments", row)


def list_recent_fragments(
    household_id: str,
    contributor_id: Optional[str] = None,
    limit: int = 30,
    offset: int = 0,
) -> list[dict]:
    """Return recent fragments, optionally filtered by contributor.

    Per-contributor filtering joins through hist_artifacts: fetch the
    contributor's artifact ids first, then fragments whose artifact_id is
    in that set. hist_fragments has no contributor_id column in the prod
    schema, so a direct contributor_id filter would 400.
    """
    if contributor_id:
        artifacts = client().select(
            "hist_artifacts",
            filters={
                "household_id": f"eq.{household_id}",
                "contributor_id": f"eq.{contributor_id}",
            },
            columns="id",
        )
        artifact_ids = [a["id"] for a in artifacts]
        if not artifact_ids:
            return []
        filters: dict[str, str] = {
            "household_id": f"eq.{household_id}",
            "artifact_id": "in.(" + ",".join(artifact_ids) + ")",
        }
    else:
        filters = {"household_id": f"eq.{household_id}"}
    return client().select(
        "hist_fragments",
        filters=filters,
        order="created_at.desc",
        limit=limit,
        offset=offset or None,
    )


def fetch_all_fragments(
    household_id: str,
    contributor_id: Optional[str] = None,
    page_size: int = 500,
) -> list[dict]:
    """Page through all fragments for a household/contributor."""
    all_rows: list[dict] = []
    offset = 0
    while True:
        page = list_recent_fragments(household_id, contributor_id=contributor_id,
                                     limit=page_size, offset=offset)
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_rows


def _set_fragment_status(fragment_id: str, status: str) -> dict:
    rows = client().update("hist_fragments", {"id": f"eq.{fragment_id}"}, {"status": status})
    return rows[0] if rows else {}


def confirm_fragment(fragment_id: str) -> dict:
    return _set_fragment_status(fragment_id, "confirmed")


def reject_fragment(fragment_id: str) -> dict:
    return _set_fragment_status(fragment_id, "rejected")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def insert_entity(
    *,
    household_id: str,
    kind: str,
    canonical_name: str,
    aliases: Optional[list[str]] = None,
    attrs: Optional[dict] = None,
) -> dict:
    row = {
        "id": _new_id(),
        "household_id": household_id,
        "kind": kind,
        "canonical_name": canonical_name,
        "aliases": aliases or [],
        "attrs": attrs or {},
        "created_at": _now_utc(),
    }
    return client().insert("hist_entities", row)


def find_entities(
    household_id: str,
    name_fragment: str,
    kind: Optional[str] = None,
) -> list[dict]:
    filters: dict[str, str] = {
        "household_id": f"eq.{household_id}",
        "canonical_name": f"ilike.*{name_fragment}*",
    }
    if kind:
        filters["kind"] = f"eq.{kind}"
    return client().select("hist_entities", filters=filters)


def get_entity(entity_id: str) -> Optional[dict]:
    rows = client().select("hist_entities", filters={"id": f"eq.{entity_id}"})
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Open threads (historian state)
# ---------------------------------------------------------------------------

def insert_thread(
    *,
    household_id: str,
    contributor_id: str,
    prompt: str,
    context: Optional[dict] = None,
    priority: int = 5,
) -> dict:
    row = {
        "id": _new_id(),
        "household_id": household_id,
        "contributor_id": contributor_id,
        "prompt": prompt,
        "context": context or {},
        "priority": priority,
        "status": "open",
        "created_at": _now_utc(),
    }
    return client().insert("hist_open_threads", row)


def list_open_threads(
    household_id: str,
    contributor_id: str,
    limit: int = 5,
) -> list[dict]:
    return client().select(
        "hist_open_threads",
        filters={
            "household_id": f"eq.{household_id}",
            "contributor_id": f"eq.{contributor_id}",
            "status": "eq.open",
        },
        order="priority.desc,created_at.asc",
        limit=limit,
    )


def _set_thread_status(thread_id: str, status: str, extra: dict | None = None) -> dict:
    data: dict = {"status": status}
    if extra:
        data.update(extra)
    rows = client().update("hist_open_threads", {"id": f"eq.{thread_id}"}, data)
    return rows[0] if rows else {}


def mark_thread_asked(thread_id: str) -> dict:
    return _set_thread_status(thread_id, "asked", {"last_asked_at": _now_utc()})


def mark_thread_answered(thread_id: str) -> dict:
    return _set_thread_status(thread_id, "answered")


def mark_thread_abandoned(thread_id: str) -> dict:
    return _set_thread_status(thread_id, "abandoned")


# ---------------------------------------------------------------------------
# Era coverage
# ---------------------------------------------------------------------------

ALL_ERAS = [
    "childhood", "school", "young-adult", "marriage",
    "parenting", "career", "late-life", "extended-family",
]


def get_era_coverage(household_id: str, contributor_id: str) -> list[dict]:
    return client().select(
        "hist_era_coverage",
        filters={
            "household_id": f"eq.{household_id}",
            "contributor_id": f"eq.{contributor_id}",
        },
        order="richness_score.asc",
    )


def upsert_era_coverage(
    *,
    household_id: str,
    contributor_id: str,
    era_label: str,
    fragment_count: int,
    richness_score: float,
) -> dict:
    row = {
        "id": _new_id(),
        "household_id": household_id,
        "contributor_id": contributor_id,
        "era_label": era_label,
        "fragment_count": fragment_count,
        "richness_score": richness_score,
        "last_touched_at": _now_utc(),
    }
    return client().upsert("hist_era_coverage", row, on_conflict="household_id,contributor_id,era_label")


# ---------------------------------------------------------------------------
# Stories (Layer 3)
# ---------------------------------------------------------------------------

def insert_story(
    *,
    household_id: str,
    title: str,
    body_md: str,
    period_label: Optional[str] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    date_uncertainty: Optional[str] = None,
    primary_entity_id: Optional[str] = None,
    source_fragment_ids: Optional[list[str]] = None,
    visibility: str = "private",
    created_by: Optional[str] = None,
) -> dict:
    row = {
        "id": _new_id(),
        "household_id": household_id,
        "title": title,
        "body_md": body_md,
        "source_fragment_ids": source_fragment_ids or [],
        "visibility": visibility,
        "created_at": _now_utc(),
    }
    for key, val in [
        ("period_label", period_label),
        ("date_start", date_start),
        ("date_end", date_end),
        ("date_uncertainty", date_uncertainty),
        ("primary_entity_id", primary_entity_id),
        ("created_by", created_by),
    ]:
        if val is not None:
            row[key] = val
    return client().insert("hist_stories", row)


def list_stories(
    household_id: str,
    visibility: Optional[str] = None,
) -> list[dict]:
    filters: dict[str, str] = {"household_id": f"eq.{household_id}"}
    if visibility:
        filters["visibility"] = f"eq.{visibility}"
    return client().select("hist_stories", filters=filters, order="date_start.asc.nullslast")


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------

def _random_code(length: int = 8) -> str:
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous chars
    return "".join(secrets.choice(alphabet) for _ in range(length))


def upsert_share_link(household_id: str, expires_at: Optional[str] = None) -> dict:
    code = _random_code()
    row = {
        "household_id": household_id,
        "code": code,
        "viewer_log": [],
        "created_at": _now_utc(),
    }
    if expires_at:
        row["expires_at"] = expires_at
    return client().upsert("hist_share_links", row, on_conflict="household_id")


def get_share_link(household_id: str) -> Optional[dict]:
    rows = client().select("hist_share_links", filters={"household_id": f"eq.{household_id}"})
    return rows[0] if rows else None


def get_share_link_by_code(code: str) -> Optional[dict]:
    rows = client().select("hist_share_links", filters={"code": f"eq.{code.upper()}"})
    return rows[0] if rows else None


def revoke_share_link(household_id: str) -> None:
    """Revoke by setting a past expiry date."""
    from datetime import timedelta
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    client().update("hist_share_links", {"household_id": f"eq.{household_id}"}, {"expires_at": past})


def log_viewer(household_id: str, viewer_info: dict) -> None:
    """Append a viewer entry to viewer_log, capped at 100 entries (best-effort)."""
    try:
        existing = get_share_link(household_id)
        if not existing:
            return
        log = existing.get("viewer_log") or []
        log.append({**viewer_info, "viewed_at": _now_utc()})
        if len(log) > 100:
            log = log[-100:]
        client().update(
            "hist_share_links",
            {"household_id": f"eq.{household_id}"},
            {"viewer_log": log},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reentry preamble — quiet ack of what was extracted between sessions
# ---------------------------------------------------------------------------
#
# Mirrors `backend/historian/reentry.py` on the portal so a WhatsApp
# contributor's first message after an extraction run gets the same "I
# went back through our last chat — added N to your record" lead-in the
# portal historian uses. Render-only: never written to hist_chat_messages.
#
# Logic stays simple and read-only:
#   1. Find the contributor's last user turn timestamp.
#   2. Bail if too recent (gap < REENTRY_MIN_MINUTES) — avoids firing on
#      rapid turns.
#   3. Count artifacts created strictly after that turn.
#   4. Return a synthetic preamble payload when count > 0.

# Long enough that rapid-fire turns ("ok", "thanks") don't fire the nudge,
# short enough that "after lunch" / "the next morning" still does. Override
# via HISTORIAN_REENTRY_MIN_MINUTES. Same default + override the portal uses
# in backend/historian/reentry.py:DEFAULT_MIN_MINUTES — kept in sync manually
# because the cross-repo boundary blocks shared imports.
REENTRY_MIN_MINUTES_DEFAULT = 30


def _reentry_min_gap() -> "timedelta":
    from datetime import timedelta
    raw = (os.environ.get("HISTORIAN_REENTRY_MIN_MINUTES") or "").strip()
    try:
        minutes = int(raw) if raw else REENTRY_MIN_MINUTES_DEFAULT
    except ValueError:
        minutes = REENTRY_MIN_MINUTES_DEFAULT
    return timedelta(minutes=max(0, minutes))


def last_user_message_ts(
    household_id: str, contributor_id: str,
) -> Optional[datetime]:
    """Return the contributor's most recent role='user' chat message
    timestamp, or None if they have no prior chat turns.
    """
    rows = client().select(
        "hist_chat_messages",
        filters={
            "household_id": f"eq.{household_id}",
            "contributor_id": f"eq.{contributor_id}",
            "role": "eq.user",
        },
        columns="created_at",
        order="created_at.desc",
        limit=1,
    )
    if not rows:
        return None
    raw = rows[0].get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def count_artifacts_since(
    household_id: str, contributor_id: str, since: datetime,
) -> int:
    """Return the number of hist_artifacts rows for this contributor with
    `captured_at > since`. Used to gate the reentry preamble.
    """
    iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = client().select(
        "hist_artifacts",
        filters={
            "household_id": f"eq.{household_id}",
            "contributor_id": f"eq.{contributor_id}",
            "captured_at": f"gt.{iso}",
        },
        columns="id",
    )
    return len(rows)


def _format_reentry_message(count: int) -> str:
    """Quiet phrasing — same shape as backend/historian/reentry.py:_format_message."""
    noun = "contribution" if count == 1 else "contributions"
    return (
        f"I went back through our last chat — added {count} {noun} to "
        "your record. You can see them when you have a moment."
    )


def build_reentry_preamble(
    household_id: str, contributor_id: str,
) -> Optional[dict]:
    """Return `{count, message}` dict if a reentry nudge is warranted,
    or None.

    Conditions:
    1. Contributor has at least one prior user turn.
    2. At least HISTORIAN_REENTRY_MIN_MINUTES (default 30) elapsed since
       that turn — covers the "returned after a break" case without
       firing on rapid turn-taking.
    3. Strictly more than zero artifacts captured after that turn.

    Read-only. Caller is responsible for surfacing the message in the
    agent's first reply (not persisting it).
    """
    last_ts = last_user_message_ts(household_id, contributor_id)
    if last_ts is None:
        return None
    if datetime.now(timezone.utc) - last_ts < _reentry_min_gap():
        return None
    try:
        count = count_artifacts_since(household_id, contributor_id, last_ts)
    except Exception:
        # Never block the live turn on a preamble lookup failure.
        return None
    if count <= 0:
        return None
    return {"count": count, "message": _format_reentry_message(count)}


# ---------------------------------------------------------------------------
# CLI (diagnostic / ops use; not an exec tool)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="History store diagnostics.")
    parser.add_argument("--list-contributors", metavar="HOUSEHOLD_ID")
    parser.add_argument("--list-artifacts", metavar="HOUSEHOLD_ID")
    args = parser.parse_args()

    if args.list_contributors:
        rows = list_contributors(args.list_contributors)
        print(json.dumps(rows, indent=2))
    elif args.list_artifacts:
        rows = list_artifacts(args.list_artifacts)
        print(json.dumps(rows, indent=2))
    else:
        parser.print_help()
        sys.exit(1)

#!/usr/bin/env python3
"""history_manage.py — Manage the family history scope (scope-level mutations).

Mirrors event_manage.py for the family_history use case. Creates and maintains
the family_history scope in scope_store.py, links contributors as participants.

Usage (via Homer exec tool):
    python tools/history_manage.py --init
    python tools/history_manage.py --status
    python tools/history_manage.py --add-contributor --contributor-id <uuid> --phone <phone> --name <name>
    python tools/history_manage.py --remove-contributor --contributor-id <uuid>
    python tools/history_manage.py --add-thread --contributor-id <uuid> --prompt "..." [--priority 7]
    python tools/history_manage.py --context --contributor-id <uuid|jid|lid|phone>
    python tools/history_manage.py --write-artifact --contributor-id <uuid|jid|lid|phone> --kind text|image|audio|video [--body "..."] [--caption "..."] [--storage-path "..."] [--channel whatsapp]

For --context and --write-artifact, --contributor-id accepts any of:
    UUID                      e.g. 7e2c8a4b-...-9f12
    JID                       e.g. 14125551234@s.whatsapp.net
    LID                       e.g. 209650423185503@lid (must already be a scope participant)
    raw phone                 e.g. 14125551234 or +1 412 555-1234
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOOLS_DIR = str(REPO_ROOT / "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import history_store as hs
import manage_guest as mg
import scope_store
from history_invite import _rebuild_guest_config, _scope_id
from history_store import normalise_phone

_SCOPE_TYPE = "family_history"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _resolve_contributor_id(raw: str) -> tuple[str | None, str | None]:
    """Resolve a sender identifier into a hist_contributors UUID.

    Accepts any of:
    - UUID                      → returned as-is
    - "<phone>@s.whatsapp.net"  → phone normalised, looked up by phone
    - "<digits>@lid"            → looked up against scope participants
    - raw digits / formatted phone → normalised, looked up by phone

    Returns (uuid_or_None, error_message_or_None). If the resolver returns an
    error, callers should print it as JSON and exit non-zero.

    The historian guest agent often only knows the contributor by their
    WhatsApp sender id (phone or LID). Forcing it to discover the UUID
    out-of-band was the proximate cause of capture failures (PR #226 review).
    """
    if not raw:
        return None, "Contributor identifier is empty"
    raw = raw.strip()
    if _UUID_RE.match(raw):
        return raw, None

    hid = hs.household_id()

    # JID form: <phone>@s.whatsapp.net
    if raw.endswith("@s.whatsapp.net"):
        phone_part = raw.split("@", 1)[0]
        digits = normalise_phone(phone_part)[0]
        contributor = hs.get_contributor_by_phone(hid, digits)
        if contributor:
            return contributor["id"], None
        return None, (
            f"No contributor found with phone {digits} "
            f"(resolved from JID '{raw}'). Run history_invite.py --list "
            "to see contributors."
        )

    # LID form: <digits>@lid — only resolvable via scope participants
    if raw.endswith("@lid"):
        envelope = scope_store.get_scope(_scope_id(hid))
        if envelope:
            for p in envelope.get("participants", []):
                if p.get("party_id") == raw and p.get("contributor_id"):
                    return p["contributor_id"], None
        return None, (
            f"Could not resolve LID '{raw}' to a contributor. "
            "Pass the contributor UUID or full phone (e.g. 14125551234) "
            "instead. Run history_invite.py --list to see contributors."
        )

    # Otherwise: treat as a phone candidate.
    digits = normalise_phone(raw)[0]
    if digits:
        contributor = hs.get_contributor_by_phone(hid, digits)
        if contributor:
            return contributor["id"], None
        return None, (
            f"No contributor found with phone {digits}. "
            "Run history_invite.py --list to see contributors."
        )

    return None, (
        f"Could not parse '{raw}' as UUID, JID, LID, or phone. "
        "Pass the contributor UUID or full phone."
    )


def _resolve_or_exit(raw: str) -> str:
    """Resolve a contributor id or print {"error": ...} and exit 1."""
    cid, err = _resolve_contributor_id(raw)
    if err:
        print(json.dumps({"error": err}))
        sys.exit(1)
    return cid  # type: ignore[return-value]


def _get_or_create_scope(household_id: str) -> dict:
    sid = _scope_id(household_id)
    existing = scope_store.get_scope(sid)
    if existing:
        return existing
    envelope = {
        "scope_id": sid,
        "scope_type": _SCOPE_TYPE,
        "principal": "",
        "guest_identity": "I am the family historian — a patient, curious archivist helping document your family's story.",
        "creation": {
            "trigger": "user_initiated",
            "parent_scope_id": None,
            "parent_task_id": None,
            "created_at": hs._now_utc(),
        },
        "participants": [],
        "authorization": {
            "granted_capabilities": ["message"],
            "max_disclosure_tier": "broad_context",
            "escalation_triggers": [],
            "expires_at": None,
        },
        "context_layers": {
            "injected": [],
            "accumulated": [],
        },
        "task_tags": [
            {
                "task_id": f"task_{sid}",
                "description": "Family history documentation",
                "status": "active",
                "context_fragment_ids": [],
            }
        ],
        "lifecycle": {
            "last_active": None,
            "pruning_policy": "retain_all",
            "review_trigger": "never",
        },
        "escalation_log": [],
    }
    scope_store.create_scope(envelope)
    return envelope


def do_init() -> None:
    hid = hs.household_id()
    sid = _scope_id(hid)
    pre_existing = scope_store.get_scope(sid)
    if pre_existing:
        print(json.dumps({
            "status": "already_initialized",
            "scope_id": sid,
            "participants": len(pre_existing.get("participants", [])),
        }))
        return
    _get_or_create_scope(hid)
    print(json.dumps({
        "status": "initialized",
        "scope_id": sid,
        "note": (
            "Family history scope created. Invite contributors with "
            "history_invite.py --invite, then add them to the scope with --add-contributor."
        ),
    }, indent=2))


def do_status() -> None:
    hid = hs.household_id()
    sid = _scope_id(hid)
    envelope = scope_store.get_scope(sid)

    contributors = hs.list_contributors(hid)
    active_contributors = [c for c in contributors if c["status"] == "active"]
    pending_contributors = [c for c in contributors if c["status"] == "pending"]

    share = hs.get_share_link(hid)

    print(json.dumps({
        "scope_id": sid,
        "scope_initialized": envelope is not None,
        "scope_status": envelope.get("_status") if envelope else None,
        "scope_participants": len(envelope.get("participants", [])) if envelope else 0,
        "contributors_total": len(contributors),
        "contributors_active": len(active_contributors),
        "contributors_pending": len(pending_contributors),
        "active_names": [c["display_name"] for c in active_contributors],
        "share_link_active": share is not None,
        "share_code": share["code"] if share else None,
    }, indent=2))


def do_add_contributor(
    contributor_id: str,
    phone: str | None,
    name: str,
) -> None:
    hid = hs.household_id()
    envelope = _get_or_create_scope(hid)
    sid = envelope["scope_id"]

    if phone:
        phone = normalise_phone(phone)[0]
    participant_id = f"{phone}@s.whatsapp.net" if phone else contributor_id
    existing_ids = {p["party_id"] for p in envelope.get("participants", [])}
    if participant_id in existing_ids:
        print(json.dumps({
            "status": "already_participant",
            "scope_id": sid,
            "contributor_id": contributor_id,
        }))
        return

    envelope.setdefault("participants", []).append({
        "party_id": participant_id,
        "name": name,
        "handle": participant_id,
        "relationship_type": "contributor",
        "channel": "whatsapp" if phone else "web",
        "contributor_id": contributor_id,
    })
    scope_store.update_scope(sid, envelope)

    # Mirror the participant into guest_agent_acl.json so the nanobot guest
    # loop's ACL-based routing accepts inbound from this contributor. The
    # scope envelope is the truth source, but the ACL is what the loop
    # consults to decide "is this sender a guest?" (see
    # AgentLoop._resolve_guest_agent_workspace).
    acl = mg.load_acl()
    acl_entry = {
        "name": name,
        "channel": "whatsapp" if phone else "web",
        "added": mg.now_str(),
        "scope_id": sid,
        "scope_type": _SCOPE_TYPE,
        "contributor_id": contributor_id,
    }
    if phone:
        acl_entry["phone"] = phone
    acl[participant_id] = acl_entry
    mg.save_acl(acl)
    if phone:
        mg.add_to_allow_from(participant_id)

    _rebuild_guest_config()

    print(json.dumps({
        "status": "added",
        "scope_id": sid,
        "participant_id": participant_id,
        "display_name": name,
    }, indent=2))


def do_remove_contributor(contributor_id: str) -> None:
    hid = hs.household_id()
    sid = _scope_id(hid)
    envelope = scope_store.get_scope(sid)
    if not envelope:
        print(json.dumps({"error": "History scope not initialized. Run --init first."}))
        sys.exit(1)

    participants = envelope.get("participants", [])
    new_participants = [
        p for p in participants
        if p.get("contributor_id") != contributor_id
    ]
    if len(new_participants) == len(participants):
        print(json.dumps({"error": f"Contributor {contributor_id} not found in scope"}))
        sys.exit(1)

    removed_party_ids = [
        p.get("party_id") for p in participants
        if p.get("contributor_id") == contributor_id
    ]
    envelope["participants"] = new_participants
    scope_store.update_scope(sid, envelope)
    hs.archive_contributor(contributor_id)

    # Drop matching ACL entries so the nanobot guest loop stops accepting
    # inbound from this contributor.
    acl = mg.load_acl()
    for pid in removed_party_ids:
        if pid and pid in acl:
            del acl[pid]
    mg.save_acl(acl)

    _rebuild_guest_config()
    print(json.dumps({
        "status": "removed",
        "contributor_id": contributor_id,
        "scope_id": sid,
    }))


def do_add_thread(
    contributor_id: str,
    prompt: str,
    priority: int = 5,
    context: dict | None = None,
) -> None:
    hid = hs.household_id()
    row = hs.insert_thread(
        household_id=hid,
        contributor_id=contributor_id,
        prompt=prompt,
        context=context,
        priority=priority,
    )
    print(json.dumps({
        "status": "created",
        "thread_id": row["id"],
        "contributor_id": contributor_id,
        "priority": priority,
        "prompt": prompt,
    }, indent=2))


def do_write_artifact(
    contributor_id: str,
    kind: str,
    body: str | None,
    caption: str | None,
    storage_path: str | None,
    channel: str,
) -> None:
    contributor_id = _resolve_or_exit(contributor_id)
    hid = hs.household_id()
    contributor = hs.get_contributor_by_id(contributor_id)
    if not contributor:
        print(json.dumps({"error": f"Contributor {contributor_id} not found"}))
        sys.exit(1)
    if contributor["status"] != "active":
        hs.activate_contributor(contributor_id)
    row = hs.insert_artifact(
        household_id=hid,
        contributor_id=contributor_id,
        channel=channel,
        kind=kind,
        body=body,
        caption=caption,
        storage_path=storage_path,
    )
    print(json.dumps({
        "artifact_id": row["id"],
        "contributor_id": contributor_id,
        "kind": kind,
        "channel": channel,
    }, indent=2))


def do_context(contributor_id: str) -> None:
    """Return injected context for a contributor's historian turn.

    Accepts UUID, JID, LID, or phone — resolver normalises before lookup so
    the guest agent can pass whatever form the channel exposed.
    """
    contributor_id = _resolve_or_exit(contributor_id)
    hid = hs.household_id()
    contributor = hs.get_contributor_by_id(contributor_id)
    if not contributor:
        print(json.dumps({"error": f"Contributor {contributor_id} not found"}))
        sys.exit(1)

    recent_fragments = hs.list_recent_fragments(hid, contributor_id=contributor_id, limit=30)
    open_threads = hs.list_open_threads(hid, contributor_id, limit=5)
    era_coverage = hs.get_era_coverage(hid, contributor_id)
    # Read-only reentry-preamble lookup — None when no nudge is warranted
    # (no prior turns, gap too small, or zero new artifacts since last
    # turn). Agent uses this to lead its first reply with a quiet ack
    # instead of greeting cold.
    reentry_preamble = hs.build_reentry_preamble(hid, contributor_id)

    print(json.dumps({
        "contributor": {
            "id": contributor["id"],
            "display_name": contributor["display_name"],
            "relationship": contributor.get("relationship"),
            "status": contributor["status"],
        },
        "recent_fragments": recent_fragments,
        "open_threads": open_threads,
        "era_coverage": era_coverage,
        "reentry_preamble": reentry_preamble,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the family history scope.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="Initialize family history scope")
    group.add_argument("--status", action="store_true", help="Show scope status")
    group.add_argument("--add-contributor", action="store_true",
                       help="Add a contributor to the scope as a participant")
    group.add_argument("--remove-contributor", action="store_true",
                       help="Remove a contributor from the scope (archives them)")
    group.add_argument("--add-thread", action="store_true",
                       help="Create a follow-up thread for a contributor")
    group.add_argument("--context", action="store_true",
                       help="Return injected context for a contributor's turn")
    group.add_argument("--write-artifact", action="store_true",
                       help="Write a raw artifact for a contributor's inbound message")

    parser.add_argument(
        "--contributor-id",
        help=(
            "Contributor UUID (required for --add-contributor / --remove-contributor / "
            "--add-thread); for --context and --write-artifact, also accepts JID "
            "(<phone>@s.whatsapp.net), LID (<id>@lid), or raw phone digits."
        ),
    )
    parser.add_argument("--phone", help="Contributor phone (digits only)")
    parser.add_argument("--name", help="Contributor display name")
    parser.add_argument("--prompt", help="Follow-up thread prompt text")
    parser.add_argument("--priority", type=int, default=5, help="Thread priority 1-10 (default 5)")
    parser.add_argument("--thread-context", help="JSON context object for the thread")
    parser.add_argument("--kind", default="text",
                        help="Artifact kind: text, image, audio, video (default: text)")
    parser.add_argument("--body", help="Text body of the artifact")
    parser.add_argument("--caption", help="Caption (for image artifacts)")
    parser.add_argument("--storage-path", help="Storage path (for media artifacts)")
    parser.add_argument("--channel", default="whatsapp",
                        help="Source channel (default: whatsapp)")

    args = parser.parse_args()

    if args.init:
        do_init()
    elif args.status:
        do_status()
    elif args.add_contributor:
        if not args.contributor_id or not args.name:
            parser.error("--add-contributor requires --contributor-id and --name")
        do_add_contributor(args.contributor_id, args.phone, args.name)
    elif args.remove_contributor:
        if not args.contributor_id:
            parser.error("--remove-contributor requires --contributor-id")
        do_remove_contributor(args.contributor_id)
    elif args.add_thread:
        if not args.contributor_id or not args.prompt:
            parser.error("--add-thread requires --contributor-id and --prompt")
        ctx = json.loads(args.thread_context) if args.thread_context else None
        do_add_thread(args.contributor_id, args.prompt, args.priority, ctx)
    elif args.context:
        if not args.contributor_id:
            parser.error("--context requires --contributor-id")
        do_context(args.contributor_id)
    elif args.write_artifact:
        if not args.contributor_id:
            parser.error("--write-artifact requires --contributor-id")
        do_write_artifact(
            args.contributor_id,
            args.kind,
            args.body,
            args.caption,
            args.storage_path,
            args.channel,
        )


if __name__ == "__main__":
    main()

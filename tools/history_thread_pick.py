#!/usr/bin/env python3
"""history_thread_pick.py — Pick the best follow-up thread for a contributor.

Weighs open_threads by priority + recency, and surfaces era_coverage gaps
as gentle invitations when threads are exhausted or the contributor is engaged.

Output: {"thread_id": ..., "kind": "open_thread"|"era_gap"|"none", "prompt": ...}

Usage (via Homer exec tool):
    python tools/history_thread_pick.py --contributor-id <uuid>
    python tools/history_thread_pick.py --contributor-id <uuid> --exclude-thread <uuid>
    python tools/history_thread_pick.py --contributor-id <uuid> --mark-asked <thread-id>
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOOLS_DIR = str(REPO_ROOT / "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import history_store as hs

_DAYS = 86400  # seconds
_seconds_since = hs.seconds_since  # aliased for test compatibility


def _era_invitation(era_label: str) -> str:
    invitations = {
        "childhood":       "You haven't told me much about your early childhood — what do you remember about where you grew up?",
        "school":          "I'd love to hear about your school days — what stands out from that time?",
        "young-adult":     "You haven't said much about your early adult years — what were you up to in your twenties?",
        "marriage":        "I don't know much about how you met your partner — want to go back to that?",
        "parenting":       "I'd love to hear more about what it was like raising your family.",
        "career":          "You've mentioned your work a few times — what was the job that shaped you most?",
        "late-life":       "I know a little about your recent years — is there something from that time you'd like to capture?",
        "extended-family": "We haven't talked much about your wider family — grandparents, aunts, uncles. Any stories there?",
    }
    return invitations.get(era_label, f"I'd love to hear more about your {era_label} years.")


def do_pick(
    contributor_id: str,
    household_id: str,
    exclude_thread: str | None = None,
) -> None:
    # 1. Try open threads first (priority-sorted by Supabase query)
    threads = hs.list_open_threads(household_id, contributor_id, limit=10)

    for thread in threads:
        if exclude_thread and thread["id"] == exclude_thread:
            continue
        seconds_since_asked = _seconds_since(thread.get("last_asked_at"))
        # Skip if asked very recently (< 2 hours ago) — give contributor space
        if thread["status"] == "asked" and seconds_since_asked < 7200:
            continue
        print(json.dumps({
            "thread_id": thread["id"],
            "kind": "open_thread",
            "prompt": thread["prompt"],
            "priority": thread["priority"],
        }, indent=2))
        return

    # 2. Fall back to era_coverage gaps
    coverage = hs.get_era_coverage(household_id, contributor_id)
    covered_eras = {row["era_label"] for row in coverage}

    # Find eras with no coverage at all
    uncovered = [era for era in hs.ALL_ERAS if era not in covered_eras]
    if uncovered:
        era = uncovered[0]
        print(json.dumps({
            "thread_id": None,
            "kind": "era_gap",
            "era_label": era,
            "prompt": _era_invitation(era),
        }, indent=2))
        return

    # Find the sparsest covered era (lowest richness_score, not touched in > 14 days)
    stale = [
        row for row in coverage
        if _seconds_since(row.get("last_touched_at")) > 14 * _DAYS
        and row["richness_score"] < 5.0
    ]
    if stale:
        era = stale[0]["era_label"]
        print(json.dumps({
            "thread_id": None,
            "kind": "era_gap",
            "era_label": era,
            "prompt": _era_invitation(era),
        }, indent=2))
        return

    # 3. Nothing to suggest right now — back off
    print(json.dumps({
        "thread_id": None,
        "kind": "none",
        "prompt": None,
    }))


def do_mark_asked(thread_id: str) -> None:
    updated = hs.mark_thread_asked(thread_id)
    print(json.dumps({
        "status": "ok",
        "thread_id": updated.get("id", thread_id),
        "thread_status": "asked",
    }))


def main() -> None:
    hid = os.environ.get("HOMER_HOUSEHOLD_ID", "").strip()
    if not hid:
        print(json.dumps({"error": "HOMER_HOUSEHOLD_ID is not set"}))
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Pick the next follow-up thread for a contributor.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--contributor-id", help="Pick a follow-up for this contributor")
    group.add_argument("--mark-asked", metavar="THREAD_ID", help="Mark a thread as asked")
    parser.add_argument("--exclude-thread", metavar="THREAD_ID",
                        help="Skip this thread (just used it)")
    args = parser.parse_args()

    if args.mark_asked:
        do_mark_asked(args.mark_asked)
    else:
        do_pick(args.contributor_id, hid, args.exclude_thread)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""history_era_recompute.py — Recompute era_coverage richness scores for a contributor.

Called async after fragment writes by history_extract.py. Can also be run
manually to backfill era coverage after bulk imports.

richness_score formula:
  base = fragment_count (volume signal)
  recency_bonus = 2.0 if touched within 7 days, 1.0 if within 30 days, else 0
  diversity_bonus = unique fragment kinds touching this era × 0.5
  score = base + recency_bonus + diversity_bonus

Output: {"contributor_id": ..., "eras_updated": N, "coverage": {...}}

Usage:
    python tools/history_era_recompute.py --contributor-id <uuid>
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

_seconds_since = hs.seconds_since

_ERA_KEYWORDS: dict[str, list[str]] = {
    "childhood":       ["child", "kid", "born", "baby", "young", "mother", "father", "parent",
                        "hometown", "village", "home town", "grew up", "childhood"],
    "school":          ["school", "class", "teacher", "student", "exam", "graduate", "university",
                        "college", "education", "study", "studies", "lesson"],
    "young-adult":     ["twenties", "twenty", "apartment", "roommate", "first job", "moved out",
                        "independence", "dating", "girlfriend", "boyfriend"],
    "marriage":        ["married", "wedding", "wife", "husband", "spouse", "proposal",
                        "engagement", "honeymoon", "anniversary", "divorce"],
    "parenting":       ["son", "daughter", "child", "kids", "baby", "parent", "raised",
                        "diaper", "school run", "homework", "birthday party"],
    "career":          ["job", "work", "boss", "company", "promotion", "salary", "retired",
                        "business", "office", "career", "profession"],
    "late-life":       ["retired", "retirement", "grandchild", "grandkid", "senior",
                        "elderly", "nursing", "health", "hospital", "widow"],
    "extended-family": ["aunt", "uncle", "cousin", "grandparent", "grandmother", "grandfather",
                        "relative", "in-law", "niece", "nephew", "clan", "family tree"],
}

_DAYS = 86400


def _fragment_touches_era(fragment: dict, era: str) -> bool:
    """Heuristic: does this fragment's payload text mention era keywords?"""
    keywords = _ERA_KEYWORDS.get(era, [])
    payload_str = json.dumps(fragment.get("payload", {})).lower()
    return any(kw in payload_str for kw in keywords)


def do_recompute(contributor_id: str) -> None:
    hid = os.environ.get("HOMER_HOUSEHOLD_ID", "").strip()
    if not hid:
        print(json.dumps({"error": "HOMER_HOUSEHOLD_ID is not set"}))
        sys.exit(1)

    all_fragments = hs.fetch_all_fragments(hid, contributor_id=contributor_id)

    coverage_out: dict[str, dict] = {}
    updated = 0

    for era in hs.ALL_ERAS:
        matching = [f for f in all_fragments if _fragment_touches_era(f, era)]
        if not matching:
            coverage_out[era] = {"fragment_count": 0, "richness_score": 0.0}
            continue

        fragment_count = len(matching)
        last_touched = max((f.get("created_at") for f in matching), default=None)

        seconds_ago = _seconds_since(last_touched)
        if seconds_ago < 7 * _DAYS:
            recency_bonus = 2.0
        elif seconds_ago < 30 * _DAYS:
            recency_bonus = 1.0
        else:
            recency_bonus = 0.0

        unique_kinds = len({f.get("kind") for f in matching})
        diversity_bonus = unique_kinds * 0.5

        score = float(fragment_count) + recency_bonus + diversity_bonus

        hs.upsert_era_coverage(
            household_id=hid,
            contributor_id=contributor_id,
            era_label=era,
            fragment_count=fragment_count,
            richness_score=round(score, 2),
        )
        coverage_out[era] = {"fragment_count": fragment_count, "richness_score": round(score, 2)}
        updated += 1

    print(json.dumps({
        "contributor_id": contributor_id,
        "eras_updated": updated,
        "coverage": coverage_out,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute era_coverage for a contributor.")
    parser.add_argument("--contributor-id", required=True, help="Contributor UUID")
    args = parser.parse_args()
    do_recompute(args.contributor_id)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-time migration: read existing ## Guests tables from status.md files
and insert rows into event_store (state/events.db).

Usage:
    python tools/migrate_guests_to_db.py [--dry-run]

Idempotent: skips guests that already exist in the database.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
HOMER_TOOLS = str(REPO_ROOT / "tools")
if HOMER_TOOLS not in sys.path:
    sys.path.insert(0, HOMER_TOOLS)

import event_store

EVENTS_DIR = Path(os.environ["HOMER_EVENTS_DIR"]) if os.environ.get("HOMER_EVENTS_DIR") else REPO_ROOT / "context" / "events"


def parse_guest_rows(content: str) -> list[dict]:
    """Parse data rows from the old ## Guests markdown table."""
    guests_m = re.search(r"## Guests\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not guests_m:
        return []
    rows = []
    for line in guests_m.group(1).strip().split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\| *Name", line) or re.match(r"^\|[-| ]+\|$", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3:
            rows.append({
                "name": cells[0],
                "phone": cells[1] if cells[1] != "—" else None,
                "jid": cells[2],
                "status": cells[3] if len(cells) > 3 else "",
                "added": cells[4] if len(cells) > 4 else "",
            })
    return rows


def migrate(dry_run: bool = False) -> dict:
    """Migrate all events. Returns summary."""
    if not EVENTS_DIR.exists():
        return {"events": 0, "migrated": 0, "skipped": 0}

    total_migrated = 0
    total_skipped = 0
    events_processed = 0

    for edir in sorted(EVENTS_DIR.iterdir()):
        sp = edir / "status.md"
        if not sp.exists():
            continue

        event_id = edir.name
        content = sp.read_text(encoding="utf-8")
        guests = parse_guest_rows(content)
        if not guests:
            continue

        events_processed += 1
        for g in guests:
            if not g["name"] or not g["jid"]:
                continue

            existing = event_store.get_guest(event_id, g["jid"])
            if existing:
                print(f"  Skip {event_id}/{g['name']} (already in DB)")
                total_skipped += 1
                continue

            channel = "telegram" if g["jid"].startswith("tg:") else "whatsapp"
            if dry_run:
                print(f"  [DRY RUN] Would migrate {event_id}/{g['name']} ({g['jid']})")
            else:
                event_store.add_guest(
                    event_id=event_id,
                    participant_id=g["jid"],
                    name=g["name"],
                    phone=g["phone"],
                    channel=channel,
                    added_at=g["added"] or None,
                )
                print(f"  Migrated {event_id}/{g['name']} ({g['jid']})")
            total_migrated += 1

    return {
        "events": events_processed,
        "migrated": total_migrated,
        "skipped": total_skipped,
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate guest rosters from status.md to events.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without writing")
    args = parser.parse_args()

    result = migrate(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

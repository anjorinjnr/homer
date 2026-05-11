#!/usr/bin/env python3
"""
event_manage.py — Manage events (trips, gatherings) in Homer.

Creates and maintains event state files under context/events/<event_id>/.
Each event has a status.md with metadata, open items, and confirmed details.
Guest roster and RSVP data are stored in state/events.db (SQLite).

Usage (via Homer exec tool):
    python tools/event_manage.py --create --name "MTB Colorado" --event-id mtb_colorado
    python tools/event_manage.py --status --event-id mtb_colorado
    python tools/event_manage.py --update --event-id mtb_colorado --field dates --value "2026-07-15 to 2026-07-20"
    python tools/event_manage.py --update --event-id mtb_colorado --field location --value "Crested Butte, CO"
    python tools/event_manage.py --add-item --event-id mtb_colorado --item "Book Airbnb" [--assignee "@alex"]
    python tools/event_manage.py --check-item --event-id mtb_colorado --item "Book Airbnb"
    python tools/event_manage.py --remove-item --event-id mtb_colorado --item "Book Airbnb"
    python tools/event_manage.py --set-status --event-id mtb_colorado --lifecycle confirmed
    python tools/event_manage.py --close --event-id mtb_colorado
    python tools/event_manage.py --list
    python tools/event_manage.py --budget-summary --event-id mtb_colorado
    python tools/event_manage.py --guests --event-id mtb_colorado
    python tools/event_manage.py --rsvp --event-id mtb_colorado --guest "Jake" --rsvp-status confirmed --headcount 3
    python tools/event_manage.py --rsvp-summary --event-id mtb_colorado
    python tools/event_manage.py --rsvp-pending --event-id mtb_colorado

Event status.md structure:
    # Event Name
    Status: Coordinating | Confirmed | Active | Archived
    Dates: TBD or date range
    Created: YYYY-MM-DD

    ## Guests (N)
    summary line rendered from SQLite

    ## Open Items
    - [ ] Item description (@assignee)

    ## Confirmed Details
    - **Location**: ...
    - **Lodging**: ...

    ## Budget
    Sheet: <url>
    Sheet-ID: <id>

    ## Activity Log
    | Date | What |
    |------|------|
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent.resolve()
EVENTS_DIR = Path(os.environ["HOMER_EVENTS_DIR"]) if os.environ.get("HOMER_EVENTS_DIR") else REPO_ROOT / "context" / "events"
LOCAL_TZ = ZoneInfo("America/New_York")
HOMER_VENV = str(REPO_ROOT / ".venv" / "bin" / "python")
HOMER_TOOLS = str(REPO_ROOT / "tools")

if HOMER_TOOLS not in sys.path:
    sys.path.insert(0, HOMER_TOOLS)
# Repo root too, so `from tools.X import Y` resolves when run as a script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import event_store
import guest_scope_guard

# Subcommands the guest agent may invoke. Enforced below when
# HOMER_GUEST_WORKSPACE is set; see tools/guest_scope_guard.py.
_GUEST_ALLOWED_ACTIONS = frozenset({
    "status",
    "add_note",
    "guests",
    "rsvp",
    "rsvp_summary",
    "rsvp_pending",
})


def _rebuild_if_guests(event_id: str) -> None:
    """Rebuild guest workspace if any active scope references this event."""
    try:
        sys.path.insert(0, HOMER_TOOLS)
        import scope_store
        for env in scope_store.list_active_scopes():
            # Check context_source.ref
            ref = env.get("context_source", {}).get("ref", "")
            if not ref:
                # Fallback: check task_tags
                for t in env.get("task_tags", []):
                    if t.get("task_id") == f"task_{event_id}":
                        ref = event_id
                        break
            if ref == event_id:
                subprocess.run(
                    [HOMER_VENV, f"{HOMER_TOOLS}/build_context.py"],
                    capture_output=True, text=True, timeout=30,
                )
                return
    except Exception:
        pass


STATUS_TEMPLATE = """# {name}
Status: Coordinating
Dates: TBD
Created: {created}

## Guests (0)

## Open Items

## Confirmed Details

## Notes

## Budget

## Activity Log
| Date | What |
|------|------|
"""


def now_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def now_ts() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def event_dir(event_id: str) -> Path:
    return EVENTS_DIR / event_id


def status_path(event_id: str) -> Path:
    return event_dir(event_id) / "status.md"


def read_status(event_id: str) -> str:
    p = status_path(event_id)
    if not p.exists():
        print(json.dumps({"error": f"Event '{event_id}' not found"}))
        sys.exit(1)
    return p.read_text(encoding="utf-8")


def write_status(event_id: str, content: str) -> None:
    status_path(event_id).write_text(content, encoding="utf-8")


def append_activity(content: str, what: str) -> str:
    """Append a row to the Activity Log table.

    Finds the ## Activity Log section and inserts after the last table row.
    Falls back to appending at end of file if the section is missing or malformed.
    """
    # Sanitize for markdown table safety: collapse newlines, escape pipes
    what = what.replace("\n", " ").replace("\r", " ").replace("|", "\\|")
    entry = f"| {now_ts()} | {what} |"
    marker = "## Activity Log"
    if marker not in content:
        # Section missing — append it
        return content.rstrip("\n") + f"\n\n{marker}\n| Date | What |\n|------|------|\n{entry}\n"

    lines = content.split("\n")
    insert_idx = None
    in_activity = False
    for i, line in enumerate(lines):
        if line.strip().startswith(marker):
            in_activity = True
        elif in_activity and line.strip().startswith("## "):
            break
        elif in_activity and (line.strip().startswith("|") or line.strip() == ""):
            insert_idx = i
    if insert_idx is not None:
        lines.insert(insert_idx + 1, entry)
    else:
        # Fallback: insert right after the marker line
        for i, line in enumerate(lines):
            if line.strip().startswith(marker):
                lines.insert(i + 1, f"| Date | What |\n|------|------|\n{entry}")
                break
    return "\n".join(lines)



def create_budget_sheet(name: str, event_id: str) -> dict | None:
    """Create a Google Sheet for the event's budget tracking."""
    title = f"{name} — Budget"
    try:
        result = subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/sheets.py",
             "--mode", "create",
             "--title", title,
             "--sheets", "Expenses,Summary"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if "error" in data:
            return None

        # Write headers to Expenses tab
        sheet_id = data["sheet_id"]
        headers = json.dumps([["Date", "Item", "Amount", "Paid By", "Split Among", "Notes"]])
        subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/sheets.py",
             "--mode", "write",
             "--sheet-id", sheet_id,
             "--range", "Expenses!A1:F1",
             "--values", headers],
            capture_output=True, text=True, timeout=30
        )

        # Write headers to Summary tab
        summary_headers = json.dumps([["Person", "Total Paid", "Fair Share", "Balance"]])
        subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/sheets.py",
             "--mode", "write",
             "--sheet-id", sheet_id,
             "--range", "Summary!A1:D1",
             "--values", summary_headers],
            capture_output=True, text=True, timeout=30
        )

        return data
    except Exception:
        return None


def do_create(name: str, event_id: str) -> None:
    edir = event_dir(event_id)
    if edir.exists():
        print(json.dumps({"error": f"Event '{event_id}' already exists"}))
        sys.exit(1)

    edir.mkdir(parents=True, exist_ok=True)

    content = STATUS_TEMPLATE.format(name=name, created=now_str())

    # Create budget sheet
    sheet_info = create_budget_sheet(name, event_id)
    if sheet_info:
        content = content.replace(
            "## Budget\n",
            f"## Budget\nSheet: {sheet_info['url']}\nSheet-ID: {sheet_info['sheet_id']}\n"
        )

    content = append_activity(content, f"Event created: {name}")
    write_status(event_id, content)

    result = {
        "status": "created",
        "event_id": event_id,
        "name": name,
        "path": str(status_path(event_id)),
    }
    if sheet_info:
        result["budget_sheet_url"] = sheet_info["url"]
        result["budget_sheet_id"] = sheet_info["sheet_id"]

    print(json.dumps(result, indent=2))


def parse_status_header(content: str) -> dict:
    """Extract core fields from a status.md string."""
    name_m = re.search(r"^# (.+)", content, re.MULTILINE)
    status_m = re.search(r"^Status:\s*(.+)", content, re.MULTILINE)
    dates_m = re.search(r"^Dates:\s*(.+)", content, re.MULTILINE)
    location_m = re.search(r"^- \*\*Location\*\*:\s*(.+)", content, re.MULTILINE | re.IGNORECASE)
    return {
        "name": name_m.group(1).strip() if name_m else "",
        "status": status_m.group(1).strip() if status_m else "",
        "dates": dates_m.group(1).strip() if dates_m else "TBD",
        "location": location_m.group(1).strip() if location_m else "",
    }


def do_status(event_id: str) -> None:
    content = read_status(event_id)

    header = parse_status_header(content)

    # Count guests from SQLite
    guest_rows = event_store.guest_count(event_id)

    # Count open items
    open_items = re.findall(r"^- \[ \] .+", content, re.MULTILINE)
    checked_items = re.findall(r"^- \[x\] .+", content, re.MULTILINE)

    # Extract sheet URL
    sheet_m = re.search(r"^Sheet:\s*(.+)", content, re.MULTILINE)
    sheet_id_m = re.search(r"^Sheet-ID:\s*(.+)", content, re.MULTILINE)

    # Extract Notes section lines (strip the leading "- " bullet prefix for readability)
    notes: list[str] = []
    notes_m = re.search(r"## Notes\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if notes_m:
        for line in notes_m.group(1).strip().split("\n"):
            line = line.strip()
            if line:
                notes.append(line[2:] if line.startswith("- ") else line)

    print(json.dumps({
        "event_id": event_id,
        "name": header["name"],
        "status": header["status"],
        "dates": header["dates"],
        "guest_count": guest_rows,
        "open_items": len(open_items),
        "completed_items": len(checked_items),
        "budget_sheet_url": sheet_m.group(1).strip() if sheet_m else "",
        "budget_sheet_id": sheet_id_m.group(1).strip() if sheet_id_m else "",
        "open_item_list": [item.replace("- [ ] ", "").strip() for item in open_items],
        "notes": notes,
    }, indent=2))


def do_update(event_id: str, field: str, value: str) -> None:
    content = read_status(event_id)

    field_lower = field.lower()

    if field_lower in ("dates", "status"):
        # Update inline field
        pattern = rf"^({field_lower.capitalize()}:\s*).+$"
        if re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
            content = re.sub(pattern, rf"\g<1>{value}", content, flags=re.MULTILINE | re.IGNORECASE)
        else:
            print(json.dumps({"error": f"Field '{field}' not found in status.md"}))
            sys.exit(1)
    else:
        # Update or add under Confirmed Details
        detail_pattern = rf"^- \*\*{re.escape(field)}\*\*:.*$"
        new_line = f"- **{field}**: {value}"
        if re.search(detail_pattern, content, re.MULTILINE | re.IGNORECASE):
            content = re.sub(detail_pattern, new_line, content, flags=re.MULTILINE | re.IGNORECASE)
        else:
            # Add after ## Confirmed Details header
            content = content.replace(
                "## Confirmed Details\n",
                f"## Confirmed Details\n{new_line}\n"
            )

    content = append_activity(content, f"Updated {field}: {value}")
    write_status(event_id, content)
    _rebuild_if_guests(event_id)
    print(json.dumps({"status": "updated", "field": field, "value": value}))


def do_add_item(event_id: str, item: str, assignee: str | None = None) -> None:
    content = read_status(event_id)
    suffix = f" ({assignee})" if assignee else ""
    new_item = f"- [ ] {item}{suffix}"

    # Insert after ## Open Items header
    marker = "## Open Items\n"
    if marker not in content:
        print(json.dumps({"error": "Open Items section not found"}))
        sys.exit(1)

    content = content.replace(marker, f"{marker}{new_item}\n")
    content = append_activity(content, f"Added item: {item}")
    write_status(event_id, content)
    _rebuild_if_guests(event_id)
    print(json.dumps({"status": "added", "item": item, "assignee": assignee or ""}))


def do_check_item(event_id: str, keyword: str) -> None:
    content = read_status(event_id)
    keyword_lower = keyword.lower()

    # Find matching unchecked item
    match = re.search(
        rf"^- \[ \] (.+{re.escape(keyword_lower)}.+)$",
        content, re.MULTILINE | re.IGNORECASE
    )
    if not match:
        # Try partial match
        for m in re.finditer(r"^- \[ \] (.+)$", content, re.MULTILINE):
            if keyword_lower in m.group(1).lower():
                match = m
                break

    if not match:
        print(json.dumps({"error": f"No open item matching '{keyword}' found"}))
        sys.exit(1)

    old_line = match.group(0)
    new_line = old_line.replace("- [ ] ", "- [x] ", 1)
    content = content.replace(old_line, new_line, 1)
    content = append_activity(content, f"Completed: {match.group(1).strip()}")
    write_status(event_id, content)
    _rebuild_if_guests(event_id)
    print(json.dumps({"status": "checked", "item": match.group(1).strip()}))


def do_remove_item(event_id: str, keyword: str) -> None:
    content = read_status(event_id)
    keyword_lower = keyword.lower()

    for m in re.finditer(r"^- \[[ x]\] (.+)$", content, re.MULTILINE):
        if keyword_lower in m.group(1).lower():
            content = content.replace(m.group(0) + "\n", "", 1)
            content = append_activity(content, f"Removed item: {m.group(1).strip()}")
            write_status(event_id, content)
            _rebuild_if_guests(event_id)
            print(json.dumps({"status": "removed", "item": m.group(1).strip()}))
            return

    print(json.dumps({"error": f"No item matching '{keyword}' found"}))
    sys.exit(1)


def do_set_status(event_id: str, lifecycle: str) -> None:
    if lifecycle.lower() == "archived":
        print(json.dumps({"error": "Use --close to archive an event (it handles guest revocation and final budget)"}))
        sys.exit(1)

    content = read_status(event_id)
    display = lifecycle.capitalize()
    content = re.sub(r"^(Status:\s*).+$", rf"\g<1>{display}", content, flags=re.MULTILINE)
    content = append_activity(content, f"Status changed to: {display}")
    write_status(event_id, content)
    _rebuild_if_guests(event_id)
    print(json.dumps({"status": "updated", "lifecycle": display}))


def do_close(event_id: str) -> None:
    content = read_status(event_id)

    # Final budget summary BEFORE revoking guests (needs guest count)
    budget_result = None
    sheet_id_m = re.search(r"^Sheet-ID:\s*(.+)", content, re.MULTILINE)
    if sheet_id_m:
        r = subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/event_manage.py",
             "--budget-summary", "--event-id", event_id],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            try:
                budget_result = json.loads(r.stdout)
            except Exception:
                pass

    # Revoke guest channel access but preserve event_store records (RSVP history).
    # --preserve-roster keeps events.db rows intact while revoking scope/ACL access.
    guests = event_store.list_guests(event_id)
    revoked = []
    for g in guests:
        if not g.get("participant_id"):
            continue
        # Use participant_id (phone/telegram-id) for precise removal, not name
        pid = g["participant_id"]
        if pid.startswith("tg:"):
            id_flag = ["--telegram-id", pid.removeprefix("tg:")]
        else:
            phone = g.get("phone") or ""
            id_flag = ["--phone", phone] if phone else ["--name", g.get("name", "")]
        subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/manage_event_guest.py",
             "--remove", "--event-id", event_id, *id_flag,
             "--preserve-roster"],
            capture_output=True, text=True, timeout=30,
        )
        revoked.append(g.get("name", pid))

    # Re-read content (manage_event_guest may have updated the summary)
    content = read_status(event_id)
    content = re.sub(r"^(Status:\s*).+$", r"\g<1>Archived", content, flags=re.MULTILINE)
    content = append_activity(content, f"Event archived. Guests revoked: {', '.join(revoked) if revoked else 'none'}")
    write_status(event_id, content)

    # Fire use_case_completed for event lifecycle. Attribute to the
    # household — there's no user identity at event-archival time, and
    # hashing a literal "system" string would collapse every tenant to one
    # global distinct_id.
    try:
        from tools.analytics.events import track_use_case_completed
        from tools.analytics.identity import get_distinct_id, get_household_id

        household_id = get_household_id()
        if household_id:
            distinct_id = get_distinct_id(household_id, "household")
            track_use_case_completed(
                distinct_id,
                use_case_tag="calendar",
                turns_to_completion=len(revoked) + 1,
                outcome="completed",
            )
    except Exception:
        logging.getLogger(__name__).debug("analytics: use_case_completed failed", exc_info=True)

    print(json.dumps({
        "status": "archived",
        "event_id": event_id,
        "guests_revoked": revoked,
        "final_budget": budget_result,
    }))


def do_list() -> None:
    if not EVENTS_DIR.exists():
        print(json.dumps([]))
        return

    events = []
    for edir in sorted(EVENTS_DIR.iterdir()):
        sp = edir / "status.md"
        if not sp.exists():
            continue
        content = sp.read_text(encoding="utf-8")
        header = parse_status_header(content)
        events.append({
            "event_id": edir.name,
            **{k: header[k] for k in ("name", "status", "dates")},
        })
    print(json.dumps(events, indent=2))


def do_budget_summary(event_id: str) -> None:
    content = read_status(event_id)
    sheet_id_m = re.search(r"^Sheet-ID:\s*(.+)", content, re.MULTILINE)
    if not sheet_id_m:
        print(json.dumps({"error": "No budget sheet found for this event"}))
        sys.exit(1)

    sheet_id = sheet_id_m.group(1).strip()

    # Read expenses
    try:
        result = subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/sheets.py",
             "--mode", "read",
             "--sheet-id", sheet_id,
             "--range", "Expenses"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(json.dumps({"error": "Failed to read budget sheet"}))
            sys.exit(1)

        data = json.loads(result.stdout)
        rows = data.get("values", [])

        if len(rows) <= 1:
            print(json.dumps({
                "event_id": event_id,
                "total_expenses": 0,
                "expenses": [],
                "per_person": {},
                "message": "No expenses logged yet"
            }))
            return

        # Skip header row
        expenses = []
        totals_paid: dict[str, float] = {}
        for row in rows[1:]:
            if len(row) < 4:
                continue
            date, item, amount_str, paid_by = row[0], row[1], row[2], row[3]
            split_among = row[4] if len(row) > 4 else "all"
            try:
                amount = float(amount_str.replace("$", "").replace(",", ""))
            except ValueError:
                continue
            expenses.append({
                "date": date, "item": item, "amount": amount,
                "paid_by": paid_by, "split_among": split_among,
            })
            totals_paid[paid_by] = totals_paid.get(paid_by, 0) + amount

        total = sum(e["amount"] for e in expenses)

        # Count guests for equal split (+1 for the owner)
        db_guest_count = event_store.guest_count(event_id) + 1

        fair_share = total / db_guest_count if db_guest_count > 0 else 0

        # Include all roster members (even those who haven't paid anything)
        all_names = {g["name"] for g in event_store.list_guests(event_id) if g["name"]}
        # Also include anyone who has paid (may not be in roster, e.g. the owner)
        all_names.update(totals_paid.keys())

        per_person = {}
        for person in sorted(all_names):
            paid = totals_paid.get(person, 0)
            per_person[person] = {
                "paid": round(paid, 2),
                "fair_share": round(fair_share, 2),
                "balance": round(paid - fair_share, 2),
            }

        # Write settlements back to Summary tab
        summary_rows = [["Person", "Total Paid", "Fair Share", "Balance"]]
        for person in sorted(per_person):
            p = per_person[person]
            summary_rows.append([person, p["paid"], p["fair_share"], p["balance"]])
        subprocess.run(
            [HOMER_VENV, f"{HOMER_TOOLS}/sheets.py",
             "--mode", "write",
             "--sheet-id", sheet_id,
             "--range", "Summary!A1",
             "--values", json.dumps(summary_rows)],
            capture_output=True, text=True, timeout=30
        )

        print(json.dumps({
            "event_id": event_id,
            "total_expenses": round(total, 2),
            "guest_count": db_guest_count,
            "fair_share_per_person": round(fair_share, 2),
            "per_person": per_person,
            "expense_count": len(expenses),
        }, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def do_add_note(event_id: str, note: str) -> None:
    """Append a timestamped note to the ## Notes section of status.md."""
    content = read_status(event_id)
    # Sanitize: collapse newlines to prevent markdown structure breakage
    note = note.replace("\n", " ").replace("\r", " ")
    entry = f"- {now_ts()}: {note}"

    notes_m = re.search(r"(## Notes\n)", content)
    if notes_m:
        insert_at = notes_m.end()
        content = content[:insert_at] + entry + "\n" + content[insert_at:]
    else:
        # No Notes section — append before Activity Log or at end
        activity_m = re.search(r"^## Activity Log", content, re.MULTILINE)
        if activity_m:
            content = content[:activity_m.start()] + "## Notes\n" + entry + "\n\n" + content[activity_m.start():]
        else:
            content = content.rstrip() + "\n\n## Notes\n" + entry + "\n"

    write_status(event_id, content)
    _rebuild_if_guests(event_id)
    print(json.dumps({"status": "ok", "event_id": event_id, "note": note}))


def update_guest_summary(event_id: str) -> None:
    """Update the ## Guests summary line in status.md from SQLite data."""
    content = read_status(event_id)
    summary = event_store.render_guest_summary(event_id)

    # Replace the existing ## Guests section up to the next ## heading or end of file
    content = re.sub(
        r"## Guests.*?(?=\n## |\Z)",
        summary + "\n",
        content,
        flags=re.DOTALL,
    )
    write_status(event_id, content)


def do_guests(event_id: str) -> None:
    """Print full guest list with RSVP status from SQLite."""
    guests = event_store.list_guests(event_id)
    print(json.dumps(guests, indent=2))


VALID_RSVP_STATUSES = event_store.ALL_RSVP_STATUSES


def record_rsvp_activity(event_id: str, guest_name: str, rsvp_status: str,
                         headcount: int | None = None, note: str | None = None) -> None:
    """Update status.md with RSVP activity log entry + guest summary. Shared by CLI and web."""
    content = read_status(event_id)
    note_text = f" — {note}" if note else ""
    hc_text = f", party of {headcount}" if headcount else ""
    content = append_activity(content, f"RSVP: {guest_name} {rsvp_status}{hc_text}{note_text}")
    write_status(event_id, content)
    update_guest_summary(event_id)
    _rebuild_if_guests(event_id)


def do_rsvp(event_id: str, guest_name: str, rsvp_status: str,
            headcount: int | None = None, note: str | None = None) -> None:
    """Record or update an RSVP for a guest."""
    if rsvp_status not in VALID_RSVP_STATUSES:
        print(json.dumps({"error": f"Invalid RSVP status '{rsvp_status}'. Must be one of: {', '.join(sorted(VALID_RSVP_STATUSES))}"}))
        sys.exit(1)

    match = event_store.find_guest_by_name(event_id, guest_name)
    if not match:
        print(json.dumps({"error": f"Guest '{guest_name}' not found in event '{event_id}'"}))
        sys.exit(1)

    ok = event_store.update_rsvp(event_id, match["participant_id"], rsvp_status, headcount, note)
    if not ok:
        print(json.dumps({"error": "Failed to update RSVP"}))
        sys.exit(1)

    record_rsvp_activity(event_id, guest_name, rsvp_status, headcount, note)

    print(json.dumps({
        "status": "ok",
        "event_id": event_id,
        "guest": guest_name,
        "rsvp_status": rsvp_status,
        "headcount": headcount,
    }, indent=2))


VALID_RSVP_FIELD_TYPES = {"text", "number", "select", "checkbox", "textarea"}


def do_set_rsvp_fields(event_id: str, fields_json: str) -> None:
    """Set the RSVP form field configuration for an event."""
    read_status(event_id)
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)
    if not isinstance(fields, list):
        print(json.dumps({"error": "rsvp_fields must be a JSON array"}))
        sys.exit(1)
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            print(json.dumps({"error": f"Field {i} must be an object"}))
            sys.exit(1)
        for key in ("id", "type", "label"):
            if key not in f:
                print(json.dumps({"error": f"Field {i} missing required key '{key}'"}))
                sys.exit(1)
        if f["type"] not in VALID_RSVP_FIELD_TYPES:
            print(json.dumps({"error": f"Field {i} has invalid type '{f['type']}'. Must be one of: {', '.join(sorted(VALID_RSVP_FIELD_TYPES))}"}))
            sys.exit(1)
    event_store.set_event_meta(event_id, rsvp_fields=fields)
    print(json.dumps({"status": "ok", "event_id": event_id, "rsvp_fields": fields}))


def do_set_rsvp_deadline(event_id: str, deadline: str) -> None:
    """Set the RSVP deadline for an event."""
    read_status(event_id)
    event_store.set_event_meta(event_id, rsvp_deadline=deadline)
    content = read_status(event_id)
    content = append_activity(content, f"RSVP deadline set: {deadline}")
    write_status(event_id, content)
    print(json.dumps({"status": "ok", "event_id": event_id, "rsvp_deadline": deadline}))


def do_set_event_description(event_id: str, description: str) -> None:
    """Set the RSVP page description for an event."""
    read_status(event_id)
    event_store.set_event_meta(event_id, event_description=description)
    print(json.dumps({"status": "ok", "event_id": event_id}))


def do_rsvp_summary(event_id: str) -> None:
    """Print RSVP summary with counts."""
    summary = event_store.rsvp_summary(event_id)
    print(json.dumps(summary, indent=2))


def do_rsvp_pending(event_id: str) -> None:
    """Print guests who haven't responded."""
    pending = event_store.rsvp_pending(event_id)
    print(json.dumps(pending, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Homer events.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Create a new event")
    group.add_argument("--status", action="store_true", help="Show event status")
    group.add_argument("--update", action="store_true", help="Update an event field")
    group.add_argument("--add-item", action="store_true", help="Add an open item")
    group.add_argument("--check-item", action="store_true", help="Mark an item as done")
    group.add_argument("--remove-item", action="store_true", help="Remove an item")
    group.add_argument("--set-status", action="store_true", help="Change event lifecycle status")
    group.add_argument("--close", action="store_true", help="Archive the event")
    group.add_argument("--list", action="store_true", help="List all events")
    group.add_argument("--budget-summary", action="store_true", help="Show budget summary")
    group.add_argument("--add-note", action="store_true", help="Append a timestamped note to the event")
    group.add_argument("--guests", action="store_true", help="List all guests with RSVP status")
    group.add_argument("--rsvp", action="store_true", help="Record or update an RSVP")
    group.add_argument("--rsvp-summary", action="store_true", help="Show RSVP summary")
    group.add_argument("--rsvp-pending", action="store_true", help="List guests who haven't responded")
    group.add_argument("--set-rsvp-fields", action="store_true", help="Set RSVP form field config (JSON)")
    group.add_argument("--set-rsvp-deadline", action="store_true", help="Set RSVP deadline")
    group.add_argument("--set-event-description", action="store_true", help="Set RSVP page description")

    parser.add_argument("--event-id", help="Event identifier (e.g. mtb_colorado)")
    parser.add_argument("--name", help="Event name (for --create)")
    parser.add_argument("--field", help="Field name to update (for --update)")
    parser.add_argument("--value", help="Field value (for --update)")
    parser.add_argument("--item", help="Item description (for --add-item, --check-item, --remove-item)")
    parser.add_argument("--assignee", help="Person responsible (for --add-item)")
    parser.add_argument("--lifecycle", help="New lifecycle status (for --set-status)")
    parser.add_argument("--note", help="Note text (for --add-note, --rsvp)")
    parser.add_argument("--guest", help="Guest name (for --rsvp)")
    parser.add_argument("--rsvp-status", help="RSVP status: confirmed, declined, maybe (for --rsvp)")
    parser.add_argument("--headcount", type=int, help="Party size (for --rsvp)")
    parser.add_argument("--fields", help="JSON array of RSVP field definitions (for --set-rsvp-fields)")
    parser.add_argument("--deadline", help="RSVP deadline date (for --set-rsvp-deadline)")
    parser.add_argument("--description", help="Event description for RSVP page (for --set-event-description)")

    args = parser.parse_args()

    if guest_scope_guard.is_guest_mode():
        action = next(
            (name for name in (
                "create", "status", "update", "add_item", "check_item", "remove_item",
                "set_status", "close", "list", "budget_summary", "add_note", "guests",
                "rsvp", "rsvp_summary", "rsvp_pending",
                "set_rsvp_fields", "set_rsvp_deadline", "set_event_description",
            ) if getattr(args, name, False)),
            None,
        )
        if action not in _GUEST_ALLOWED_ACTIONS:
            print(
                json.dumps({
                    "ok": False,
                    "error": f"Action '--{(action or '?').replace('_', '-')}' is not "
                             "available to the guest agent. Allowed: "
                             + ", ".join(f"--{a.replace('_', '-')}" for a in sorted(_GUEST_ALLOWED_ACTIONS)),
                }),
                file=sys.stderr,
            )
            sys.exit(2)
        if args.event_id:
            guest_scope_guard.assert_event_allowed(args.event_id)

    if args.create:
        if not args.name or not args.event_id:
            parser.error("--create requires --name and --event-id")
        do_create(args.name, args.event_id)
    elif args.status:
        if not args.event_id:
            parser.error("--status requires --event-id")
        do_status(args.event_id)
    elif args.update:
        if not args.event_id or not args.field or not args.value:
            parser.error("--update requires --event-id, --field, and --value")
        do_update(args.event_id, args.field, args.value)
    elif args.add_item:
        if not args.event_id or not args.item:
            parser.error("--add-item requires --event-id and --item")
        do_add_item(args.event_id, args.item, args.assignee)
    elif args.check_item:
        if not args.event_id or not args.item:
            parser.error("--check-item requires --event-id and --item")
        do_check_item(args.event_id, args.item)
    elif args.remove_item:
        if not args.event_id or not args.item:
            parser.error("--remove-item requires --event-id and --item")
        do_remove_item(args.event_id, args.item)
    elif args.set_status:
        if not args.event_id or not args.lifecycle:
            parser.error("--set-status requires --event-id and --lifecycle")
        do_set_status(args.event_id, args.lifecycle)
    elif args.close:
        if not args.event_id:
            parser.error("--close requires --event-id")
        do_close(args.event_id)
    elif args.list:
        do_list()
    elif args.budget_summary:
        if not args.event_id:
            parser.error("--budget-summary requires --event-id")
        do_budget_summary(args.event_id)
    elif args.add_note:
        if not args.event_id or not args.note:
            parser.error("--add-note requires --event-id and --note")
        do_add_note(args.event_id, args.note)
    elif args.guests:
        if not args.event_id:
            parser.error("--guests requires --event-id")
        do_guests(args.event_id)
    elif args.rsvp:
        if not args.event_id or not args.guest or not args.rsvp_status:
            parser.error("--rsvp requires --event-id, --guest, and --rsvp-status")
        do_rsvp(args.event_id, args.guest, args.rsvp_status, args.headcount, args.note)
    elif args.rsvp_summary:
        if not args.event_id:
            parser.error("--rsvp-summary requires --event-id")
        do_rsvp_summary(args.event_id)
    elif args.rsvp_pending:
        if not args.event_id:
            parser.error("--rsvp-pending requires --event-id")
        do_rsvp_pending(args.event_id)
    elif args.set_rsvp_fields:
        if not args.event_id or not args.fields:
            parser.error("--set-rsvp-fields requires --event-id and --fields")
        do_set_rsvp_fields(args.event_id, args.fields)
    elif args.set_rsvp_deadline:
        if not args.event_id or not args.deadline:
            parser.error("--set-rsvp-deadline requires --event-id and --deadline")
        do_set_rsvp_deadline(args.event_id, args.deadline)
    elif args.set_event_description:
        if not args.event_id or not args.description:
            parser.error("--set-event-description requires --event-id and --description")
        do_set_event_description(args.event_id, args.description)


if __name__ == "__main__":
    main()

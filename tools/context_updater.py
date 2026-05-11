#!/usr/bin/env python3
"""
context_updater.py — Homer's context file update tool.

Handles the incremental context update flow:
  1. Writes a proposed update to the correct context file and section
  2. Commits the change to git
  3. Rebuilds MEMORY.md so nanobot picks up the change immediately

Context files:
  household       → context/household.md        (people, preferences, location)
  property        → context/property.md         (home systems, maintenance log)
  projects        → context/projects.md         (active projects)
  finance         → context/finance.md          (accounts, budget)
  health          → context/health.md           (appointments, providers)

Usage:
    python tools/context_updater.py \
        --file property \
        --section "HVAC" \
        --key "Filter size" \
        --value "20x25x1" \
        --source "user via Telegram"

    python tools/context_updater.py \
        --file property \
        --section "Maintenance Log" \
        --append-row "2026-03-08|HVAC|Filter replacement|DIY|20x25x1 Filtrete" \
        --source "user via Telegram"
"""

import argparse
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent.resolve()
# Allow env-var overrides so subprocess invocations (e.g. from the simulation
# harness or a containerized hosted instance) can redirect context reads/writes
# to an isolated directory without code changes.
CONTEXT_DIR = Path(os.environ.get("HOMER_CONTEXT_DIR") or (REPO_ROOT / "context"))
USER_CONTEXT_DIR = Path(
    os.environ.get("HOMER_USER_CONTEXT_DIR") or (CONTEXT_DIR / "user_context")
)

VALID_FILES = ["household", "property", "projects", "finance", "health"]


def get_context_file(name: str, *, for_write: bool = False) -> Path:
    """Resolve context file path.

    Reads: check user_context/ first, fall back to context/ root.
    Writes: always target user_context/ (canonical location).
    """
    if name not in VALID_FILES:
        raise ValueError(f"Invalid file '{name}'. Valid options: {VALID_FILES}")
    new_path = USER_CONTEXT_DIR / f"{name}.md"
    old_path = CONTEXT_DIR / f"{name}.md"

    if for_write:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        return new_path

    # For reads: prefer new location, fall back to old
    if new_path.exists():
        return new_path
    if old_path.exists():
        return old_path
    # Default to new location for missing files
    new_path.parent.mkdir(parents=True, exist_ok=True)
    return new_path


def read_context(file_path: Path) -> str:
    if not file_path.exists():
        raise FileNotFoundError(f"Context file not found: {file_path}")
    return file_path.read_text(encoding="utf-8")


def write_context(file_path: Path, content: str) -> None:
    file_path.write_text(content, encoding="utf-8")


def rebuild_memory() -> None:
    """Rebuild MEMORY.md so nanobot picks up the change immediately."""
    build_script = REPO_ROOT / "tools" / "build_context.py"
    subprocess.run(["python3", str(build_script)], cwd=REPO_ROOT, check=True, capture_output=True)


def update_timestamp(content: str) -> str:
    """Update the 'Last updated' line in the header."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return re.sub(
        r"# Last updated: .*",
        f"# Last updated: {now}",
        content,
    )


def update_key_value(content: str, section: str, subsection: str | None, key: str, value: str) -> str:
    """
    Replace a placeholder or existing value for a key within a section.

    Looks for patterns like:
        - **Key**: [FILL: ...]
        - **Key**: existing value
    and replaces the value portion.
    """
    # Build a regex that matches "- **key**: anything" in the target section
    pattern = rf"(- \*\*{re.escape(key)}\*\*: )(.+)"
    replacement = rf"\g<1>{value}"
    new_content, count = re.subn(pattern, replacement, content, count=1)
    if count == 0:
        # Key not found — append under the section/subsection
        new_content = append_under_section(content, section, subsection, f"- **{key}**: {value}")
    return new_content


def append_under_section(content: str, section: str, subsection: str | None, line: str) -> str:
    """
    Append a new line under a section (and optionally subsection) header.
    Inserts before the next same-level header.
    """
    target = f"### {subsection}" if subsection else f"## {section}"
    lines = content.splitlines(keepends=True)
    insert_at = None
    in_target = False

    for i, l in enumerate(lines):
        if l.strip() == target:
            in_target = True
            continue
        if in_target:
            # Stop at the next header of the same or higher level
            if re.match(r"^#{1,3} ", l) and l.strip() != target:
                insert_at = i
                break

    if insert_at is None and in_target:
        insert_at = len(lines)  # append at end of file

    if insert_at is None:
        # Section not found — append at end of file with a new subsection
        lines.append(f"\n{target}\n{line}\n")
    else:
        lines.insert(insert_at, line + "\n")

    return "".join(lines)


def append_table_row(content: str, section: str, row_pipe_delimited: str) -> str:
    """
    Append a row to a markdown table in the given section.
    row_pipe_delimited: "col1|col2|col3|col4|col5"
    """
    cols = [c.strip() for c in row_pipe_delimited.split("|")]
    row_md = "| " + " | ".join(cols) + " |"

    # Find the section and insert before the next section header or end of section
    section_header = f"## {section}"
    lines = content.splitlines(keepends=True)
    in_section = False
    last_table_row = None

    for i, l in enumerate(lines):
        if l.strip() == section_header:
            in_section = True
            continue
        if in_section:
            if l.strip().startswith("|"):
                last_table_row = i
            if re.match(r"^## ", l) and l.strip() != section_header:
                break

    if last_table_row is not None:
        lines.insert(last_table_row + 1, row_md + "\n")
    else:
        # No table found — fall back to appending under section
        return append_under_section(content, section, None, row_md)

    return "".join(lines)



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update a Homer context file, commit to git, rebuild MEMORY.md."
    )
    parser.add_argument("--file", required=True,
                        help=f"Context file to update. Options: {VALID_FILES}")
    parser.add_argument("--section", required=True, help="Section header (e.g. 'HVAC')")
    parser.add_argument("--subsection", default=None, help="Subsection header if needed")
    parser.add_argument("--key", default=None, help="Key to update (e.g. 'Filter size')")
    parser.add_argument("--value", default=None, help="New value for the key")
    parser.add_argument("--append-row", default=None, dest="append_row",
                        help="Pipe-delimited row to append to a table")
    parser.add_argument("--source", default="Homer",
                        help="Source of the update for git commit message")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the updated file without writing or committing")

    args = parser.parse_args()

    if not args.key and not args.append_row:
        parser.error("Provide either --key + --value or --append-row")
    if args.key and not args.value:
        parser.error("--key requires --value")

    read_path = get_context_file(args.file)
    content = read_context(read_path)

    if args.key and args.value:
        content = update_key_value(content, args.section, args.subsection, args.key, args.value)
        commit_msg = f"Homer update: {args.file}/{args.section} → {args.key} ({args.source})"
    else:
        content = append_table_row(content, args.section, args.append_row)
        commit_msg = f"Homer update: {args.file}/{args.section} log entry ({args.source})"

    content = update_timestamp(content)

    if args.dry_run:
        print("=== DRY RUN — not writing file ===")
        print(content)
        return

    write_path = get_context_file(args.file, for_write=True)
    write_context(write_path, content)

    # Clean up old-location file after successful write to user_context/
    old_path = CONTEXT_DIR / f"{args.file}.md"
    if old_path != write_path:
        try:
            old_path.unlink(missing_ok=True)
        except PermissionError:
            pass  # best-effort cleanup

    rebuild_memory()
    print(f"✓ {args.file}.md updated, MEMORY.md rebuilt: {commit_msg}")


if __name__ == "__main__":
    main()

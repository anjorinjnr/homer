#!/usr/bin/env python3
"""
sync_from_drive.py — Sync Homer's context files from a tabbed Google Doc.

Each tab in the Google Doc maps to a local context/*.md file. On sync, tab
content is converted to markdown and written to disk. This makes Google Docs
the editing interface while keeping local .md files as the operational truth.

Setup:
    1. Create a Google Doc with tabs. Tab names drive filenames automatically:
         "Household" → context/household.md
         "Property"       → context/property.md
       Name tabs however you like — lowercase + spaces→underscores + .md
    2. Copy the doc ID from the URL: docs.google.com/document/d/<DOC_ID>/edit
    3. Set HOMER_CONTEXT_DOC_ID in secrets/.env
    4. Enable the Google Docs API in GCP Console (same project as Drive)
    5. Re-run google_auth.py (new scopes: documents.readonly, calendar)

Usage:
    python tools/sync_from_drive.py              # sync all tabs
    python tools/sync_from_drive.py --tab "Household Core"  # sync one tab
    python tools/sync_from_drive.py --dry-run    # print output, don't write
    python tools/sync_from_drive.py --list-tabs  # list tabs + derived filenames
    python tools/sync_from_drive.py --build      # sync + rebuild workspace
"""

import argparse
import os
import pickle
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOKEN_FILE = REPO_ROOT / "secrets" / "google_token.pickle"
CONTEXT_DIR = REPO_ROOT / "context"


def tab_title_to_filename(title: str) -> str:
    """Derive context filename from tab title.

    Convention: tab title lowercased, spaces → underscores, + .md
    Examples:
      "Household" → household.md
      "Property"       → property.md
      "Finance"        → finance.md
    """
    return title.strip().lower().replace(" ", "_") + ".md"


HEADING_PREFIX = {
    "HEADING_1": "#",
    "HEADING_2": "##",
    "HEADING_3": "###",
    "HEADING_4": "####",
    "HEADING_5": "#####",
    "HEADING_6": "######",
}


def load_doc_id() -> str:
    doc_id = os.environ.get("HOMER_CONTEXT_DOC_ID")
    if not doc_id:
        print("Error: HOMER_CONTEXT_DOC_ID not set in environment.")
        sys.exit(1)
    return doc_id


def get_creds():
    try:
        from google.auth.transport.requests import Request
    except ImportError:
        print("Missing deps. Run: pip install google-auth google-api-python-client")
        sys.exit(1)

    if not TOKEN_FILE.exists():
        print(f"Token not found at {TOKEN_FILE}. Run: python tools/google_auth.py")
        sys.exit(1)

    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return creds


def get_docs_service():
    from googleapiclient.discovery import build
    return build("docs", "v1", credentials=get_creds())


def _inline_text(pe: dict) -> str:
    """Convert a ParagraphElement to formatted inline text."""
    # Rich link / smart chip (e.g. linked Google Sheet)
    rich = pe.get("richLink")
    if rich:
        props = rich.get("richLinkProperties", {})
        title = props.get("title", "")
        uri = props.get("uri", "")
        if title and uri:
            return f"[{title}]({uri})"
        return title or uri

    run = pe.get("textRun")
    if not run:
        return ""
    text = run.get("content", "").rstrip("\n")
    if not text:
        return ""
    ts = run.get("textStyle", {})
    bold = ts.get("bold", False)
    italic = ts.get("italic", False)
    link = ts.get("link", {}).get("url") if ts.get("link") else None

    if link:
        text = f"[{text}]({link})"
    elif bold and italic:
        text = f"***{text}***"
    elif bold:
        text = f"**{text}**"
    elif italic:
        text = f"*{text}*"
    return text


def _table_to_markdown(table: dict) -> list[str]:
    """Convert a Docs API table to markdown table rows."""
    rows = table.get("tableRows", [])
    if not rows:
        return []

    md_rows = []
    for i, row in enumerate(rows):
        cells = []
        for cell in row.get("tableCells", []):
            # Flatten all text from cell paragraphs
            parts = []
            for el in cell.get("content", []):
                para = el.get("paragraph")
                if para:
                    parts.append("".join(_inline_text(pe) for pe in para.get("elements", [])).strip())
            cells.append(" ".join(p for p in parts if p))
        md_rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            md_rows.append("| " + " | ".join("---" for _ in cells) + " |")

    return md_rows


def docs_content_to_markdown(content: list) -> str:
    """Convert Docs API body content array to a markdown string."""
    lines = []

    for element in content:
        if "paragraph" in element:
            para = element["paragraph"]
            style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
            bullet = para.get("bullet")

            line = "".join(_inline_text(pe) for pe in para.get("elements", [])).rstrip()

            if not line:
                lines.append("")
                continue

            if style in HEADING_PREFIX:
                lines.append(f"{HEADING_PREFIX[style]} {line}")
            elif bullet is not None:
                nesting = bullet.get("nestingLevel", 0)
                indent = "  " * nesting
                lines.append(f"{indent}- {line}")
            else:
                lines.append(line)

        elif "table" in element:
            lines.extend(_table_to_markdown(element["table"]))
            lines.append("")

    # Collapse consecutive blank lines into one
    result = []
    prev_blank = False
    for line in lines:
        if line == "":
            if not prev_blank:
                result.append("")
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False

    return "\n".join(result).strip()


def list_tabs(doc: dict) -> list[dict]:
    """Return a flat list of tab info dicts from the document."""
    tabs = []
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        tabs.append({
            "id": props.get("tabId", ""),
            "title": props.get("title", "Untitled"),
            "index": props.get("index", 0),
        })
        for child in tab.get("childTabs", []):
            cprops = child.get("tabProperties", {})
            tabs.append({
                "id": cprops.get("tabId", ""),
                "title": cprops.get("title", "Untitled"),
                "index": cprops.get("index", 0),
                "parent": props.get("title", ""),
            })
    return tabs


def sync_tab(doc: dict, tab_title: str, dest_file: Path, dry_run: bool) -> bool:
    """Find a tab by title, convert to markdown, write to dest_file."""
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title", "").strip().lower() == tab_title.strip().lower():
            content = tab.get("documentTab", {}).get("body", {}).get("content", [])
            md = docs_content_to_markdown(content)
            if not md:
                print(f"  – {tab_title} skipped (empty)")
                return False
            if dry_run:
                print(f"\n{'─'*60}")
                print(f"Tab: {tab_title}  →  {dest_file.name}")
                print('─'*60)
                print(md)
            else:
                dest_file.write_text(md + "\n")
                print(f"  ✓ {tab_title} → context/{dest_file.name} ({len(md):,} chars)")
            return True

    print(f"  ✗ Tab '{tab_title}' not found in document")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Sync Homer context files from a tabbed Google Doc."
    )
    parser.add_argument("--tab", help="Sync only this tab (by title as it appears in the doc)")
    parser.add_argument("--dry-run", action="store_true", help="Print output without writing files")
    parser.add_argument("--list-tabs", action="store_true", help="List all tabs in the doc and exit")
    parser.add_argument("--build", action="store_true", help="Run build_context.py after sync")
    args = parser.parse_args()

    doc_id = load_doc_id()
    service = get_docs_service()

    print(f"Fetching document {doc_id} ...")
    doc = service.documents().get(
        documentId=doc_id,
        includeTabsContent=True,
    ).execute()
    print(f"Document: {doc.get('title', 'Untitled')}")

    if args.list_tabs:
        tabs = list_tabs(doc)
        print(f"\nTabs ({len(tabs)}):")
        for t in tabs:
            parent = f"  (child of '{t['parent']}')" if t.get("parent") else ""
            filename = tab_title_to_filename(t["title"])
            exists = "✓" if (CONTEXT_DIR / filename).exists() else "new"
            print(f"  [{t['index']}] {t['title']}  → context/{filename} [{exists}]{parent}")
        return

    tabs = list_tabs(doc)

    if args.tab:
        # Filter to the single requested tab
        match = next((t for t in tabs if t["title"].lower() == args.tab.lower()), None)
        if not match:
            available = ", ".join(t["title"] for t in tabs)
            print(f"Tab '{args.tab}' not found. Available: {available}")
            sys.exit(1)
        to_sync = [match]
    else:
        to_sync = tabs

    print(f"\nSyncing {len(to_sync)} tab(s){' [dry-run]' if args.dry_run else ''} ...")
    success = 0
    for t in to_sync:
        filename = tab_title_to_filename(t["title"])
        if sync_tab(doc, t["title"], CONTEXT_DIR / filename, args.dry_run):
            success += 1

    if args.dry_run:
        print(f"\n[dry-run] Would write {success}/{len(to_sync)} files")
        return

    print(f"\nSynced {success}/{len(to_sync)} files")

    if args.build and success > 0:
        print("\nRebuilding workspace ...")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "build_context.py")],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("  ✓ Workspace rebuilt")
        else:
            print(f"  ✗ build_context.py failed:\n{result.stderr}")
            sys.exit(1)


if __name__ == "__main__":
    main()

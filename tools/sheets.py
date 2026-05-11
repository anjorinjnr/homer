#!/usr/bin/env python3
"""
sheets.py — Google Sheets read and write access for Homer.

Usage:
    python tools/sheets.py --mode create --title "Family Budget 2026" --sheets "Expenses,Income"
    python tools/sheets.py --mode info   --sheet-id <id>
    python tools/sheets.py --mode read   --sheet-id <id> --range "Sheet1!A1:D20"
    python tools/sheets.py --mode read   --sheet-id <id> --range "Expenses"      # whole sheet
    python tools/sheets.py --mode write  --sheet-id <id> --range "Sheet1!B2" --values '[["Done"]]'
    python tools/sheets.py --mode append --sheet-id <id> --sheet "Expenses" --values '[["2026-03-16","Groceries","150.00"]]'
    python tools/sheets.py --mode append --sheet-id <id> --sheet "Expenses" --values-file /path/to/values.json
    python tools/sheets.py --mode note   --sheet-id <id> --range "Sheet1!B2" --note "Needs review"
    python tools/sheets.py --mode read   --sheet-id <id> --range "A1:C5" --account personal

The sheet ID is the long string in the URL:
    docs.google.com/spreadsheets/d/<SHEET_ID>/edit

Output (JSON):
    create: { "sheet_id", "url", "title", "sheets": ["Tab1", "Tab2"] }
    info:   { "title", "sheets": [{ "name", "rows", "cols" }] }
    read:   { "range", "values": [[row], [row], ...] }
    write:  { "updated_range", "updated_rows", "updated_cols", "updated_cells" }
    append: { "updated_range", "updated_rows", "updated_cells" }
    note:   { "status": "success", "range", "note" }
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from google_auth import DEFAULT_ACCOUNT, load_google_credentials

REPO_ROOT  = Path(__file__).parent.parent.resolve()
ALLOWED_VALUES_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "tmp"

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def get_service(account: str = DEFAULT_ACCOUNT):
    try:
        from googleapiclient.discovery import build
        creds = load_google_credentials(account)
    except ImportError:
        print(json.dumps({"error": "Missing deps. Run: pip install google-auth google-api-python-client"}))
        sys.exit(1)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    if SHEETS_SCOPE not in (creds.scopes or []):
        print(json.dumps({"error": f"Token for '{account}' missing spreadsheets scope. Re-run: python tools/google_auth.py --account {account}"}))
        sys.exit(1)

    return build("sheets", "v4", credentials=creds)


def do_create(title: str, service, tab_names: list[str] = None) -> None:
    sheets_body = []
    for i, name in enumerate(tab_names or ["Sheet1"]):
        sheets_body.append({"properties": {"title": name, "index": i}})

    body = {
        "properties": {"title": title},
        "sheets": sheets_body,
    }
    try:
        result = service.spreadsheets().create(body=body).execute()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps({
        "sheet_id": result.get("spreadsheetId", ""),
        "url": result.get("spreadsheetUrl", ""),
        "title": result.get("properties", {}).get("title", ""),
        "sheets": [s["properties"]["title"] for s in result.get("sheets", [])],
    }, indent=2))


def do_info(sheet_id: str, service) -> None:
    try:
        result = service.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="properties.title,sheets(properties(title,gridProperties))",
        ).execute()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    sheets = []
    for s in result.get("sheets", []):
        props = s.get("properties", {})
        grid = props.get("gridProperties", {})
        sheets.append({
            "name": props.get("title", ""),
            "rows": grid.get("rowCount"),
            "cols": grid.get("columnCount"),
        })

    print(json.dumps({
        "title": result.get("properties", {}).get("title", ""),
        "sheets": sheets,
    }, indent=2))


def do_read(sheet_id: str, range_: str, service) -> None:
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_,
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps({
        "range": result.get("range", range_),
        "values": result.get("values", []),
    }, indent=2))


def do_write(sheet_id: str, range_: str, values: list, service) -> None:
    body = {"values": values}
    try:
        result = service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_,
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(json.dumps({
        "updated_range": result.get("updatedRange", ""),
        "updated_rows": result.get("updatedRows", 0),
        "updated_cols": result.get("updatedColumns", 0),
        "updated_cells": result.get("updatedCells", 0),
    }, indent=2))


def do_append(sheet_id: str, sheet: str, values: list, service) -> None:
    body = {"values": values}
    try:
        result = service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=sheet,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    updates = result.get("updates", {})
    print(json.dumps({
        "updated_range": updates.get("updatedRange", ""),
        "updated_rows": updates.get("updatedRows", 0),
        "updated_cells": updates.get("updatedCells", 0),
    }, indent=2))


def do_note(sheet_id: str, range_: str, note: str, service) -> None:
    match = re.match(r"^(?:'?(.*?)'?!)?([A-Za-z]+)([0-9]+)(?::([A-Za-z]+)([0-9]+))?$", range_)
    if not match:
        print(json.dumps({"error": f"Invalid A1 range: {range_}"}))
        sys.exit(1)
        
    sheet_name, start_col_str, start_row_str, end_col_str, end_row_str = match.groups()
    
    try:
        info = service.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets(properties(sheetId,title))").execute()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
        
    target_sheet_id = None
    for s in info.get("sheets", []):
        props = s.get("properties", {})
        if sheet_name:
            if props.get("title") == sheet_name:
                target_sheet_id = props.get("sheetId")
                break
        else:
            target_sheet_id = props.get("sheetId")
            break
            
    if target_sheet_id is None:
        print(json.dumps({"error": f"Sheet '{sheet_name}' not found"}))
        sys.exit(1)
        
    def col_to_num(col):
        num = 0
        for c in col:
            num = num * 26 + (ord(c.upper()) - ord('A')) + 1
        return num - 1
        
    start_col = col_to_num(start_col_str)
    start_row = int(start_row_str) - 1
    
    end_col = col_to_num(end_col_str) + 1 if end_col_str else start_col + 1
    end_row = int(end_row_str) if end_row_str else start_row + 1
    
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": target_sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col
            },
            "cell": {"note": note},
            "fields": "note"
        }
    }]
    
    body = {"requests": requests}
    try:
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
        print(json.dumps({"status": "success", "range": range_, "note": note}, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Sheets read/write for Homer.")
    parser.add_argument("--mode", required=True, choices=["create", "info", "read", "write", "append", "note"])
    parser.add_argument("--sheet-id", help="Spreadsheet ID from the URL (not required for create)")
    parser.add_argument("--title", help="Spreadsheet title (create mode)")
    parser.add_argument("--sheets", help="Comma-separated tab names (create mode, default: Sheet1)")
    parser.add_argument("--range", dest="range_", help="A1 notation range (read/write/note modes)")
    parser.add_argument("--sheet", help="Sheet/tab name (append mode)")
    parser.add_argument("--values", help="JSON 2D array of values e.g. '[[\"a\",\"b\"],[\"c\",\"d\"]]'")
    parser.add_argument("--values-file", help="Path to a JSON file containing a 2D array of values (use instead of --values to avoid shell quoting issues)")
    parser.add_argument("--note", help="Text for the cell note (note mode)")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to use (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    # --values-file overrides --values (avoids shell metacharacter issues with JSON arrays)
    if args.values_file:
        vf = Path(args.values_file).resolve()
        if not vf.is_relative_to(ALLOWED_VALUES_DIR.resolve()):
            print(json.dumps({"error": f"--values-file must be inside {ALLOWED_VALUES_DIR}"}))
            sys.exit(1)
        if not vf.exists():
            print(json.dumps({"error": f"Values file not found: {args.values_file}"}))
            sys.exit(1)
        try:
            raw = vf.read_text().strip()
        except OSError as e:
            print(json.dumps({"error": f"Cannot read values file: {e}"}))
            sys.exit(1)
        try:
            json.loads(raw)  # validate JSON before passing downstream
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON in values file: {e}"}))
            sys.exit(1)
        args.values = raw

    service = get_service(args.account)

    if args.mode == "create":
        if not args.title:
            print(json.dumps({"error": "--title is required for create mode"}))
            sys.exit(1)
        tab_names = [t.strip() for t in args.sheets.split(",")] if args.sheets else None
        do_create(args.title, service, tab_names)

    elif args.mode == "info":
        if not args.sheet_id:
            print(json.dumps({"error": "--sheet-id is required for info mode"}))
            sys.exit(1)
        do_info(args.sheet_id, service)

    elif args.mode == "read":
        if not args.sheet_id:
            print(json.dumps({"error": "--sheet-id is required for read mode"}))
            sys.exit(1)
        if not args.range_:
            print(json.dumps({"error": "--range is required for read mode"}))
            sys.exit(1)
        do_read(args.sheet_id, args.range_, service)

    elif args.mode == "write":
        if not args.sheet_id:
            print(json.dumps({"error": "--sheet-id is required for write mode"}))
            sys.exit(1)
        if not args.range_:
            print(json.dumps({"error": "--range is required for write mode"}))
            sys.exit(1)
        if not args.values:
            print(json.dumps({"error": "--values is required for write mode"}))
            sys.exit(1)
        try:
            values = json.loads(args.values)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON for --values: {e}"}))
            sys.exit(1)
        do_write(args.sheet_id, args.range_, values, service)

    elif args.mode == "append":
        if not args.sheet_id:
            print(json.dumps({"error": "--sheet-id is required for append mode"}))
            sys.exit(1)
        if not args.sheet:
            print(json.dumps({"error": "--sheet is required for append mode"}))
            sys.exit(1)
        if not args.values:
            print(json.dumps({"error": "--values is required for append mode"}))
            sys.exit(1)
        try:
            values = json.loads(args.values)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON for --values: {e}"}))
            sys.exit(1)
        do_append(args.sheet_id, args.sheet, values, service)

    elif args.mode == "note":
        if not args.sheet_id:
            print(json.dumps({"error": "--sheet-id is required for note mode"}))
            sys.exit(1)
        if not args.range_:
            print(json.dumps({"error": "--range is required for note mode"}))
            sys.exit(1)
        if args.note is None:
            print(json.dumps({"error": "--note is required for note mode"}))
            sys.exit(1)
        do_note(args.sheet_id, args.range_, args.note, service)


if __name__ == "__main__":
    main()

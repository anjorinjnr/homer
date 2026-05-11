#!/usr/bin/env python3
"""
drive_fetch.py — Sync Google Drive family_docs index via gogcli.

Builds a metadata-only index of files in the family_docs/ Drive folder.
No file content is downloaded or stored — only names, IDs, and folder paths.
Files are fetched on demand and deleted immediately after use.

Usage:
    python tools/drive_fetch.py --sync                    # rebuild index from Drive
    python tools/drive_fetch.py --sync --force            # force even if synced today
    python tools/drive_fetch.py --list                    # print current index
    python tools/drive_fetch.py --sync --account personal # index a different account

Index written to:
    context/.nanobot_workspace/state/drive_index.json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import DEFAULT_ACCOUNT, load_google_credentials, require_scopes

REPO_ROOT = Path(__file__).parent.parent.resolve()
INDEX_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "drive_index.json"
LAST_SYNC_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "drive_last_sync.txt"

ROOT_FOLDER_NAME = "family_docs"
LOCAL_TZ = ZoneInfo("America/New_York")
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

# Mime types we care about
SUPPORTED_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.google-apps.spreadsheet": "google_sheet",
    "application/vnd.google-apps.document": "google_doc",
    "image/jpeg": "image",
    "image/png": "image",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


def get_access_token(account: str) -> str:
    creds = load_google_credentials(account)
    require_scopes(creds, account, DRIVE_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


def already_synced_today() -> bool:
    if not LAST_SYNC_FILE.exists():
        return False
    ts = LAST_SYNC_FILE.read_text().strip()
    try:
        last = datetime.fromisoformat(ts).astimezone(LOCAL_TZ)
        return last.date() == datetime.now(LOCAL_TZ).date()
    except ValueError:
        return False


def save_last_sync() -> None:
    LAST_SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SYNC_FILE.write_text(datetime.now(LOCAL_TZ).isoformat())


def find_root_folder(token: str) -> str | None:
    """Find the family_docs folder ID in Drive root."""
    query = f"name='{ROOT_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    result = gogcli.run(token, "drive", "ls", "--query", query, "--max", "5")
    files = result.get("files", [])
    if not files:
        return None
    return files[0]["id"]


def walk_folder(token: str, folder_id: str, folder_path: str) -> list[dict]:
    """Recursively list all supported files in a Drive folder."""
    files = []
    page_token = ""

    while True:
        args = ["drive", "ls", "--parent", folder_id, "--max", "100"]
        if page_token:
            args.extend(["--page", page_token])
            
        result = gogcli.run(token, *args)

        for f in result.get("files", []):
            mime = f.get("mimeType", "")
            name = f.get("name", "")

            if mime == "application/vnd.google-apps.folder":
                # Recurse into subfolder
                sub_path = f"{folder_path}/{name}"
                files.extend(walk_folder(token, f["id"], sub_path))
            elif mime in SUPPORTED_TYPES:
                files.append({
                    "id": f["id"],
                    "name": name,
                    "folder": folder_path,
                    "path": f"{folder_path}/{name}",
                    "type": SUPPORTED_TYPES[mime],
                    "mime_type": mime,
                    "modified": f.get("modifiedTime", ""),
                    "size_bytes": int(f.get("size", 0)) if f.get("size") else None,
                })

        page_token = result.get("nextPageToken", "")
        if not page_token:
            break

    return files


def sync(force: bool = False, account: str = DEFAULT_ACCOUNT) -> None:
    if not force and already_synced_today():
        print("SKIP: Drive index already synced today (use --force to override)")
        return

    try:
        token = get_access_token(account)
    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    print(f"Looking for '{ROOT_FOLDER_NAME}' in Drive...")
    root_id = find_root_folder(token)

    if not root_id:
        print(f"WARNING: '{ROOT_FOLDER_NAME}' folder not found in Drive.")
        print("  Create it in Google Drive and re-run to build the index.")
        # Write empty index so Homer knows sync ran
        index = {
            "synced_at": datetime.now(LOCAL_TZ).isoformat(),
            "root_folder": ROOT_FOLDER_NAME,
            "root_found": False,
            "files": [],
        }
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(index, indent=2))
        save_last_sync()
        return

    print(f"Found '{ROOT_FOLDER_NAME}' — walking folder tree...")
    files = walk_folder(token, root_id, ROOT_FOLDER_NAME)

    index = {
        "synced_at": datetime.now(LOCAL_TZ).isoformat(),
        "root_folder": ROOT_FOLDER_NAME,
        "root_found": True,
        "file_count": len(files),
        "files": files,
    }

    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2))
    INDEX_FILE.chmod(0o600)
    save_last_sync()

    print(f"✓ Drive index updated: {len(files)} file(s) in {ROOT_FOLDER_NAME}/")

    # Print folder summary
    folders: dict[str, int] = {}
    for f in files:
        folders[f["folder"]] = folders.get(f["folder"], 0) + 1
    for folder, count in sorted(folders.items()):
        print(f"  {folder}/  ({count} file(s))")


def list_index() -> None:
    if not INDEX_FILE.exists():
        print("No index found. Run: python tools/drive_fetch.py --sync")
        return

    index = json.loads(INDEX_FILE.read_text())
    synced_at = index.get("synced_at", "unknown")
    files = index.get("files", [])

    print(f"Drive index — synced {synced_at}")
    print(f"Root: {index.get('root_folder')}/ ({'found' if index.get('root_found') else 'NOT FOUND'})")
    print(f"Files: {len(files)}\n")

    if not files:
        print("  (empty)")
        return

    current_folder = None
    for f in sorted(files, key=lambda x: (x["folder"], x["name"])):
        if f["folder"] != current_folder:
            current_folder = f["folder"]
            print(f"  {current_folder}/")
        size = f"{f['size_bytes'] // 1024}KB" if f["size_bytes"] else "—"
        print(f"    [{f['type']:12}]  {f['name']}  ({size})")

    # Output as JSON for Homer
    print("\n" + json.dumps(index))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Google Drive family_docs index.")
    parser.add_argument("--sync", action="store_true", help="Rebuild index from Drive")
    parser.add_argument("--list", action="store_true", help="Print current index")
    parser.add_argument("--force", action="store_true", help="Force sync even if ran today")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to index (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    if not any([args.sync, args.list]):
        parser.print_help()
        sys.exit(1)

    if args.sync:
        sync(force=args.force, account=args.account)
    elif args.list:
        list_index()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
restore_backup.py — List and download context backups from Google Drive.

Usage:
    python tools/restore_backup.py --list
    python tools/restore_backup.py --download latest
    python tools/restore_backup.py --download homer_backup_2026-03-30.zip
    python tools/restore_backup.py --download latest --output restore_20250101/

Output (JSON):
    --list:     {"backups": [{"name": "...", "id": "...", "created": "...", "size_mb": ...}, ...]}
    --download: {"status": "ok", "file": "...", "extracted_to": "/tmp/homer_restore/", "files": [...]}

Downloaded backups are extracted for review — NOT auto-applied to live context.
The user reviews extracted files and copies what they need manually.

Requires HOMER_BACKUP_FOLDER_ID env var (Google Drive folder ID).
"""

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOKEN_FILE = REPO_ROOT / "secrets" / "google_token.pickle"
WORKSPACE_DIR = Path(os.environ.get("HOMER_WORKSPACE",
                     REPO_ROOT / "context" / ".nanobot_workspace")).resolve()
SAFE_RESTORE_DIR = WORKSPACE_DIR / "tmp" / "restore"

DEFAULT_OUTPUT_DIR = str(SAFE_RESTORE_DIR)


def get_drive_service():
    try:
        import pickle
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        print(json.dumps({"error": "Missing deps: pip install google-auth google-api-python-client"}))
        sys.exit(1)

    if not TOKEN_FILE.exists():
        print(json.dumps({"error": f"Token not found at {TOKEN_FILE}. Run: python tools/google_auth.py"}))
        sys.exit(1)

    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)


def list_backups(service, folder_id: str) -> list[dict]:
    """List all backups in the Drive folder, newest first."""
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime desc",
        fields="files(id,name,createdTime,size)",
        pageSize=100,
    ).execute()
    files = results.get("files", [])
    return [
        {
            "name": f["name"],
            "id": f["id"],
            "created": f.get("createdTime", ""),
            "size_mb": round(int(f.get("size", 0)) / (1024 * 1024), 2),
        }
        for f in files
    ]


def download_file(service, file_id: str, dest_path: str) -> str:
    """Download a file from Drive to a local path."""
    from googleapiclient.http import MediaIoBaseDownload
    import io

    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest_path


def extract_backup(zip_path: str, output_dir: str) -> list[str]:
    """Extract a backup zip to output_dir, return list of extracted file paths."""
    import shutil

    out = Path(output_dir).resolve()

    # Safety: only allow rmtree inside a subdirectory of workspace/tmp/
    safe_parent = (WORKSPACE_DIR / "tmp").resolve()
    if not out.is_relative_to(safe_parent) or out == safe_parent:
        raise ValueError(f"Output dir must be a subdirectory of {safe_parent}, got {out}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Validate no path traversal in zip entries
        for name in zf.namelist():
            if name.startswith("/") or ".." in name:
                raise ValueError(f"Unsafe path in zip: {name}")
        zf.extractall(out)
        return sorted(zf.namelist())


def main():
    parser = argparse.ArgumentParser(description="List or restore context backups from Google Drive")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List available backups")
    group.add_argument("--download", metavar="NAME", help='Backup name to download, or "latest"')
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help=f"Extract directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--folder-id", default=None, help="Drive folder ID (overrides env)")
    args = parser.parse_args()

    folder_id = args.folder_id or os.environ.get("HOMER_BACKUP_FOLDER_ID")
    if not folder_id:
        print(json.dumps({"error": "HOMER_BACKUP_FOLDER_ID not set. Set it in .env or pass --folder-id."}))
        sys.exit(1)

    try:
        service = get_drive_service()

        if args.list:
            backups = list_backups(service, folder_id)
            print(json.dumps({"backups": backups}, indent=2))
            return

        # --download mode
        backups = list_backups(service, folder_id)
        if not backups:
            print(json.dumps({"error": "No backups found in the Drive folder."}))
            sys.exit(1)

        if args.download == "latest":
            target = backups[0]  # list is sorted newest first
        else:
            matches = [b for b in backups if b["name"] == args.download]
            if not matches:
                names = [b["name"] for b in backups]
                print(json.dumps({"error": f"Backup '{args.download}' not found. Available: {names}"}))
                sys.exit(1)
            target = matches[0]

        # Download to temp file, then extract
        tmp = NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.close()
        try:
            download_file(service, target["id"], tmp.name)
            extracted = extract_backup(tmp.name, args.output)
            print(json.dumps({
                "status": "ok",
                "file": target["name"],
                "extracted_to": args.output,
                "files": extracted,
            }, indent=2))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
export_context.py — Export Homer's context files to a shared Google Drive folder.

Uploads local context/*.md files to a Drive folder so household members
can read what Homer knows without accessing the codebase.

On re-export, existing files in the folder are updated in place (same URL).

Setup:
    1. Create a folder in Google Drive and share it with whoever needs access
    2. Copy the folder ID from the URL: drive.google.com/drive/folders/<FOLDER_ID>
    3. Set HOMER_EXPORT_FOLDER_ID in secrets/.env
    4. Re-run google_auth.py if needed (requires drive scope)

Usage:
    python tools/export_context.py              # export all context files
    python tools/export_context.py --file household   # export one file
    python tools/export_context.py --dry-run    # show what would be exported
"""

import argparse
import os
import pickle
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOKEN_FILE = REPO_ROOT / "secrets" / "google_token.pickle"
CONTEXT_DIR = REPO_ROOT / "context"
USER_CONTEXT_DIR = CONTEXT_DIR / "user_context"

CONTEXT_FILES = ["household", "property", "projects", "finance", "health"]


def load_folder_id() -> str:
    folder_id = os.environ.get("HOMER_EXPORT_FOLDER_ID")
    if not folder_id:
        print("Error: HOMER_EXPORT_FOLDER_ID not set in environment.")
        sys.exit(1)
    return folder_id


def get_drive_service():
    try:
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
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

    return build("drive", "v3", credentials=creds)


def find_existing_file(service, folder_id: str, filename: str) -> str | None:
    """Return file ID if a file with this name already exists in the folder."""
    safe = filename.replace("'", "\\'")
    result = service.files().list(
        q=f"name='{safe}' and '{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        pageSize=1,
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def export_file(service, folder_id: str, name: str, content: str, dry_run: bool) -> str | None:
    """Upload or update a file in the Drive folder. Returns the file URL."""
    from googleapiclient.http import MediaInMemoryUpload

    filename = f"{name}.md"
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)

    existing_id = find_existing_file(service, folder_id, filename)

    if dry_run:
        action = "update" if existing_id else "create"
        print(f"  – {filename} [{action}]")
        return None

    if existing_id:
        service.files().update(
            fileId=existing_id,
            media_body=media,
        ).execute()
        file_id = existing_id
        action = "updated"
    else:
        file_meta = {"name": filename, "parents": [folder_id]}
        result = service.files().create(
            body=file_meta,
            media_body=media,
            fields="id",
        ).execute()
        file_id = result["id"]
        action = "created"

    url = f"https://drive.google.com/file/d/{file_id}/view"
    print(f"  ✓ {filename} [{action}] — {url}")
    return url


def main():
    parser = argparse.ArgumentParser(description="Export Homer context files to Google Drive.")
    parser.add_argument("--file", help="Export only this file (e.g. household, property)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be exported without uploading")
    args = parser.parse_args()

    folder_id = load_folder_id()

    if args.file:
        if args.file not in CONTEXT_FILES:
            print(f"Unknown file '{args.file}'. Options: {', '.join(CONTEXT_FILES)}")
            sys.exit(1)
        to_export = [args.file]
    else:
        to_export = CONTEXT_FILES

    # Filter to files that exist locally
    def _resolve(name: str) -> Path:
        p = USER_CONTEXT_DIR / f"{name}.md"
        return p if p.exists() else CONTEXT_DIR / f"{name}.md"

    to_export = [f for f in to_export if _resolve(f).exists()]
    if not to_export:
        print("No context files found to export.")
        sys.exit(0)

    service = get_drive_service()
    print(f"Exporting {len(to_export)} file(s) to Drive folder {folder_id}{' [dry-run]' if args.dry_run else ''} ...\n")

    for name in to_export:
        content = _resolve(name).read_text(encoding="utf-8")
        export_file(service, folder_id, name, content, args.dry_run)


if __name__ == "__main__":
    main()

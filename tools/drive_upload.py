#!/usr/bin/env python3
"""
drive_upload.py — Upload a file to Google Drive and return a shareable link.

Usage:
    python tools/drive_upload.py --file /path/to/report.md
    python tools/drive_upload.py --content "Report text..." --name "report.md"
    python tools/drive_upload.py --file /path/to/file.md --folder-id <FOLDER_ID>
    python tools/drive_upload.py --file /path/to/report.md --account personal

Output (JSON):
    {"url": "https://drive.google.com/file/d/.../view", "name": "report.md"}

The uploaded file is shared as "anyone with the link can view" so the URL
can be sent directly to household members.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from google_auth import DEFAULT_ACCOUNT, build_service_or_exit


def load_default_folder_id() -> str | None:
    return os.environ.get("HOMER_EXPORT_FOLDER_ID")


def upload(service, name: str, content: bytes, mimetype: str, folder_id: str | None) -> dict:
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content, mimetype=mimetype, resumable=False)
    file_meta = {"name": name}
    if folder_id:
        file_meta["parents"] = [folder_id]

    result = service.files().create(
        body=file_meta,
        media_body=media,
        fields="id,name",
    ).execute()

    file_id = result["id"]

    # Make viewable by anyone with the link
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    url = f"https://drive.google.com/file/d/{file_id}/view"
    return {"url": url, "name": name}


def main():
    parser = argparse.ArgumentParser(description="Upload a file to Google Drive.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="Path to local file to upload")
    src.add_argument("--content", help="Text content to upload (use with --name)")
    parser.add_argument("--name", help="Filename for uploaded file (required with --content)")
    parser.add_argument("--folder-id", help="Drive folder ID (defaults to HOMER_EXPORT_FOLDER_ID)")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to upload to (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    if args.content and not args.name:
        print(json.dumps({"error": "--name is required when using --content"}))
        sys.exit(1)

    folder_id = args.folder_id or load_default_folder_id()

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(json.dumps({"error": f"File not found: {args.file}"}))
            sys.exit(1)
        content = path.read_bytes()
        name = args.name or path.name
        import mimetypes
        mimetype = mimetypes.guess_type(name)[0] or "text/plain"
    else:
        content = args.content.encode("utf-8")
        name = args.name
        mimetype = "text/plain"

    service = build_service_or_exit("drive", args.account)
    result = upload(service, name, content, mimetype, folder_id)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

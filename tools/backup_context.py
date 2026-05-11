#!/usr/bin/env python3
"""
backup_context.py — Zip and upload context/ to Google Drive for nightly backup.

Usage:
    python tools/backup_context.py                    # zip + upload + prune
    python tools/backup_context.py --dry-run          # zip only, no upload
    python tools/backup_context.py --retain 14        # keep 14 backups (default: 7)

Output (JSON):
    {"status": "ok", "file": "homer_backup_2026-03-30.zip", "size_mb": 0.12, "drive_id": "...", "pruned": 2}

Requires HOMER_BACKUP_FOLDER_ID env var (Google Drive folder ID).
Cron: 0 2 * * * (2:00 AM UTC daily).
"""

import argparse
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

REPO_ROOT = Path(__file__).parent.parent.resolve()
CONTEXT_DIR = REPO_ROOT / "context"
TOKEN_FILE = REPO_ROOT / "secrets" / "google_token.pickle"

# Exclude these from the zip — large, not critical, or regenerable
EXCLUDE_PATTERNS = [
    ".nanobot_workspace/sessions",
    ".guest_workspace/sessions",
    ".nanobot_workspace/skills",
    ".guest_workspace/skills",
    ".guest_workspace/TOOLS.md",
    ".nanobot_workspace/TOOLS.md",
]


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


def should_exclude(rel_path: str) -> bool:
    for pattern in EXCLUDE_PATTERNS:
        if rel_path.startswith(pattern):
            return True
    return False


def create_zip(context_dir: Path, output_path: str) -> tuple[str, float]:
    """Create a zip of context_dir, excluding sessions and generated files."""
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(context_dir):
            for f in files:
                full = Path(root) / f
                rel = str(full.relative_to(context_dir))
                if not should_exclude(rel):
                    zf.write(full, rel)
    size_mb = round(os.path.getsize(output_path) / (1024 * 1024), 2)
    return output_path, size_mb


def upload_backup(service, zip_path: str, folder_id: str) -> dict:
    from googleapiclient.http import MediaFileUpload

    name = os.path.basename(zip_path)
    media = MediaFileUpload(zip_path, mimetype="application/zip", resumable=True)
    file_meta = {"name": name, "parents": [folder_id]}
    result = service.files().create(
        body=file_meta, media_body=media, fields="id,name"
    ).execute()
    return {"id": result["id"], "name": result["name"]}


def prune_old_backups(service, folder_id: str, retain: int) -> int:
    """Delete oldest backups beyond the retain count."""
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime",
        fields="files(id,name,createdTime)",
        pageSize=100,
    ).execute()
    files = results.get("files", [])

    if len(files) <= retain:
        return 0

    to_delete = files[:len(files) - retain]
    for f in to_delete:
        service.files().delete(fileId=f["id"]).execute()
    return len(to_delete)


def main():
    parser = argparse.ArgumentParser(description="Backup context/ to Google Drive")
    parser.add_argument("--dry-run", action="store_true", help="Create zip but don't upload")
    parser.add_argument("--retain", type=int, default=7, help="Number of backups to keep (default: 7)")
    parser.add_argument("--folder-id", default=None, help="Drive folder ID (overrides env)")
    args = parser.parse_args()

    folder_id = args.folder_id or os.environ.get("HOMER_BACKUP_FOLDER_ID")
    if not folder_id and not args.dry_run:
        print(json.dumps({"error": "HOMER_BACKUP_FOLDER_ID not set. Set it in .env or pass --folder-id."}))
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    zip_name = f"homer_backup_{timestamp}.zip"

    tmp = NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        _, size_mb = create_zip(CONTEXT_DIR, tmp.name)

        if args.dry_run:
            print(json.dumps({"status": "dry_run", "file": zip_name, "size_mb": size_mb}))
            return

        service = get_drive_service()
        uploaded = upload_backup(service, tmp.name, folder_id)
        pruned = prune_old_backups(service, folder_id, args.retain)

        print(json.dumps({
            "status": "ok",
            "file": zip_name,
            "size_mb": size_mb,
            "drive_id": uploaded["id"],
            "pruned": pruned,
        }))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    main()

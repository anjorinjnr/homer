#!/usr/bin/env python3
"""
drive_download.py — Download a Google Drive file to the local tmp/ directory.

Downloads the raw file (or exports Google Docs/Sheets to text/CSV) and writes
it to {HOMER_WORKSPACE}/tmp/.

Usage:
    python tools/drive_download.py --query "budget spreadsheet"
    python tools/drive_download.py --file-id <drive_id>
    python tools/drive_download.py --url "https://docs.google.com/..."
    python tools/drive_download.py --path "family_docs/budget.csv"
    python tools/drive_download.py --query "receipt" --account personal

Output (JSON):
    {
        "name": "budget.csv",
        "local_path": "/opt/homer/context/.nanobot_workspace/tmp/budget.csv",
        "mime_type": "text/csv",
        "size_bytes": 4096
    }

The file is written to tmp/ — use local_path to access or process it further.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from google_auth import DEFAULT_ACCOUNT, build_service_or_exit

REPO_ROOT = Path(__file__).parent.parent.resolve()
INDEX_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "drive_index.json"
TMP_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "tmp"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
TMP_MAX_AGE_SECS = 24 * 60 * 60        # 24 hours

# MIME type → (ftype, export_mime, extension)
# export_mime is None for raw binary downloads
DOWNLOAD_MAP = {
    "application/vnd.google-apps.document": ("google_doc", "text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("google_sheet", "text/csv", ".csv"),
    "application/pdf": ("raw", None, ".pdf"),
    "text/csv": ("raw", None, ".csv"),
    "text/plain": ("raw", None, ".txt"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("raw", None, ".xlsx"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("raw", None, ".docx"),
    "application/msword": ("raw", None, ".doc"),
    "application/vnd.ms-word": ("raw", None, ".doc"),
    "image/jpeg": ("raw", None, ".jpg"),
    "image/png": ("raw", None, ".png"),
}


def extract_file_id_from_url(url: str) -> str | None:
    patterns = [
        r"/document/d/([a-zA-Z0-9_-]+)",
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/presentation/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/file/d/([a-zA-Z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def get_by_id(service, file_id: str) -> dict | None:
    try:
        return service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, modifiedTime",
        ).execute()
    except Exception:
        return None


def search_drive(service, query: str) -> dict | None:
    safe_query = query.replace("'", "\\'")
    result = service.files().list(
        q=f"fullText contains '{safe_query}' and trashed=false",
        fields="files(id, name, mimeType, modifiedTime)",
        pageSize=5,
    ).execute()
    files = result.get("files", [])
    if not files:
        return {"error": f"No documents found matching '{query}'."}
    match = files[0]
    if len(files) > 1:
        match["_other_matches"] = [f["name"] for f in files[1:]]
    return match


def get_by_path(path_str: str, service) -> dict | None:
    if not INDEX_FILE.exists():
        return None
    index_files = json.loads(INDEX_FILE.read_text()).get("files", [])
    entry = next((f for f in index_files if f["path"] == path_str), None)
    if not entry:
        return None
    return get_by_id(service, entry["id"])


def purge_old_tmp_files() -> None:
    """Delete files in tmp/ older than TMP_MAX_AGE_SECS."""
    if not TMP_DIR.exists():
        return
    cutoff = time.time() - TMP_MAX_AGE_SECS
    for f in TMP_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def safe_filename(name: str, ext: str, file_id: str) -> str:
    """Sanitize filename, append a short file-ID hash, and ensure correct extension.

    The hash makes filenames unique per Drive file — two files named budget.csv
    from different years won't collide, and the same file always gets the same name.
    """
    name = re.sub(r'[/\\:\0]', '_', name)
    stem = re.sub(r'\.[^.]+$', '', name)  # strip any existing extension
    hsh = hashlib.md5(file_id.encode()).hexdigest()[:6]
    return f"{stem}_{hsh}{ext}"


def download_file(service, file_id: str, mime_type: str, name: str) -> dict:
    """Download the file and write to tmp/. Returns result dict."""
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError:
        return {"error": "Missing googleapiclient. Run: pip install google-api-python-client"}

    info = DOWNLOAD_MAP.get(mime_type)
    if info is None:
        return {"error": f"Download not supported for mime type: {mime_type}"}

    ftype, export_mime, ext = info

    if export_mime is not None:
        req = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        req = service.files().get_media(fileId=file_id)

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(name, ext, file_id)
    out_path = TMP_DIR / filename

    # Stream directly to disk — never buffer the whole file in memory.
    # Overwrite any existing file with the same name (tmp/ is ephemeral).
    too_large = False
    try:
        with open(out_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, req, chunksize=1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
                if f.tell() > MAX_DOWNLOAD_BYTES:
                    too_large = True
                    break
    except Exception as e:
        out_path.unlink(missing_ok=True)
        return {"error": f"Download failed: {e}"}

    if too_large:
        out_path.unlink(missing_ok=True)
        return {"error": f"File too large — exceeds {MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit"}

    out_path.chmod(0o644)
    size = out_path.stat().st_size

    return {
        "name": out_path.name,
        "local_path": str(out_path),
        "sandbox_path": f"/home/sandbox/data/{out_path.name}",
        "mime_type": mime_type,
        "size_bytes": size,
    }


def main() -> None:
    purge_old_tmp_files()
    parser = argparse.ArgumentParser(description="Download a Drive file to tmp/")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--query", help="Full-text search query")
    group.add_argument("--file-id", help="Exact Google Drive file ID")
    group.add_argument("--url", help="Google Docs/Drive share URL")
    group.add_argument("--path", help="Exact file path from index")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to read from (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    service = build_service_or_exit("drive", args.account)

    if args.url:
        file_id = extract_file_id_from_url(args.url)
        if not file_id:
            print(json.dumps({"error": f"Could not extract a file ID from URL: {args.url}"}))
            sys.exit(1)
        file_meta = get_by_id(service, file_id)
    elif args.query:
        file_meta = search_drive(service, args.query)
    elif args.file_id:
        file_meta = get_by_id(service, args.file_id)
    else:
        file_meta = get_by_path(args.path, service)

    if file_meta is None:
        print(json.dumps({"error": "File not found."}))
        sys.exit(1)

    if file_meta.get("error"):
        print(json.dumps(file_meta))
        sys.exit(1)

    other_matches = file_meta.pop("_other_matches", [])
    result = download_file(service, file_meta["id"], file_meta["mimeType"], file_meta["name"])

    if result.get("error"):
        print(json.dumps(result))
        sys.exit(1)

    if other_matches:
        result["other_matches"] = other_matches

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

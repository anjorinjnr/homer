#!/usr/bin/env python3
"""
drive_search.py — Search Google Drive and return matching file metadata via gogcli.

Returns a list of files matching the query without reading or downloading content.
Use this to discover what files are available before deciding which to read or download.

Usage:
    python tools/drive_search.py --query "car insurance"
    python tools/drive_search.py --query "budget" --limit 10
    python tools/drive_search.py --query "tax" --account personal

Output (JSON):
    {
        "query": "car insurance",
        "count": 2,
        "files": [
            {
                "id": "1AbC...",
                "name": "Car Insurance Policy 2025.pdf",
                "path": "family_docs/insurance/Car Insurance Policy 2025.pdf",
                "type": "pdf",
                "mime_type": "application/pdf",
                "modified": "2025-03-01T12:00:00Z"
            },
            ...
        ]
    }
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import DEFAULT_ACCOUNT, load_google_credentials, require_scopes

REPO_ROOT = Path(__file__).parent.parent.resolve()
INDEX_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "drive_index.json"

DEFAULT_LIMIT = 10
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

MIME_LABELS = {
    "application/pdf": "pdf",
    "application/vnd.google-apps.spreadsheet": "google_sheet",
    "application/vnd.google-apps.document": "google_doc",
    "text/csv": "csv",
    "text/plain": "plain",
    "image/jpeg": "image",
    "image/png": "image",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.ms-word": "doc",
}


def get_access_token(account: str) -> str:
    creds = load_google_credentials(account)
    require_scopes(creds, account, DRIVE_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


def load_index() -> dict[str, str]:
    """Load the drive index once and return a {file_id: path} lookup dict."""
    if not INDEX_FILE.exists():
        return {}
    try:
        entries = json.loads(INDEX_FILE.read_text()).get("files", [])
        return {e["id"]: e["path"] for e in entries if "id" in e and "path" in e}
    except Exception:
        return {}


def search(token: str, query: str, limit: int) -> list[dict]:
    safe_query = query.replace("'", "\\'")
    drive_query = f"fullText contains '{safe_query}' and trashed=false"
    
    result = gogcli.run(token, "drive", "ls", "--query", drive_query, "--max", str(limit), "--all")

    index = load_index()
    files = []
    for f in result.get("files", [])[:limit]:
        files.append({
            "id": f["id"],
            "name": f["name"],
            "path": index.get(f["id"]),
            "type": MIME_LABELS.get(f["mimeType"], "other"),
            "mime_type": f["mimeType"],
            "modified": f.get("modifiedTime", ""),
        })
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description="Search Google Drive and return matching file metadata.")
    parser.add_argument("--query", required=True, help="Full-text search query (filename and content)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max results (default {DEFAULT_LIMIT})")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to search (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    try:
        token = get_access_token(args.account)
        files = search(token, args.query, args.limit)
        print(json.dumps({"query": args.query, "count": len(files), "files": files}, indent=2, ensure_ascii=False))

    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

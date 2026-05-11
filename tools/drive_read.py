#!/usr/bin/env python3
"""
drive_read.py — Fetch and extract text content from a Google Drive file.

Searches Drive via gogcli (full-text — covers filename and file content).
File content is downloaded into memory only — nothing is written to disk.
gogcli's `drive download --out=-` does `io.Copy(os.Stdout, resp.Body)` straight
from the HTTP response (verified at internal/cmd/drive.go:981-984), and we
capture stdout in a Python bytes object via subprocess.PIPE.

Usage:
    python tools/drive_read.py --query "car insurance"    # Drive full-text search
    python tools/drive_read.py --file-id <drive_id>       # exact file ID
    python tools/drive_read.py --path "family_docs/insurance/doc.pdf"  # exact path from index
    python tools/drive_read.py --query "tax" --account personal

Output (JSON):
    {
        "name": "filename.pdf",
        "mime_type": "application/pdf",
        "content": "extracted text...",
        "char_count": 1234,
        "truncated": false
    }

Supported types:
    PDF              — extracted via pypdf (in-memory)
    Google Doc       — exported as plain text (gogcli --format=txt)
    Google Sheet     — exported as CSV (gogcli --format=csv)
    DOCX             — downloaded and decoded as text
    image / xlsx     — metadata only (no text extraction)
"""

import argparse
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import DEFAULT_ACCOUNT, load_google_credentials, require_scopes

REPO_ROOT = Path(__file__).parent.parent.resolve()
INDEX_FILE = REPO_ROOT / "context" / ".nanobot_workspace" / "state" / "drive_index.json"

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

MAX_CHARS = 40_000  # ~10K tokens
# 20 MB cap on raw download size. Peak Python heap is ~3× that during
# extraction (subprocess pipe buffer + bytes object + decode-to-string), so
# real memory pressure tops out near 60 MB transient — comfortable inside the
# tenant container's RSS budget. Realistic household docs are <5 MB; this
# leaves headroom for outliers without enabling pathological cases.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

SUPPORTED_MIME_TYPES = {
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


def extract_file_id_from_url(url: str) -> str | None:
    """Extract the file/document ID from a Google Docs/Drive URL."""
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


def fetch_public_doc(file_id: str) -> str | None:
    """
    Fetch a publicly shared Google Doc as plain text without authentication.
    Returns text content or None if the doc is not publicly accessible.
    """
    export_url = f"https://docs.google.com/document/d/{file_id}/export?format=txt"
    try:
        req = urllib.request.Request(export_url, headers={"User-Agent": "Homer/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def search_drive(token: str, query: str) -> dict | None:
    """Search Drive by full text. Returns the best matching file metadata."""
    data = gogcli.run(token, "drive", "search", query, "--max=5")
    files = data.get("files", [])
    if not files:
        return {"error": f"No documents found matching '{query}'."}
    if len(files) > 1:
        match = files[0]
        match["_other_matches"] = [f["name"] for f in files[1:]]
        return match
    return files[0]


def get_by_id(token: str, file_id: str) -> dict | None:
    """Fetch file metadata by ID via gogcli."""
    try:
        data = gogcli.run(token, "drive", "get", file_id)
    except RuntimeError:
        return None
    return data.get("file") or None


def get_by_path(file_path: str, token: str) -> dict | None:
    """Look up file ID from local index, then fetch metadata from Drive."""
    if not INDEX_FILE.exists():
        return None
    index_files = json.loads(INDEX_FILE.read_text()).get("files", [])
    entry = next((f for f in index_files if f["path"] == file_path), None)
    if not entry:
        return None
    return get_by_id(token, entry["id"])


def extract_doc_text(data: bytes) -> str:
    """
    Extract readable text from an old-style .doc binary (application/msword).
    .doc files store text in Latin-1 blocks; we extract contiguous runs of
    printable characters (4+ chars) and join them. No extra deps required.
    """
    try:
        text = data.decode("latin-1", errors="replace")
        runs, current = [], []
        for ch in text:
            if ch.isprintable() or ch in "\n\r\t":
                current.append(ch)
            else:
                if len(current) >= 4:
                    runs.append("".join(current))
                current = []
        if len(current) >= 4:
            runs.append("".join(current))
        return "\n".join(r.strip() for r in runs if r.strip())
    except Exception as e:
        return f"[.doc text extraction error: {e}]"


def extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes entirely in memory."""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = [page.extract_text() for page in reader.pages if page.extract_text()]
        return "\n\n".join(p.strip() for p in parts)
    except ImportError:
        return "[PDF text extraction requires pypdf. Run: pip install pypdf]"
    except Exception as e:
        return f"[PDF extraction error: {e}]"


def check_size_limit(file_meta: dict) -> str | None:
    """Return an error message if the file is too large to download in memory.

    Uses the `size` field from Drive metadata (bytes, as a string). For Google
    Workspace files the metadata size reflects the doc structure, not the
    export size; for binary files (PDF, DOCX, etc.) it's exact. Either way
    it's a tight enough proxy to refuse multi-hundred-MB downloads up front.
    Returns None if size is unknown (Google Docs sometimes omit it) — in that
    case we trust the caller and accept the risk of a one-off large stream.
    """
    raw = file_meta.get("size")
    if raw in (None, "", "0"):
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    if size > MAX_DOWNLOAD_BYTES:
        return (
            f"File too large to read in memory: {size:,} bytes "
            f"(limit: {MAX_DOWNLOAD_BYTES:,}). "
            f"Open the file directly in Drive instead."
        )
    return None


def fetch_content_into_memory(token: str, file_id: str, mime_type: str) -> str:
    """
    Download file content via gogcli and extract text. Bytes stream from
    gogcli's stdout (PIPE) into a Python bytes object — never written to disk.
    See gogcli.download_bytes for the streaming guarantee.

    Memory: the `bytes` object is referenced only inside this function. On
    return, refcount → 0 and CPython frees it immediately; the OS reclaims
    pages on process exit (drive_read is a short-lived CLI). Peak memory ≈
    file size — pre-flight size enforcement happens in main() via
    check_size_limit().
    """
    ftype = SUPPORTED_MIME_TYPES.get(mime_type, "unknown")

    if ftype in ("image", "xlsx"):
        return f"[Content extraction not supported for {ftype} files.]"

    if ftype == "google_doc":
        download_args = ["drive", "download", file_id, "--format=txt", "--out=-"]
    elif ftype == "google_sheet":
        download_args = ["drive", "download", file_id, "--format=csv", "--out=-"]
    elif ftype in ("pdf", "docx", "doc", "csv", "plain"):
        download_args = ["drive", "download", file_id, "--out=-"]
    else:
        return f"[Unsupported file type: {mime_type}]"

    data = gogcli.download_bytes(token, *download_args)

    if ftype == "pdf":
        return extract_pdf_text(data)
    elif ftype == "doc":
        return extract_doc_text(data)
    else:
        return data.decode("utf-8", errors="replace")


def _truncate(content: str) -> tuple[str, bool]:
    if len(content) > MAX_CHARS:
        return content[:MAX_CHARS] + "\n\n[... content truncated ...]", True
    return content, False


def _emit_file(file_meta: dict, content: str, truncated: bool, other_matches: list[str] | None = None) -> None:
    output = {
        "name": file_meta["name"],
        "mime_type": file_meta["mimeType"],
        "modified": file_meta.get("modifiedTime", ""),
        "content": content,
        "char_count": len(content),
        "truncated": truncated,
    }
    if other_matches:
        output["other_matches"] = other_matches
    print(json.dumps(output, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Drive file content for Homer.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--query", help="Full-text search query (searches filename and content via Drive API)")
    group.add_argument("--file-id", help="Exact Google Drive file ID")
    group.add_argument("--url", help="Google Docs/Drive share URL (works for public docs too)")
    group.add_argument("--path", help="Exact file path from index (e.g. family_docs/insurance/doc.pdf)")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help=f"Google account to read from (default: {DEFAULT_ACCOUNT})")
    args = parser.parse_args()

    try:
        token = get_access_token(args.account)
    except (FileNotFoundError, PermissionError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    # --url: extract file ID, try Drive API, fall back to public export
    if args.url:
        file_id = extract_file_id_from_url(args.url)
        if not file_id:
            print(json.dumps({"error": f"Could not extract a file ID from URL: {args.url}"}))
            sys.exit(1)

        file_meta = get_by_id(token, file_id)

        if file_meta is None:
            # Not in this user's Drive — try public export
            content = fetch_public_doc(file_id)
            if content is None:
                print(json.dumps({"error": "File not accessible via Drive API or as a public document."}))
                sys.exit(1)
            content, truncated = _truncate(content)
            print(json.dumps({
                "name": f"public_doc_{file_id}",
                "mime_type": "application/vnd.google-apps.document",
                "source": "public_export",
                "content": content,
                "char_count": len(content),
                "truncated": truncated,
            }, indent=2))
            return

        size_error = check_size_limit(file_meta)
        if size_error:
            print(json.dumps({"error": size_error}))
            sys.exit(1)
        try:
            content = fetch_content_into_memory(token, file_id, file_meta["mimeType"])
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        content, truncated = _truncate(content)
        _emit_file(file_meta, content, truncated)
        return

    try:
        if args.query:
            file_meta = search_drive(token, args.query)
        elif args.file_id:
            file_meta = get_by_id(token, args.file_id)
        else:
            file_meta = get_by_path(args.path, token)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    if file_meta is None:
        print(json.dumps({"error": "File not found."}))
        sys.exit(1)

    if file_meta.get("error"):
        print(json.dumps(file_meta))
        sys.exit(1)

    other_matches = file_meta.pop("_other_matches", [])

    size_error = check_size_limit(file_meta)
    if size_error:
        print(json.dumps({"error": size_error}))
        sys.exit(1)

    try:
        content = fetch_content_into_memory(token, file_meta["id"], file_meta["mimeType"])
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    content, truncated = _truncate(content)
    _emit_file(file_meta, content, truncated, other_matches)


if __name__ == "__main__":
    main()

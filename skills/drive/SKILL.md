---
name: drive
description: Find, read, and download files from the family Google Drive. Use for detailed documents — insurance policies, leases, contracts, warranties, CSVs, and any file the user references.
metadata: {"nanobot":{"always":false,"emoji":"📁"}}
---

# Drive Skill

Homer reads and downloads files from the family Google Drive (`family_docs/` folder).

## Account selection

All drive tools accept `--account <name>` (default: `primary`, the household Drive).
Pass `--account <name>` (e.g. `--account personal`) when the user's request points
to a different Drive. If unsure, default to `primary`. The `family_docs/` index
is built from the `--account` you sync with `drive_fetch.py`.

## Rules

- **Never ask the user to provide a file path or link** — search for it with `drive_search.py` first.
- If search returns multiple matches, pick the most relevant one and proceed. Mention others only if ambiguity could affect the answer.
- `drive_read.py` returns a `"content"` field — summarize it directly, do not dump raw text.
- If `"error"` is returned, report it. If `"truncated": true`, note that only part of the document was read.
- Do not save Drive file contents to memory — the file is the source of truth.

## Workflow

### Finding and reading a document
1. Search first:
```
{HOMER_VENV} {HOMER_TOOLS}/drive_search.py --query "car insurance"
```
2. Read the best match by file ID:
```
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --file-id <id>
```
Or skip search and read in one step if the query is specific enough:
```
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --query "car insurance policy"
```

### Reading a file the user linked
```
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --url "https://docs.google.com/..."
```
Works for public docs even if not in the family Drive.

### Downloading a file for processing (CSV analysis, run_code, etc.)
```
{HOMER_VENV} {HOMER_TOOLS}/drive_download.py --file-id <id>
```
Then use `sandbox_path` from the output directly in a run_code.py script:
```python
with open("/home/sandbox/data/budget_a1b2c3.csv") as f:
    rows = list(csv.DictReader(f))
```

### Listing all indexed files
```
{HOMER_VENV} {HOMER_TOOLS}/drive_fetch.py --list
```
Use when you need to browse what's available without a specific query.

### Uploading a file
```
{HOMER_VENV} {HOMER_TOOLS}/drive_upload.py --file /path/to/file.md
{HOMER_VENV} {HOMER_TOOLS}/drive_upload.py --content "report text..." --name "report.md"
{HOMER_VENV} {HOMER_TOOLS}/drive_upload.py --content "..." --name "file.md" --folder-id <ID>
```

## Tool Reference

### drive_search.py
Search Drive and return matching file metadata (no content downloaded).
```
{HOMER_VENV} {HOMER_TOOLS}/drive_search.py --query "car insurance"
{HOMER_VENV} {HOMER_TOOLS}/drive_search.py --query "budget" --limit 5
{HOMER_VENV} {HOMER_TOOLS}/drive_search.py --query "tax" --account personal
```
Output: `{{"query": "...", "count": N, "files": [{{"id": "...", "name": "...", "path": "...", "type": "...", "modified": "..."}}]}}`
Each result includes the file ID — pass it to `drive_read.py` or `drive_download.py` with `--file-id`.
If count is 0, try a broader query or check `drive_fetch.py --list`.

### drive_read.py
Read file content into context (in-memory, never written to disk).
```
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --query "search terms"
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --file-id <drive_id>
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --path "family_docs/subfolder/file.pdf"
{HOMER_VENV} {HOMER_TOOLS}/drive_read.py --url "https://docs.google.com/document/d/.../edit"
```
Supports: PDF, Google Doc, Google Sheet, native CSV, plain text, DOCX, DOC.

### drive_download.py
Download a file to `tmp/` for processing (e.g. with run_code.py).
```
{HOMER_VENV} {HOMER_TOOLS}/drive_download.py --query "budget csv"
{HOMER_VENV} {HOMER_TOOLS}/drive_download.py --file-id <drive_id>
{HOMER_VENV} {HOMER_TOOLS}/drive_download.py --url "https://drive.google.com/..."
{HOMER_VENV} {HOMER_TOOLS}/drive_download.py --path "family_docs/data.csv"
```
Output: `{{"name": "...", "local_path": "...", "sandbox_path": "/home/sandbox/data/<name>", "mime_type": "...", "size_bytes": N}}`
Google Docs → `.txt`; Google Sheets → `.csv`; all other types downloaded raw.
Filenames include a short hash of the file ID (e.g. `budget_a1b2c3.csv`) — unique per Drive file, stable across downloads.
Use `sandbox_path` directly in run_code.py scripts.

### drive_fetch.py
```
{HOMER_VENV} {HOMER_TOOLS}/drive_fetch.py --list           # list available documents
```

### drive_upload.py
```
{HOMER_VENV} {HOMER_TOOLS}/drive_upload.py --file /path/to/file.md
{HOMER_VENV} {HOMER_TOOLS}/drive_upload.py --content "report text..." --name "report.md"
{HOMER_VENV} {HOMER_TOOLS}/drive_upload.py --content "..." --name "file.md" --folder-id <ID>
```
Returns JSON with a shareable URL. Always share the link with the user after uploading.
The uploaded file is viewable by anyone with the link — do not upload sensitive content.

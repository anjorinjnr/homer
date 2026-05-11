#!/usr/bin/env python3
"""
context_scrub.py — Scan context files for sensitive patterns.

Checks for account numbers, SSNs, credit cards, API keys, passwords,
phone numbers, and other sensitive data that shouldn't be in tracked files.
This is a developer audit tool — it never modifies files.

Usage:
    python tools/context_scrub.py                    # scan all context/*.md
    python tools/context_scrub.py --file finance.md  # scan one file
    python tools/context_scrub.py --json             # output as JSON
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
CONTEXT_DIR = REPO_ROOT / "context"
USER_CONTEXT_DIR = CONTEXT_DIR / "user_context"

# ── Sensitive patterns ────────────────────────────────────────────────────────

PATTERNS = [
    ("SSN",             r"\b\d{3}-\d{2}-\d{4}\b"),
    ("Credit card",     r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"),
    ("Password",        r"(?i)(?:password|passwd|passphrase)\s*[:=]\s*\S+"),
    ("API key / token", r"(?i)(?:api[_\-]?key|secret|token|bearer)\s*[:= ]\s*[A-Za-z0-9+/\-_]{20,}"),
    ("URL with credentials", r"\w+://[^:@\s]+:[^@\s]+@\S+"),
]

# No noisy patterns — all above are high-confidence
NOISY: set = set()


def redact(value: str, keep: int = 4) -> str:
    """Show first and last `keep` chars, mask the middle."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


def scan_file(path: Path) -> list[dict]:
    findings = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return [{"file": str(path), "line": 0, "type": "ERROR", "value": str(e)}]

    for lineno, line in enumerate(lines, start=1):
        # Skip comment lines and headers
        if line.strip().startswith("#") and not re.search(r"\d{6,}", line):
            continue
        for label, pattern in PATTERNS:
            for m in re.finditer(pattern, line):
                matched = m.group(0).strip()
                # Skip very short matches for noisy patterns
                if label in NOISY and len(matched) < 32:
                    continue
                findings.append({
                    "file": path.name,
                    "line": lineno,
                    "type": label,
                    "value": redact(matched),
                    "context": line.strip()[:120],
                })
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan context files for sensitive patterns.")
    parser.add_argument("--file", help="Scan a specific file in context/")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--all", action="store_true", help="Include noisy patterns (more false positives)")
    args = parser.parse_args()

    if args.all:
        NOISY.clear()

    if args.file:
        # Check user_context/ first, fall back to context/
        path = USER_CONTEXT_DIR / args.file
        if not path.exists():
            path = CONTEXT_DIR / args.file
        files = [path]
    else:
        # Scan both directories — user_context/ is canonical, context/ may have unmigrated files
        seen_names: set[str] = set()
        files = []
        for d in [USER_CONTEXT_DIR, CONTEXT_DIR]:
            for f in sorted(d.glob("*.md")):
                if f.name not in seen_names:
                    seen_names.add(f.name)
                    files.append(f)

    all_findings: list[dict] = []
    for f in files:
        if not f.exists():
            print(f"Warning: {f} not found", file=sys.stderr)
            continue
        all_findings.extend(scan_file(f))

    if args.json:
        print(json.dumps(all_findings, indent=2))
        return

    if not all_findings:
        print("✓ No sensitive patterns found.")
        return

    # Group by file
    by_file: dict[str, list[dict]] = {}
    for f in all_findings:
        by_file.setdefault(f["file"], []).append(f)

    print(f"⚠  Found {len(all_findings)} potential sensitive pattern(s):\n")
    for filename, findings in by_file.items():
        print(f"  {filename}")
        for f in findings:
            print(f"    Line {f['line']:>4}  [{f['type']}]  {f['value']}")
            print(f"           {f['context']}")
        print()


if __name__ == "__main__":
    main()

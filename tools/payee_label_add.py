#!/usr/bin/env python3
"""
payee_label_add.py — Add or update a payee label in context/payee_labels.json.

Homer calls this when the user provides labels for unknown transactions from
the monthly spending report.

Usage:
    python tools/payee_label_add.py --payee "Check Paid" --label "Personal Checks"
    python tools/payee_label_add.py --payee "ZELLE" --label "Transfers"

Output (stdout):
    {"status": "added", "payee": "Check Paid", "label": "Personal Checks"}
    {"status": "updated", "payee": "ZELLE", "label": "Transfers", "previous": "Other"}
    {"error": "..."}  on failure + sys.exit(1)
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE_DIR = REPO_ROOT / "context" / ".nanobot_workspace"
PAYEE_LABELS_FILE = WORKSPACE_DIR / "state" / "payee_labels.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Add or update a payee label.")
    parser.add_argument("--payee", required=True, help="Payee substring to match (case-insensitive)")
    parser.add_argument("--label", required=True, help="Category label to assign")
    args = parser.parse_args()

    payee = args.payee.strip()
    label = args.label.strip()

    if not payee or not label:
        print(json.dumps({"error": "--payee and --label must not be empty"}))
        sys.exit(1)

    try:
        if PAYEE_LABELS_FILE.exists():
            labels = json.loads(PAYEE_LABELS_FILE.read_text())
        else:
            PAYEE_LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
            labels = {}
    except (json.JSONDecodeError, OSError) as e:
        print(json.dumps({"error": f"Could not read payee_labels.json: {e}"}))
        sys.exit(1)

    previous = labels.get(payee)
    labels[payee] = label

    try:
        PAYEE_LABELS_FILE.write_text(json.dumps(labels, indent=2) + "\n")
    except OSError as e:
        print(json.dumps({"error": f"Could not write payee_labels.json: {e}"}))
        sys.exit(1)

    status = "updated" if previous is not None else "added"
    result: dict = {"status": status, "payee": payee, "label": label}
    if previous is not None and previous != label:
        result["previous"] = previous

    print(json.dumps(result))


if __name__ == "__main__":
    main()

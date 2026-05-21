#!/usr/bin/env python3
"""
migrate_users_yaml_to_v2.py — Rewrite context/users.yaml from v1 (list-of-records,
keyed by display name) to v2 (dict-of-records, keyed by stable symbol).

Idempotent: running on an already-v2 file is a no-op. See
docs/identity-resolution.md for why this exists.

Usage:
  python scripts/migrate_users_yaml_to_v2.py                  # default path
  python scripts/migrate_users_yaml_to_v2.py --path /tmp/u.yaml
  python scripts/migrate_users_yaml_to_v2.py --dry-run        # print, don't write

Exit codes:
  0  already v2, or migrated successfully
  2  users.yaml missing
  3  users.yaml unparseable / invalid
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.users_loader import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    DEFAULT_USERS_FILE,
    is_v1,
    normalize,
    save_users,
)

import yaml  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    p.add_argument("--path", default=None, help=f"users.yaml path (default: {DEFAULT_USERS_FILE})")
    p.add_argument("--dry-run", action="store_true", help="Print v2 output to stdout; do not write.")
    args = p.parse_args()

    path = Path(args.path) if args.path else DEFAULT_USERS_FILE
    if not path.exists():
        print(f"migrate: {path} does not exist", file=sys.stderr)
        return 2

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"migrate: parse failed: {e}", file=sys.stderr)
        return 3
    if not isinstance(raw, dict):
        print("migrate: users.yaml is not a mapping at top level", file=sys.stderr)
        return 3

    on_disk_v1 = is_v1(raw)
    if not on_disk_v1:
        on_disk_version = int(raw.get("schema_version", CURRENT_SCHEMA_VERSION))
        if on_disk_version >= CURRENT_SCHEMA_VERSION:
            print(f"migrate: already v{on_disk_version}, nothing to do")
            return 0

    data = normalize(raw)

    if args.dry_run:
        yaml.dump(data, sys.stdout, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f"\nmigrate: would rewrite {path} (v1 → v{CURRENT_SCHEMA_VERSION})",
              file=sys.stderr)
        return 0

    save_users(data, path)
    print(f"migrate: rewrote {path} (v1 → v{CURRENT_SCHEMA_VERSION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

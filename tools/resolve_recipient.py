#!/usr/bin/env python3
"""
resolve_recipient.py — Single resolver: (symbol, channel) → handle.

The only piece of code that maps a household symbol like ``primary`` or
``seun`` to the current channel-side handle (a WhatsApp JID, a Telegram
chat id, an email address). Everything that needs to deliver a message
consults this resolver — never reads users.yaml directly, never
constructs handles. See docs/identity-resolution.md.

Exit codes:
  0  resolved successfully; handle on stdout (no trailing whitespace)
  2  symbol or channel unknown / empty (error on stderr)
  3  users.yaml is missing or unreadable (error on stderr)

No silent fallback. Drift in identity routing only stays hidden when the
resolver picks something on a miss — so on a miss this tool refuses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Put repo root on sys.path so `from tools.X` resolves whether invoked as
# `python tools/resolve_recipient.py` (sys.path[0] == tools/) or
# `python -m tools.resolve_recipient`. Matches the convention in
# tools/manage_users.py and tools/tasks_update.py.
_REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.users_loader import load_users, resolve_handle  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="Resolve a household (symbol, channel) to its current handle.",
    )
    p.add_argument("--symbol", required=True, help="Stable user symbol, e.g. 'primary' or 'seun'.")
    p.add_argument("--channel", required=True, help="Channel name, e.g. 'whatsapp', 'telegram', 'email'.")
    p.add_argument("--path", default=None, help="users.yaml path (default: repo's context/users.yaml).")
    args = p.parse_args()

    symbol = (args.symbol or "").strip()
    channel = (args.channel or "").strip()
    if not symbol:
        print("resolve_recipient: --symbol is empty", file=sys.stderr)
        return 2
    if not channel:
        print("resolve_recipient: --channel is empty", file=sys.stderr)
        return 2

    try:
        path = Path(args.path) if args.path else None
        # Surface a clearer error on missing file than KeyError from resolve_handle.
        data = load_users(path)
        if not (data.get("users") or {}):
            print("resolve_recipient: users.yaml has no users", file=sys.stderr)
            return 3
        handle = resolve_handle(symbol, channel, path)
    except FileNotFoundError as e:
        print(f"resolve_recipient: {e}", file=sys.stderr)
        return 3
    except (KeyError, ValueError) as e:
        print(f"resolve_recipient: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())

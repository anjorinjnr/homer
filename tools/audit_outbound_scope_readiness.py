#!/usr/bin/env python3
"""audit_outbound_scope_readiness.py — pre-rollout readiness check for HOMER_OUTBOUND_SCOPE_GUARD.

Lists recipients in this container's channel ``allow_from`` lists that are
neither household members nor covered by an active scope-with-context. When
the guard flag flips, those recipients become "stranded" — Homer can still
receive their replies (they're in allow_from), but any outbound to them
will be refused until a scope is created.

Run *inside* a tenant container:

    docker exec <tenant> python /opt/homer/tools/audit_outbound_scope_readiness.py
    docker exec <tenant> python /opt/homer/tools/audit_outbound_scope_readiness.py --json

The output groups recipients by status:
    no_scope          — in allow_from, no scope at all (ACL-only entry).
    scope_no_context  — scope exists but the envelope has no purpose.
    ok                — household member or active scope-with-context (not flagged).

The fix for each non-OK entry is the same: run
    python tools/manage_interaction.py --create \\
        --name "<recipient>" --phone "<...>" \\
        --purpose "<one-line reason>"
…with the appropriate channel flag (--telegram-id / --email / --whatsapp-group).
This is intentionally interactive — the agent must commit to a purpose, not
the platform.

Exit code: 0 if no stranded recipients, 1 if any.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = str(REPO_ROOT / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import manage_guest as mg  # noqa: E402
import outbound_scope_lookup as osl  # noqa: E402
import scope_store as ss  # noqa: E402


def _load_config_safe(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _wa_allow_from(config: dict) -> list[str]:
    wa = config.get("channels", {}).get("whatsapp", {}) or {}
    return list(wa.get("allow_from") or wa.get("allowFrom") or [])


def _tg_allow_from(config: dict) -> list[str]:
    tg = config.get("channels", {}).get("telegram", {}) or {}
    return list(tg.get("allowFrom") or tg.get("allow_from") or [])


def _classify(channel: str, recipient: str) -> tuple[str, list[str]]:
    """Return (status, scope_ids) for one (channel, recipient).

    Reuses outbound_scope_lookup.resolve so the audit aligns 1:1 with what
    the platform guard will decide at send time. If resolve says authorized,
    we return ``"ok"``; refusals come back with their reason as the status.
    """
    result = osl.resolve(channel, recipient)
    if result.authorized:
        return "ok", list(result.scope_ids)
    return result.reason, list(result.scope_ids)


def _name_for(recipient: str) -> str:
    """Look up the friendly name from manage_guest's ACL, if any."""
    try:
        acl = mg.load_acl()
    except Exception:
        return ""
    entry = acl.get(recipient) or {}
    return entry.get("name") or ""


def audit() -> dict:
    main_cfg = _load_config_safe(mg.NANOBOT_CONFIG_PATH)
    guest_cfg = _load_config_safe(mg.GUEST_NANOBOT_CONFIG_PATH)

    candidates: list[tuple[str, str]] = []  # (channel, recipient)

    # WhatsApp ACLs live on the guest config (manage_guest.add_to_allow_from
    # writes there). Wildcards skip — a "*" allow_from doesn't enumerate
    # individual recipients.
    for jid in _wa_allow_from(guest_cfg):
        if jid == "*":
            continue
        candidates.append(("whatsapp", jid))

    # Telegram ACLs may live on either main or guest config.
    for tg_id in _tg_allow_from(main_cfg) + _tg_allow_from(guest_cfg):
        if tg_id == "*":
            continue
        candidates.append(("telegram", str(tg_id)))

    # Email channels generally allow_from = ["*"] and route by scope_email_index.
    # Walk the index to surface email recipients with scopes-without-context.
    try:
        for email in ss.get_all_active_email_addresses():
            candidates.append(("email", email))
    except Exception:
        pass

    seen: set[tuple[str, str]] = set()
    grouped: dict[str, list[dict]] = {}
    for channel, recipient in candidates:
        key = (channel, recipient)
        if key in seen:
            continue
        seen.add(key)
        status, scope_ids = _classify(channel, recipient)
        grouped.setdefault(status, []).append({
            "channel": channel,
            "recipient": recipient,
            "name": _name_for(recipient),
            "scope_ids": scope_ids,
        })

    return grouped


def _print_report(grouped: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(grouped, indent=2, sort_keys=True))
        return

    ok_count = len(grouped.get("ok", []))
    stranded_groups = {k: v for k, v in grouped.items() if k != "ok"}

    if not stranded_groups:
        print(f"Outbound-scope-readiness: clean. {ok_count} recipients ok.")
        return

    print(f"Outbound-scope-readiness: {sum(len(v) for v in stranded_groups.values())} stranded "
          f"recipient(s) ({ok_count} ok).\n")
    for status, rows in sorted(stranded_groups.items()):
        print(f"## {status}  ({len(rows)})")
        for row in sorted(rows, key=lambda r: (r["channel"], r["recipient"])):
            name = row["name"] or "(no name)"
            scope_info = (
                f" [scopes: {', '.join(row['scope_ids'])}]"
                if row["scope_ids"] else ""
            )
            print(f"  - {row['channel']:8s} {row['recipient']}  {name}{scope_info}")
        print()
    print("Fix each by running:")
    print("  python tools/manage_interaction.py --create --name '...' "
          "[--phone|--telegram-id|--email|--whatsapp-group] '...' "
          "--purpose '<reason>'")


def main() -> int:
    p = argparse.ArgumentParser(description="Audit recipients that would be refused under HOMER_OUTBOUND_SCOPE_GUARD=1.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a human-readable report")
    args = p.parse_args()

    grouped = audit()
    _print_report(grouped, as_json=args.json)
    return 0 if not any(k != "ok" for k in grouped) else 1


if __name__ == "__main__":
    sys.exit(main())

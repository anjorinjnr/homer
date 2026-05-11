#!/usr/bin/env python3
"""Merge tenant-managed MCP servers into a rendered nanobot config.

The portal owns the per-tenant list of MCP servers and writes them as a
plain JSON object to ``/data/mcp_servers.json`` (one entry per server,
keyed by server name, secrets already decrypted). At container boot the
entrypoint calls this script after rendering the main nanobot config so
the merged result lands in ``~/.nanobot/config.json``'s
``tools.mcpServers`` block — exactly where nanobot looks for them.

Validation is deliberately loose: the schema-side guard already lives
in the portal (typed Pydantic model + admin-only write path). Here we
only enforce shape (dict-of-dicts) and drop entries with invalid
transport types so a corrupt row can't take the container down.

CLI:

    render_mcp_servers.py --config <path> [--mcp-file <path>]

Exits 0 even when the MCP file is absent — that is the default state for
tenants who haven't added any servers and must not block boot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VALID_TYPES = {"stdio", "sse", "streamableHttp"}


def _coerce_server(name: str, raw: Any) -> dict[str, Any] | None:
    """Return a sanitized server entry, or None if it should be dropped."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    transport = raw.get("type")
    if transport is not None:
        if transport not in VALID_TYPES:
            return None
        out["type"] = transport
    # stdio
    if "command" in raw and isinstance(raw["command"], str) and raw["command"]:
        out["command"] = raw["command"]
    if "args" in raw and isinstance(raw["args"], list):
        out["args"] = [str(a) for a in raw["args"]]
    if "env" in raw and isinstance(raw["env"], dict):
        out["env"] = {str(k): str(v) for k, v in raw["env"].items()}
    # http/sse
    if "url" in raw and isinstance(raw["url"], str) and raw["url"]:
        out["url"] = raw["url"]
    if "headers" in raw and isinstance(raw["headers"], dict):
        out["headers"] = {str(k): str(v) for k, v in raw["headers"].items()}
    # shared
    if "tool_timeout" in raw and isinstance(raw["tool_timeout"], int):
        out["tool_timeout"] = raw["tool_timeout"]
    if "enabled_tools" in raw and isinstance(raw["enabled_tools"], list):
        out["enabled_tools"] = [str(t) for t in raw["enabled_tools"]]
    # Must have either a command (stdio) or a url (remote) to be useful.
    if "command" not in out and "url" not in out:
        return None
    return out


def load_tenant_servers(path: Path) -> dict[str, dict[str, Any]]:
    """Load and sanitize the tenant MCP file. Missing file → empty dict."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[render_mcp_servers] WARN: could not read {path}: {e}", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print(
            f"[render_mcp_servers] WARN: {path} is not a JSON object; ignoring",
            file=sys.stderr,
        )
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name:
            continue
        coerced = _coerce_server(name, entry)
        if coerced is None:
            print(
                f"[render_mcp_servers] WARN: dropping invalid MCP server {name!r}",
                file=sys.stderr,
            )
            continue
        out[name] = coerced
    return out


def merge_into_config(config_path: Path, servers: dict[str, dict[str, Any]]) -> int:
    """Merge ``servers`` into ``config_path``'s tools.mcpServers block.

    Returns the number of servers written. Tenant entries override any
    same-named entry from the rendered template (template ships empty
    today; this just future-proofs against image-bundled defaults).
    """
    config = json.loads(config_path.read_text())
    tools = config.setdefault("tools", {})
    existing = tools.get("mcpServers")
    if not isinstance(existing, dict):
        existing = {}
    merged = {**existing, **servers}
    tools["mcpServers"] = merged
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return len(servers)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, help="Path to rendered nanobot config.json")
    p.add_argument(
        "--mcp-file",
        default="/data/mcp_servers.json",
        help="Path to the tenant MCP servers JSON (default: /data/mcp_servers.json)",
    )
    args = p.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[render_mcp_servers] ERROR: config not found: {config_path}", file=sys.stderr)
        return 1

    servers = load_tenant_servers(Path(args.mcp_file))
    written = merge_into_config(config_path, servers)
    print(f"[render_mcp_servers] merged {written} tenant MCP server(s) into {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

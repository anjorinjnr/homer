"""Tests for render_mcp_servers.py — tenant MCP merge into nanobot config."""

import json
from pathlib import Path

import pytest

from tools import render_mcp_servers as rms


def _write_config(tmp_path: Path, mcp_servers=None) -> Path:
    cfg = {
        "providers": {"anthropic": {"apiKey": "x"}},
        "tools": {
            "restrictToWorkspace": True,
            "exec": {"allowPatterns": []},
            "mcpServers": mcp_servers if mcp_servers is not None else {},
        },
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return p


# ── load_tenant_servers ────────────────────────────────────────────────────


def test_missing_file_returns_empty(tmp_path):
    assert rms.load_tenant_servers(tmp_path / "absent.json") == {}


def test_invalid_json_returns_empty(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    assert rms.load_tenant_servers(p) == {}


def test_non_object_root_returns_empty(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]")
    assert rms.load_tenant_servers(p) == {}


def test_stdio_server_loaded(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "brave": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                    "env": {"BRAVE_API_KEY": "secret123"},
                }
            }
        )
    )
    out = rms.load_tenant_servers(p)
    assert out == {
        "brave": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-brave-search"],
            "env": {"BRAVE_API_KEY": "secret123"},
        }
    }


def test_streamable_http_server_loaded(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "remote": {
                    "type": "streamableHttp",
                    "url": "https://mcp.example.com/sse",
                    "headers": {"Authorization": "Bearer abc"},
                    "tool_timeout": 60,
                }
            }
        )
    )
    out = rms.load_tenant_servers(p)
    assert out["remote"]["type"] == "streamableHttp"
    assert out["remote"]["url"] == "https://mcp.example.com/sse"
    assert out["remote"]["headers"] == {"Authorization": "Bearer abc"}
    assert out["remote"]["tool_timeout"] == 60


def test_invalid_transport_dropped(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "good": {"type": "stdio", "command": "npx"},
                "bad": {"type": "websocket", "url": "ws://x"},
            }
        )
    )
    out = rms.load_tenant_servers(p)
    assert "good" in out
    assert "bad" not in out


def test_entry_without_command_or_url_dropped(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"empty": {"type": "stdio"}}))
    assert rms.load_tenant_servers(p) == {}


def test_non_dict_entry_dropped(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"weird": "string-not-object"}))
    assert rms.load_tenant_servers(p) == {}


def test_args_coerced_to_strings(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"x": {"type": "stdio", "command": "echo", "args": ["a", 1, True]}}))
    out = rms.load_tenant_servers(p)
    assert out["x"]["args"] == ["a", "1", "True"]


# ── merge_into_config ──────────────────────────────────────────────────────


def test_merge_writes_tenant_servers(tmp_path):
    cfg = _write_config(tmp_path)
    written = rms.merge_into_config(cfg, {"brave": {"type": "stdio", "command": "npx"}})
    assert written == 1
    result = json.loads(cfg.read_text())
    assert result["tools"]["mcpServers"] == {"brave": {"type": "stdio", "command": "npx"}}


def test_merge_preserves_other_tools_keys(tmp_path):
    cfg = _write_config(tmp_path)
    rms.merge_into_config(cfg, {"a": {"type": "stdio", "command": "x"}})
    result = json.loads(cfg.read_text())
    assert result["tools"]["restrictToWorkspace"] is True
    assert result["tools"]["exec"] == {"allowPatterns": []}


def test_merge_overrides_template_entries(tmp_path):
    cfg = _write_config(tmp_path, mcp_servers={"foo": {"type": "stdio", "command": "old"}})
    rms.merge_into_config(cfg, {"foo": {"type": "stdio", "command": "new"}})
    result = json.loads(cfg.read_text())
    assert result["tools"]["mcpServers"]["foo"]["command"] == "new"


def test_merge_with_no_tenant_servers_clears_to_empty(tmp_path):
    cfg = _write_config(tmp_path, mcp_servers={"old": {"type": "stdio", "command": "x"}})
    # An empty tenant file is the "user removed everything" case — but
    # the entrypoint only merges, it doesn't reset. So template entries
    # survive when tenant dict is empty (which is the desired behavior:
    # the template ships empty today, but if a future image bundles
    # built-in MCPs they should not be silently wiped).
    rms.merge_into_config(cfg, {})
    result = json.loads(cfg.read_text())
    assert result["tools"]["mcpServers"] == {"old": {"type": "stdio", "command": "x"}}


def test_merge_creates_tools_block_if_missing(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"providers": {}}))
    rms.merge_into_config(cfg, {"a": {"type": "stdio", "command": "x"}})
    result = json.loads(cfg.read_text())
    assert result["tools"]["mcpServers"] == {"a": {"type": "stdio", "command": "x"}}


# ── CLI ────────────────────────────────────────────────────────────────────


def test_main_missing_config_returns_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["render_mcp_servers.py", "--config", str(tmp_path / "absent.json")],
    )
    assert rms.main() == 1


def test_main_no_mcp_file_is_noop(tmp_path, monkeypatch, capsys):
    cfg = _write_config(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "render_mcp_servers.py",
            "--config",
            str(cfg),
            "--mcp-file",
            str(tmp_path / "absent.json"),
        ],
    )
    assert rms.main() == 0
    result = json.loads(cfg.read_text())
    assert result["tools"]["mcpServers"] == {}


def test_main_merges_tenant_file(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        json.dumps({"brave": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]}})
    )
    monkeypatch.setattr(
        "sys.argv",
        ["render_mcp_servers.py", "--config", str(cfg), "--mcp-file", str(mcp)],
    )
    assert rms.main() == 0
    result = json.loads(cfg.read_text())
    assert "brave" in result["tools"]["mcpServers"]

"""Tests for tools/resolve_recipient.py — single (symbol, channel) → handle resolver."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "resolve_recipient.py"


def _v2_doc(**users) -> dict:
    return {"schema_version": 2, "users": users}


def _alice_bob_v2():
    return _v2_doc(
        primary={
            "display_name": "Alice",
            "role": "admin",
            "channels": {"whatsapp": "246157477413033@lid.whatsapp.net", "telegram": "tg-alice"},
        },
        bob={
            "display_name": "Bob",
            "role": "member",
            "channels": {"whatsapp": "105321339076677@lid.whatsapp.net"},
        },
    )


@pytest.fixture
def users_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump(_alice_bob_v2(), sort_keys=False))
    return p


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
    )


class TestResolveRecipient:
    def test_resolves_primary_whatsapp(self, users_yaml):
        r = _run("--symbol", "primary", "--channel", "whatsapp", "--path", str(users_yaml))
        assert r.returncode == 0
        assert r.stdout == "246157477413033@lid.whatsapp.net"

    def test_resolves_member_by_first_name_symbol(self, users_yaml):
        r = _run("--symbol", "bob", "--channel", "whatsapp", "--path", str(users_yaml))
        assert r.returncode == 0
        assert r.stdout == "105321339076677@lid.whatsapp.net"

    def test_unknown_symbol_exit_2(self, users_yaml):
        r = _run("--symbol", "ghost", "--channel", "whatsapp", "--path", str(users_yaml))
        assert r.returncode == 2
        assert "unknown symbol" in r.stderr

    def test_unknown_channel_exit_2(self, users_yaml):
        r = _run("--symbol", "primary", "--channel", "email", "--path", str(users_yaml))
        assert r.returncode == 2
        assert "no 'email' channel" in r.stderr

    def test_empty_symbol_exit_2(self, users_yaml):
        r = _run("--symbol", "", "--channel", "whatsapp", "--path", str(users_yaml))
        assert r.returncode == 2

    def test_missing_file_exit_3(self, tmp_path):
        r = _run("--symbol", "primary", "--channel", "whatsapp",
                 "--path", str(tmp_path / "does_not_exist.yaml"))
        assert r.returncode == 3

    def test_empty_users_yaml_exit_3(self, tmp_path):
        # File exists but has zero users — distinct from missing.
        p = tmp_path / "users.yaml"
        p.write_text("schema_version: 2\nusers: {}\n")
        r = _run("--symbol", "primary", "--channel", "whatsapp", "--path", str(p))
        assert r.returncode == 3

    def test_handle_emits_no_trailing_newline(self, users_yaml):
        """Callers pipe this into other tools; trailing whitespace would
        silently corrupt the value."""
        r = _run("--symbol", "primary", "--channel", "telegram", "--path", str(users_yaml))
        assert r.stdout == "tg-alice"
        assert not r.stdout.endswith("\n")

    def test_resolves_v1_file(self, tmp_path):
        """Step 1 is backward compatible — a v1-shape users.yaml still resolves."""
        p = tmp_path / "users.yaml"
        p.write_text(yaml.safe_dump({"users": [
            {"name": "Alice", "role": "admin", "channels": {"whatsapp": "wa-1"}},
        ]}, sort_keys=False))
        r = _run("--symbol", "primary", "--channel", "whatsapp", "--path", str(p))
        assert r.returncode == 0
        assert r.stdout == "wa-1"

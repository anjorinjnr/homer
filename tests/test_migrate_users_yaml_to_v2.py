"""Tests for scripts/migrate_users_yaml_to_v2.py — idempotent in-place migration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "migrate_users_yaml_to_v2.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def v1_file(tmp_path: Path) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump({"users": [
        {"name": "Alice", "role": "admin", "channels": {"whatsapp": "wa-1"}},
        {"name": "Bob", "role": "member", "channels": {"telegram": "tg-2"}},
    ]}, sort_keys=False))
    return p


@pytest.fixture
def v2_file(tmp_path: Path) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text("schema_version: 2\nusers:\n  primary:\n    display_name: Alice\n    role: admin\n")
    return p


class TestMigrateUsersYamlToV2:
    def test_v1_to_v2_rewrites_in_place(self, v1_file):
        r = _run("--path", str(v1_file))
        assert r.returncode == 0, r.stderr
        raw = yaml.safe_load(v1_file.read_text())
        assert raw["schema_version"] == 2
        assert isinstance(raw["users"], dict)
        assert "primary" in raw["users"]
        assert raw["users"]["primary"]["display_name"] == "Alice"

    def test_idempotent_on_v2(self, v2_file):
        before = v2_file.read_text()
        r = _run("--path", str(v2_file))
        assert r.returncode == 0
        assert "already" in r.stdout
        # File is unchanged.
        assert v2_file.read_text() == before

    def test_dry_run_does_not_write(self, v1_file):
        before = v1_file.read_text()
        r = _run("--path", str(v1_file), "--dry-run")
        assert r.returncode == 0
        # v2 yaml dumped to stdout
        assert "schema_version: 2" in r.stdout
        assert "primary:" in r.stdout
        # Disk unchanged.
        assert v1_file.read_text() == before

    def test_missing_file_exit_2(self, tmp_path):
        r = _run("--path", str(tmp_path / "nope.yaml"))
        assert r.returncode == 2

    def test_unparseable_exit_3(self, tmp_path):
        p = tmp_path / "users.yaml"
        p.write_text("not: valid: yaml: ::\n")
        r = _run("--path", str(p))
        assert r.returncode == 3

    def test_non_mapping_top_level_exit_3(self, tmp_path):
        p = tmp_path / "users.yaml"
        p.write_text("- just\n- a\n- list\n")
        r = _run("--path", str(p))
        assert r.returncode == 3

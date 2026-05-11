"""Tests for vault.py — secure key-value store for sensitive reference data."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent.parent / "tools"
VAULT = TOOLS / "vault.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_vault(*args: str, db: Path, expect_error: bool = False) -> dict:
    """Run vault.py with --db override and return parsed JSON."""
    result = subprocess.run(
        [sys.executable, str(VAULT), *args, "--db", str(db)],
        capture_output=True, text=True,
    )
    if expect_error:
        assert result.returncode == 1, f"Expected exit 1, got {result.returncode}"
    else:
        assert result.returncode == 0, f"vault.py failed: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Unit tests (via Python import)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(TOOLS))
from vault import get_conn, vault_set, vault_get, vault_list, vault_remove


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "vault.db"
    c = get_conn(db_path)
    yield c
    c.close()


class TestVaultSet:
    def test_create_new_key(self, conn):
        result = vault_set(conn, "bonvoy", "123456")
        assert result["status"] == "created"
        assert result["key"] == "bonvoy"

    def test_update_existing_key(self, conn):
        vault_set(conn, "bonvoy", "123456")
        result = vault_set(conn, "bonvoy", "789012")
        assert result["status"] == "updated"

    def test_set_with_label(self, conn):
        vault_set(conn, "bonvoy", "123456", label="Marriott Bonvoy")
        row = conn.execute("SELECT label FROM vault WHERE key = ?", ("bonvoy",)).fetchone()
        assert row["label"] == "Marriott Bonvoy"

    def test_update_preserves_label_when_not_given(self, conn):
        vault_set(conn, "bonvoy", "123456", label="Marriott Bonvoy")
        vault_set(conn, "bonvoy", "789012")  # no label
        row = conn.execute("SELECT label FROM vault WHERE key = ?", ("bonvoy",)).fetchone()
        assert row["label"] == "Marriott Bonvoy"

    def test_update_overwrites_label_when_given(self, conn):
        vault_set(conn, "bonvoy", "123456", label="Old label")
        vault_set(conn, "bonvoy", "789012", label="New label")
        row = conn.execute("SELECT label FROM vault WHERE key = ?", ("bonvoy",)).fetchone()
        assert row["label"] == "New label"


class TestVaultGet:
    def test_get_existing(self, conn):
        vault_set(conn, "plaid_code", "NFGZD4", label="Plaid recovery code")
        result = vault_get(conn, "plaid_code")
        assert result["key"] == "plaid_code"
        assert result["value"] == "NFGZD4"
        assert result["label"] == "Plaid recovery code"

    def test_get_missing(self, conn):
        result = vault_get(conn, "nonexistent")
        assert "error" in result

    def test_get_includes_internal_warning(self, conn):
        vault_set(conn, "secret", "abc123")
        result = vault_get(conn, "secret")
        assert "_internal" in result
        assert "Do not echo" in result["_internal"]


class TestVaultList:
    def test_list_empty(self, conn):
        result = vault_list(conn)
        assert result["count"] == 0
        assert result["entries"] == []

    def test_list_does_not_include_values(self, conn):
        vault_set(conn, "key1", "secret_value_1", label="Label 1")
        vault_set(conn, "key2", "secret_value_2", label="Label 2")
        result = vault_list(conn)
        assert result["count"] == 2
        for entry in result["entries"]:
            assert "value" not in entry
            assert "key" in entry
            assert "label" in entry

    def test_list_sorted_by_key(self, conn):
        vault_set(conn, "zebra", "z")
        vault_set(conn, "alpha", "a")
        vault_set(conn, "middle", "m")
        result = vault_list(conn)
        keys = [e["key"] for e in result["entries"]]
        assert keys == ["alpha", "middle", "zebra"]


class TestVaultRemove:
    def test_remove_existing(self, conn):
        vault_set(conn, "temp", "123")
        result = vault_remove(conn, "temp")
        assert result["status"] == "removed"
        assert vault_get(conn, "temp").get("error")

    def test_remove_missing(self, conn):
        result = vault_remove(conn, "nonexistent")
        assert "error" in result


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestVaultCLI:
    def test_set_and_get(self, tmp_path):
        db = tmp_path / "vault.db"
        run_vault("--set", "bonvoy", "072663520", "--label", "Marriott Bonvoy", db=db)
        result = run_vault("--get", "bonvoy", db=db)
        assert result["value"] == "072663520"
        assert result["label"] == "Marriott Bonvoy"

    def test_list_shows_keys_not_values(self, tmp_path):
        db = tmp_path / "vault.db"
        run_vault("--set", "secret_key", "super_secret_value", "--label", "A secret", db=db)
        result = run_vault("--list", db=db)
        assert result["count"] == 1
        assert result["entries"][0]["key"] == "secret_key"
        output_str = json.dumps(result)
        assert "super_secret_value" not in output_str

    def test_remove(self, tmp_path):
        db = tmp_path / "vault.db"
        run_vault("--set", "temp", "123", db=db)
        result = run_vault("--remove", "temp", db=db)
        assert result["status"] == "removed"

    def test_set_update(self, tmp_path):
        db = tmp_path / "vault.db"
        run_vault("--set", "k", "v1", db=db)
        run_vault("--set", "k", "v2", db=db)
        result = run_vault("--get", "k", db=db)
        assert result["value"] == "v2"

    def test_get_missing_returns_error(self, tmp_path):
        db = tmp_path / "vault.db"
        # Need to create the DB first
        run_vault("--set", "x", "y", db=db)
        run_vault("--remove", "x", db=db)
        result = run_vault("--get", "x", db=db, expect_error=True)
        assert "error" in result

    def test_remove_missing_returns_error(self, tmp_path):
        db = tmp_path / "vault.db"
        run_vault("--set", "x", "y", db=db)
        run_vault("--remove", "x", db=db)
        result = run_vault("--remove", "x", db=db, expect_error=True)
        assert "error" in result

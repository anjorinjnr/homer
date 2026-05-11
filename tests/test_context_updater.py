"""
Tests for context_updater.py (multi-file context architecture)

Run: .venv/bin/python -m pytest tests/test_context_updater.py -v
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import context_updater as cu


SAMPLE_PROPERTY = textwrap.dedent("""\
    # Property & Systems Context
    # Last updated: 2026-03-01 10:00

    ---

    ## HVAC

    - **System**: [FILL: brand, model, install year]
    - **Last service**: [FILL: date, what was done]
    - **Filter size**: [FILL: e.g., 20x25x1]

    ---

    ## Maintenance Log

    | Date | System | Work Done | Done By | Notes |
    |------|--------|-----------|---------|-------|
    | 2025-10-01 | HVAC | Annual service | Contractor | ABC HVAC |

""")


@pytest.fixture
def property_file(tmp_path):
    """Create a temporary property.md for testing."""
    f = tmp_path / "property.md"
    f.write_text(SAMPLE_PROPERTY, encoding="utf-8")
    return f


class TestUpdateKeyValue:
    def test_replaces_fill_placeholder(self, property_file):
        content = cu.read_context(property_file)
        result = cu.update_key_value(content, "HVAC", None, "Filter size", "20x25x1")
        assert "- **Filter size**: 20x25x1" in result
        assert "[FILL: e.g., 20x25x1]" not in result

    def test_replaces_existing_value(self, property_file):
        content = cu.read_context(property_file)
        content = cu.update_key_value(content, "HVAC", None, "Filter size", "20x25x1")
        result = cu.update_key_value(content, "HVAC", None, "Filter size", "16x25x1")
        assert "- **Filter size**: 16x25x1" in result
        assert "20x25x1" not in result

    def test_appends_new_key_when_not_found(self, property_file):
        content = cu.read_context(property_file)
        result = cu.update_key_value(content, "HVAC", None, "Warranty expiry", "2028-06")
        assert "- **Warranty expiry**: 2028-06" in result

    def test_updates_last_service(self, property_file):
        content = cu.read_context(property_file)
        result = cu.update_key_value(content, "HVAC", None, "Last service", "2026-03-07 — filter replaced")
        assert "2026-03-07 — filter replaced" in result


class TestAppendTableRow:
    def test_appends_maintenance_row(self, property_file):
        content = cu.read_context(property_file)
        result = cu.append_table_row(content, "Maintenance Log", "2026-03-07|HVAC|Filter replacement|DIY|20x25x1")
        assert "| 2026-03-07 | HVAC | Filter replacement | DIY | 20x25x1 |" in result

    def test_existing_row_preserved(self, property_file):
        content = cu.read_context(property_file)
        result = cu.append_table_row(content, "Maintenance Log", "2026-03-07|Pool|Chemical check|DIY|")
        assert "2025-10-01" in result
        assert "2026-03-07" in result


class TestUpdateTimestamp:
    def test_timestamp_updated(self):
        content = "# Last updated: 2026-01-01 00:00\nsome content"
        result = cu.update_timestamp(content)
        assert "2026-01-01 00:00" not in result
        assert "# Last updated:" in result


class TestDryRun:
    def test_dry_run_does_not_write(self, property_file):
        original_content = property_file.read_text()
        content = cu.read_context(property_file)
        updated = cu.update_key_value(content, "HVAC", None, "Filter size", "DRYRUN-VALUE")
        assert property_file.read_text() == original_content
        assert "DRYRUN-VALUE" in updated


class TestMigration:
    """Tests for user_context/ migration behavior."""

    def test_read_falls_back_to_old_location(self, tmp_path, monkeypatch):
        """get_context_file() for reads falls back to context/ when user_context/ has no file."""
        ctx_dir = tmp_path / "context"
        uc_dir = ctx_dir / "user_context"
        ctx_dir.mkdir(parents=True)
        uc_dir.mkdir(parents=True)

        old_file = ctx_dir / "property.md"
        old_file.write_text(SAMPLE_PROPERTY)

        monkeypatch.setattr(cu, "CONTEXT_DIR", ctx_dir)
        monkeypatch.setattr(cu, "USER_CONTEXT_DIR", uc_dir)

        result = cu.get_context_file("property")
        assert result == old_file

    def test_read_prefers_user_context(self, tmp_path, monkeypatch):
        """When file exists in both locations, user_context/ wins."""
        ctx_dir = tmp_path / "context"
        uc_dir = ctx_dir / "user_context"
        ctx_dir.mkdir(parents=True)
        uc_dir.mkdir(parents=True)

        (ctx_dir / "property.md").write_text("old")
        (uc_dir / "property.md").write_text("new")

        monkeypatch.setattr(cu, "CONTEXT_DIR", ctx_dir)
        monkeypatch.setattr(cu, "USER_CONTEXT_DIR", uc_dir)

        result = cu.get_context_file("property")
        assert result == uc_dir / "property.md"

    def test_write_always_targets_user_context(self, tmp_path, monkeypatch):
        """for_write=True always returns user_context/ path."""
        ctx_dir = tmp_path / "context"
        uc_dir = ctx_dir / "user_context"
        ctx_dir.mkdir(parents=True)

        # Even when old-path file exists, write target is user_context/
        (ctx_dir / "property.md").write_text("old content")

        monkeypatch.setattr(cu, "CONTEXT_DIR", ctx_dir)
        monkeypatch.setattr(cu, "USER_CONTEXT_DIR", uc_dir)

        result = cu.get_context_file("property", for_write=True)
        assert result == uc_dir / "property.md"
        assert uc_dir.exists()  # directory created

    def test_write_creates_in_user_context_deletes_old(self, tmp_path, monkeypatch):
        """Full write flow: reads from context/, writes to user_context/, deletes old."""
        ctx_dir = tmp_path / "context"
        uc_dir = ctx_dir / "user_context"
        ctx_dir.mkdir(parents=True)
        uc_dir.mkdir(parents=True)

        old_file = ctx_dir / "property.md"
        old_file.write_text(SAMPLE_PROPERTY)

        monkeypatch.setattr(cu, "CONTEXT_DIR", ctx_dir)
        monkeypatch.setattr(cu, "USER_CONTEXT_DIR", uc_dir)

        # Read from old location
        read_path = cu.get_context_file("property")
        content = cu.read_context(read_path)

        # Modify content
        content = cu.update_key_value(content, "HVAC", None, "Filter size", "20x25x1")

        # Write to new location
        write_path = cu.get_context_file("property", for_write=True)
        cu.write_context(write_path, content)

        # Simulate the cleanup from main()
        if old_file.exists() and old_file != write_path:
            old_file.unlink()

        assert (uc_dir / "property.md").exists()
        assert "20x25x1" in (uc_dir / "property.md").read_text()
        assert not old_file.exists()


class TestEndToEnd:
    def test_key_value_write_roundtrip(self, property_file):
        content = cu.read_context(property_file)
        updated = cu.update_key_value(content, "HVAC", None, "System", "Lennox XC21 (5-ton, installed 2019)")
        updated = cu.update_timestamp(updated)
        cu.write_context(property_file, updated)
        result = cu.read_context(property_file)
        assert "Lennox XC21" in result
        assert "# Last updated:" in result

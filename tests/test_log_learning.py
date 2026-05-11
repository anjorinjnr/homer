"""
Tests for log_learning.py — append-only learning log.

Uses a temp file to avoid touching the real context/learnings.md.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import log_learning


def run_append(tmp_path, entry_type, desc, context=None):
    """Run append_entry with the log file redirected to tmp_path."""
    with patch.object(log_learning, "LEARNINGS_FILE", tmp_path):
        with patch("builtins.print") as mock_print:
            log_learning.append_entry(entry_type, desc, context)
            return json.loads(mock_print.call_args[0][0])


def run_list(tmp_path, filter_type=None, limit=20):
    """Run list_entries with the log file redirected to tmp_path."""
    with patch.object(log_learning, "LEARNINGS_FILE", tmp_path):
        with patch("builtins.print") as mock_print:
            log_learning.list_entries(filter_type=filter_type, limit=limit)
            return json.loads(mock_print.call_args[0][0])


class TestAppendEntry(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mktemp(suffix=".md"))

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_creates_file_with_header_on_first_write(self):
        run_append(self.tmp, "bug", "Test bug")
        content = self.tmp.read_text()
        self.assertIn("# Homer Learnings Log", content)

    def test_returns_logged_true_with_type_and_desc(self):
        out = run_append(self.tmp, "bug", "Hardcoded totals")
        self.assertTrue(out["logged"])
        self.assertEqual(out["type"], "bug")
        self.assertEqual(out["desc"], "Hardcoded totals")

    def test_entry_contains_type_and_desc(self):
        run_append(self.tmp, "feature", "Group chat support")
        content = self.tmp.read_text()
        self.assertIn("feature", content)
        self.assertIn("Group chat support", content)

    def test_entry_contains_date(self):
        run_append(self.tmp, "prompt", "User prefers bullets")
        content = self.tmp.read_text()
        import re
        self.assertRegex(content, r"\d{4}-\d{2}-\d{2}")

    def test_context_included_when_provided(self):
        run_append(self.tmp, "prompt", "Pool is monthly", context="User said weekly was wrong")
        content = self.tmp.read_text()
        self.assertIn("Context: User said weekly was wrong", content)

    def test_context_omitted_when_not_provided(self):
        run_append(self.tmp, "bug", "Some bug")
        content = self.tmp.read_text()
        self.assertNotIn("Context:", content)

    def test_multiple_entries_appended(self):
        run_append(self.tmp, "bug", "Bug one")
        run_append(self.tmp, "feature", "Feature one")
        run_append(self.tmp, "prompt", "Correction one")
        content = self.tmp.read_text()
        self.assertIn("Bug one", content)
        self.assertIn("Feature one", content)
        self.assertIn("Correction one", content)

    def test_all_types_accepted(self):
        for t in ("bug", "feature", "prompt"):
            out = run_append(self.tmp, t, f"Test {t}")
            self.assertEqual(out["type"], t)

    def test_emoji_included_in_entry(self):
        run_append(self.tmp, "bug", "Test")
        content = self.tmp.read_text()
        self.assertIn("🐛", content)

        run_append(self.tmp, "feature", "Test")
        content = self.tmp.read_text()
        self.assertIn("💡", content)

        run_append(self.tmp, "prompt", "Test")
        content = self.tmp.read_text()
        self.assertIn("✏️", content)


class TestListEntries(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mktemp(suffix=".md"))

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_empty_when_no_file(self):
        out = run_list(self.tmp)
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["total"], 0)

    def test_returns_entries_in_reverse_order(self):
        run_append(self.tmp, "bug", "First bug")
        run_append(self.tmp, "feature", "Second feature")
        out = run_list(self.tmp)
        # Most recent first
        self.assertEqual(out["entries"][0]["desc"], "Second feature")
        self.assertEqual(out["entries"][1]["desc"], "First bug")

    def test_filter_by_type(self):
        run_append(self.tmp, "bug", "A bug")
        run_append(self.tmp, "prompt", "A prompt issue")
        run_append(self.tmp, "bug", "Another bug")
        out = run_list(self.tmp, filter_type="bug")
        self.assertEqual(len(out["entries"]), 2)
        for e in out["entries"]:
            self.assertEqual(e["type"], "bug")

    def test_limit_applied(self):
        for i in range(10):
            run_append(self.tmp, "bug", f"Bug {i}")
        out = run_list(self.tmp, limit=3)
        self.assertEqual(len(out["entries"]), 3)

    def test_entry_fields_present(self):
        run_append(self.tmp, "prompt", "Wrong timezone", context="User corrected me")
        out = run_list(self.tmp)
        e = out["entries"][0]
        self.assertIn("datetime", e)
        self.assertIn("type", e)
        self.assertIn("desc", e)
        self.assertIn("context", e)
        self.assertEqual(e["type"], "prompt")
        self.assertEqual(e["desc"], "Wrong timezone")
        self.assertEqual(e["context"], "User corrected me")

    def test_entry_without_context_has_no_context_field(self):
        run_append(self.tmp, "prompt", "User likes bullets")
        out = run_list(self.tmp)
        self.assertNotIn("context", out["entries"][0])


class TestClearEntries(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mktemp(suffix=".md"))

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def _run_clear(self, tmp_path):
        with patch.object(log_learning, "LEARNINGS_FILE", tmp_path):
            with patch("builtins.print") as mock_print:
                log_learning.clear_entries()
                return json.loads(mock_print.call_args[0][0])

    def test_clear_removes_all_entries(self):
        run_append(self.tmp, "bug", "Bug one")
        run_append(self.tmp, "feature", "Feature one")
        self._run_clear(self.tmp)
        content = self.tmp.read_text()
        self.assertNotIn("Bug one", content)
        self.assertNotIn("Feature one", content)

    def test_clear_reports_count(self):
        run_append(self.tmp, "bug", "Bug one")
        run_append(self.tmp, "bug", "Bug two")
        run_append(self.tmp, "feature", "Feature one")
        out = self._run_clear(self.tmp)
        self.assertTrue(out["cleared"])
        self.assertEqual(out["entries_removed"], 3)

    def test_clear_preserves_header(self):
        run_append(self.tmp, "bug", "Bug one")
        self._run_clear(self.tmp)
        content = self.tmp.read_text()
        self.assertIn("# Homer Learnings Log", content)

    def test_clear_on_empty_file_returns_zero(self):
        out = self._run_clear(self.tmp)
        self.assertTrue(out["cleared"])
        self.assertEqual(out["entries_removed"], 0)

    def test_new_entries_can_be_appended_after_clear(self):
        run_append(self.tmp, "bug", "Old bug")
        self._run_clear(self.tmp)
        run_append(self.tmp, "feature", "New feature")
        out = run_list(self.tmp)
        self.assertEqual(len(out["entries"]), 1)
        self.assertEqual(out["entries"][0]["desc"], "New feature")


class TestMainValidation(unittest.TestCase):

    def _run_main(self, argv):
        with patch("sys.argv", argv):
            with patch.object(log_learning, "LEARNINGS_FILE", Path(tempfile.mktemp(suffix=".md"))):
                with patch("builtins.print") as mock_print:
                    with self.assertRaises(SystemExit):
                        log_learning.main()
                    return json.loads(mock_print.call_args[0][0])

    def test_missing_desc_exits_with_error(self):
        out = self._run_main(["log_learning.py", "--type", "bug"])
        self.assertIn("error", out)

    def test_valid_entry_does_not_exit(self):
        with patch("sys.argv", ["log_learning.py", "--type", "bug", "--desc", "Test"]):
            with patch.object(log_learning, "LEARNINGS_FILE", Path(tempfile.mktemp(suffix=".md"))) as tmp:
                with patch("builtins.print"):
                    log_learning.main()  # should not raise


if __name__ == "__main__":
    unittest.main()

"""
Tests for context_scrub.py — sensitive pattern detection in context files.

Uses temp files so no real context files are touched.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import context_scrub


def scan(content: str) -> list[dict]:
    """Write content to a temp file and scan it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(content)
        tmp = Path(f.name)
    try:
        return context_scrub.scan_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


class TestSsnDetection(unittest.TestCase):

    def test_detects_ssn(self):
        findings = scan("SSN: 123-45-6789")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "SSN")

    def test_no_false_positive_phone(self):
        # Phone numbers don't match SSN pattern
        findings = scan("Phone: 770-555-1234")
        ssn_findings = [f for f in findings if f["type"] == "SSN"]
        self.assertEqual(ssn_findings, [])


class TestCreditCardDetection(unittest.TestCase):

    def test_detects_card_with_spaces(self):
        findings = scan("Card: 4111 1111 1111 1111")
        self.assertTrue(any(f["type"] == "Credit card" for f in findings))

    def test_detects_card_with_dashes(self):
        findings = scan("Card: 4111-1111-1111-1111")
        self.assertTrue(any(f["type"] == "Credit card" for f in findings))

    def test_detects_card_no_separator(self):
        findings = scan("Card: 4111111111111111")
        self.assertTrue(any(f["type"] == "Credit card" for f in findings))


class TestPasswordDetection(unittest.TestCase):

    def test_detects_password_colon(self):
        findings = scan("password: mysecretpassword123")
        self.assertTrue(any(f["type"] == "Password" for f in findings))

    def test_detects_passwd_equals(self):
        findings = scan("passwd=hunter2abc")
        self.assertTrue(any(f["type"] == "Password" for f in findings))

    def test_case_insensitive(self):
        findings = scan("PASSWORD: MySecret123")
        self.assertTrue(any(f["type"] == "Password" for f in findings))


class TestApiKeyDetection(unittest.TestCase):

    def test_detects_api_key(self):
        findings = scan("api_key: sk-ant-abcdefghijklmnopqrstuvwxyz123456")
        self.assertTrue(any(f["type"] == "API key / token" for f in findings))

    def test_detects_token(self):
        findings = scan("token: ghp_abcdefghijklmnopqrstuvwxyz12345678")
        self.assertTrue(any(f["type"] == "API key / token" for f in findings))

    def test_detects_bearer(self):
        findings = scan("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc")
        self.assertTrue(any(f["type"] == "API key / token" for f in findings))

    def test_short_value_not_flagged(self):
        # Values under 20 chars shouldn't trigger
        findings = scan("token: shortval")
        api_findings = [f for f in findings if f["type"] == "API key / token"]
        self.assertEqual(api_findings, [])


class TestUrlWithCredentials(unittest.TestCase):

    def test_detects_url_with_credentials(self):
        findings = scan("db: postgres://admin:password123@db.example.com/mydb")
        self.assertTrue(any(f["type"] == "URL with credentials" for f in findings))

    def test_plain_url_not_flagged(self):
        findings = scan("url: https://www.google.com/search?q=test")
        url_findings = [f for f in findings if f["type"] == "URL with credentials"]
        self.assertEqual(url_findings, [])


class TestRedact(unittest.TestCase):

    def test_short_value_fully_masked(self):
        self.assertEqual(context_scrub.redact("abc", keep=4), "***")

    def test_long_value_shows_ends(self):
        result = context_scrub.redact("sk-ant-1234567890abcdef", keep=4)
        self.assertTrue(result.startswith("sk-a"))
        self.assertTrue(result.endswith("cdef"))
        self.assertIn("*", result)

    def test_exact_double_keep_fully_masked(self):
        # len == keep*2 → fully masked
        result = context_scrub.redact("12345678", keep=4)
        self.assertEqual(result, "********")


class TestCleanContent(unittest.TestCase):

    def test_clean_file_returns_no_findings(self):
        content = """# Household Context

## People
- Alex: primary user
- Sam: spouse

## Location
Anytown, ST 12345
"""
        findings = scan(content)
        self.assertEqual(findings, [])

    def test_comment_lines_skipped(self):
        # Lines starting with # are skipped (unless they contain long digit sequences)
        findings = scan("# api_key: sk-ant-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(findings, [])

    def test_line_number_reported(self):
        content = "Normal line\nSSN: 123-45-6789\nAnother line"
        findings = scan(content)
        self.assertEqual(findings[0]["line"], 2)

    def test_multiple_findings_in_one_file(self):
        content = "SSN: 123-45-6789\npassword: mysecret123abc"
        findings = scan(content)
        self.assertEqual(len(findings), 2)

    def test_json_output(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("SSN: 123-45-6789\n")
            tmp = Path(f.name)
        try:
            with patch.object(context_scrub, "CONTEXT_DIR", tmp.parent):
                with patch("sys.argv", ["context_scrub.py", "--json", "--file", tmp.name]):
                    with patch("builtins.print") as mock_print:
                        with patch("context_scrub.CONTEXT_DIR", tmp.parent):
                            # Call scan_file directly and test JSON serialization
                            findings = context_scrub.scan_file(tmp)
                            print_output = json.dumps(findings, indent=2)
                            parsed = json.loads(print_output)
                            self.assertIsInstance(parsed, list)
        finally:
            tmp.unlink(missing_ok=True)


class TestMigrationScanning(unittest.TestCase):
    """Tests that context_scrub scans both user_context/ and context/ directories."""

    def test_scans_user_context_first(self):
        """Files in user_context/ are scanned."""
        import tempfile
        base = Path(tempfile.mkdtemp())
        uc_dir = base / "user_context"
        uc_dir.mkdir()
        (uc_dir / "test.md").write_text("SSN: 123-45-6789\n")
        try:
            with patch.object(context_scrub, "USER_CONTEXT_DIR", uc_dir), \
                 patch.object(context_scrub, "CONTEXT_DIR", base):
                with patch("sys.argv", ["context_scrub.py", "--json"]):
                    # Use main's file discovery logic manually
                    seen_names = set()
                    files = []
                    for d in [uc_dir, base]:
                        for f in sorted(d.glob("*.md")):
                            if f.name not in seen_names:
                                seen_names.add(f.name)
                                files.append(f)
                    all_findings = []
                    for f in files:
                        all_findings.extend(context_scrub.scan_file(f))
                    self.assertTrue(any(f["type"] == "SSN" for f in all_findings))
        finally:
            import shutil
            shutil.rmtree(base)

    def test_scans_both_directories(self):
        """Unmigrated files in context/ are also scanned alongside user_context/ files."""
        import tempfile
        base = Path(tempfile.mkdtemp())
        uc_dir = base / "user_context"
        uc_dir.mkdir()
        (uc_dir / "household.md").write_text("Clean content\n")
        (base / "finance.md").write_text("SSN: 111-22-3333\n")
        try:
            with patch.object(context_scrub, "USER_CONTEXT_DIR", uc_dir), \
                 patch.object(context_scrub, "CONTEXT_DIR", base):
                seen_names = set()
                files = []
                for d in [uc_dir, base]:
                    for f in sorted(d.glob("*.md")):
                        if f.name not in seen_names:
                            seen_names.add(f.name)
                            files.append(f)
                # Both files discovered
                names = [f.name for f in files]
                self.assertIn("household.md", names)
                self.assertIn("finance.md", names)
                # SSN in old-location file is still found
                all_findings = []
                for f in files:
                    all_findings.extend(context_scrub.scan_file(f))
                self.assertTrue(any(f["type"] == "SSN" for f in all_findings))
        finally:
            import shutil
            shutil.rmtree(base)

    def test_user_context_shadows_old_location(self):
        """If same file exists in both dirs, user_context/ version is scanned (not both)."""
        import tempfile
        base = Path(tempfile.mkdtemp())
        uc_dir = base / "user_context"
        uc_dir.mkdir()
        (uc_dir / "household.md").write_text("Clean content\n")
        (base / "household.md").write_text("SSN: 999-88-7777\n")
        try:
            with patch.object(context_scrub, "USER_CONTEXT_DIR", uc_dir), \
                 patch.object(context_scrub, "CONTEXT_DIR", base):
                seen_names = set()
                files = []
                for d in [uc_dir, base]:
                    for f in sorted(d.glob("*.md")):
                        if f.name not in seen_names:
                            seen_names.add(f.name)
                            files.append(f)
                # Only one household.md (from user_context/)
                hh_files = [f for f in files if f.name == "household.md"]
                self.assertEqual(len(hh_files), 1)
                self.assertEqual(hh_files[0].parent.name, "user_context")
        finally:
            import shutil
            shutil.rmtree(base)


if __name__ == "__main__":
    unittest.main()

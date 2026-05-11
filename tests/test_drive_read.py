"""
Tests for drive_read.py — verifies in-memory-only file handling and content extraction.

Critical invariant: file content is NEVER written to disk.
gogcli's `drive download --out=-` streams bytes from the HTTP response directly
to stdout (verified at internal/cmd/drive.go:981-984); we capture stdout via
subprocess.PIPE into a Python bytes object. We snapshot the filesystem before
and after to assert no new files appear.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import drive_read


def _snapshot_tmpdir():
    return set(Path(tempfile.gettempdir()).iterdir())


def _patch_download(data: bytes):
    """Patch gogcli.download_bytes to return given bytes."""
    return patch.object(drive_read.gogcli, "download_bytes", return_value=data)


class TestInMemoryOnlyDownload(unittest.TestCase):
    """fetch_content_into_memory must not write any files to disk."""

    def test_pdf_never_written_to_disk(self):
        # Minimal PDF; pypdf may bail — that's fine, we just check no disk write.
        fake_pdf = b"%PDF-1.4\n%%EOF"
        before = _snapshot_tmpdir()
        with _patch_download(fake_pdf):
            drive_read.fetch_content_into_memory("tok", "fake-id", "application/pdf")
        self.assertEqual(_snapshot_tmpdir() - before, set())

    def test_google_doc_never_written_to_disk(self):
        fake_text = b"This is the document content."
        before = _snapshot_tmpdir()
        with _patch_download(fake_text):
            result = drive_read.fetch_content_into_memory(
                "tok", "fake-id", "application/vnd.google-apps.document"
            )
        self.assertEqual(_snapshot_tmpdir() - before, set())
        self.assertEqual(result, "This is the document content.")

    def test_google_sheet_never_written_to_disk(self):
        fake_csv = b"Category,Amount\nRent,2000\nGroceries,500"
        before = _snapshot_tmpdir()
        with _patch_download(fake_csv):
            result = drive_read.fetch_content_into_memory(
                "tok", "fake-id", "application/vnd.google-apps.spreadsheet"
            )
        self.assertEqual(_snapshot_tmpdir() - before, set())
        self.assertIn("Rent", result)
        self.assertIn("2000", result)

    def test_csv_never_written_to_disk(self):
        before = _snapshot_tmpdir()
        with _patch_download(b"col1,col2\nval1,val2\n"):
            drive_read.fetch_content_into_memory("tok", "fake-id", "text/csv")
        self.assertEqual(_snapshot_tmpdir() - before, set())


class TestDownloadArgvShape(unittest.TestCase):
    """gogcli.download_bytes must be called with the right args per mime type."""

    def _capture(self, mime: str) -> list:
        captured = {}

        def fake_dl(token, *args):
            captured["token"] = token
            captured["args"] = list(args)
            return b""

        with patch.object(drive_read.gogcli, "download_bytes", side_effect=fake_dl):
            drive_read.fetch_content_into_memory("tok", "F1", mime)
        return captured["args"]

    def test_pdf_uses_raw_download(self):
        self.assertEqual(self._capture("application/pdf"),
                         ["drive", "download", "F1", "--out=-"])

    def test_google_doc_uses_format_txt(self):
        self.assertEqual(self._capture("application/vnd.google-apps.document"),
                         ["drive", "download", "F1", "--format=txt", "--out=-"])

    def test_google_sheet_uses_format_csv(self):
        self.assertEqual(self._capture("application/vnd.google-apps.spreadsheet"),
                         ["drive", "download", "F1", "--format=csv", "--out=-"])

    def test_csv_uses_raw_download(self):
        self.assertEqual(self._capture("text/csv"),
                         ["drive", "download", "F1", "--out=-"])

    def test_docx_uses_raw_download(self):
        self.assertEqual(
            self._capture(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            ["drive", "download", "F1", "--out=-"],
        )


class TestUnsupportedTypes(unittest.TestCase):
    """Unsupported mime types must short-circuit before invoking gogcli."""

    def _assert_no_subprocess(self, mime: str, expected_substr: str):
        with patch.object(drive_read.gogcli, "download_bytes",
                          side_effect=AssertionError("must not be called")):
            result = drive_read.fetch_content_into_memory("tok", "fake-id", mime)
        self.assertIn(expected_substr.lower(), result.lower())

    def test_image_returns_unsupported_message(self):
        self._assert_no_subprocess("image/jpeg", "not supported")

    def test_xlsx_returns_unsupported_message(self):
        self._assert_no_subprocess(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "not supported",
        )

    def test_unknown_mime_returns_unsupported(self):
        with patch.object(drive_read.gogcli, "download_bytes",
                          side_effect=AssertionError("must not be called")):
            result = drive_read.fetch_content_into_memory(
                "tok", "fake-id", "application/octet-stream"
            )
        self.assertIn("Unsupported", result)


class TestContentExtraction(unittest.TestCase):
    """Content extraction post-download."""

    def test_google_sheet_returns_csv_text(self):
        fake_csv = b"Month,Income,Expenses\nJanuary,10000,8000\nFebruary,10000,7500"
        with _patch_download(fake_csv):
            result = drive_read.fetch_content_into_memory(
                "tok", "fake-id", "application/vnd.google-apps.spreadsheet"
            )
        self.assertIn("Month,Income,Expenses", result)
        self.assertIn("January,10000,8000", result)

    def test_google_doc_returns_plain_text(self):
        fake_text = b"Insurance Policy\nDeductible: $1000\nVehicles: Honda Civic, Hyundai Santa Fe"
        with _patch_download(fake_text):
            result = drive_read.fetch_content_into_memory(
                "tok", "fake-id", "application/vnd.google-apps.document"
            )
        self.assertIn("Deductible: $1000", result)

    def test_csv_returns_raw_content(self):
        fake_csv = b"date,amount,description\n2026-01-01,100.00,Groceries\n"
        with _patch_download(fake_csv):
            result = drive_read.fetch_content_into_memory("tok", "fake-id", "text/csv")
        self.assertIn("date,amount,description", result)
        self.assertIn("Groceries", result)

    def test_doc_extracts_text_runs(self):
        fake_doc = b"\x00\x00Hello PTC Schedule 2025\x00\x00"
        with _patch_download(fake_doc):
            result = drive_read.fetch_content_into_memory(
                "tok", "fake-id", "application/msword"
            )
        self.assertIn("Hello PTC Schedule 2025", result)


class TestExtractDocText(unittest.TestCase):
    """Verify .doc binary text extraction."""

    def test_extracts_printable_runs(self):
        data = b"\x00\x01\x02Hello, World\x00\x03\x04PTC Schedule\x00"
        result = drive_read.extract_doc_text(data)
        self.assertIn("Hello, World", result)
        self.assertIn("PTC Schedule", result)

    def test_ignores_short_runs(self):
        data = b"\x00Hi\x00\x00This is real content\x00"
        result = drive_read.extract_doc_text(data)
        self.assertNotIn("Hi", result)
        self.assertIn("This is real content", result)

    def test_empty_data_returns_empty(self):
        self.assertEqual(drive_read.extract_doc_text(b"\x00\x01\x02\x03").strip(), "")


class TestMimeTypeRecognition(unittest.TestCase):
    def test_csv_mime_type_recognised(self):
        self.assertEqual(drive_read.SUPPORTED_MIME_TYPES.get("text/csv"), "csv")

    def test_plain_mime_type_recognised(self):
        self.assertEqual(drive_read.SUPPORTED_MIME_TYPES.get("text/plain"), "plain")

    def test_msword_mime_type_recognised(self):
        self.assertEqual(drive_read.SUPPORTED_MIME_TYPES.get("application/msword"), "doc")

    def test_vnd_ms_word_recognised(self):
        self.assertEqual(drive_read.SUPPORTED_MIME_TYPES.get("application/vnd.ms-word"), "doc")


class TestExtractFileIdFromUrl(unittest.TestCase):
    """Verify file ID extraction from various Google URL formats."""

    def test_google_docs_edit_url(self):
        url = "https://docs.google.com/document/d/1I2nWsASxBWzH26gYOozaIBtqlcoIRFPf/edit?usp=drivesdk"
        self.assertEqual(drive_read.extract_file_id_from_url(url), "1I2nWsASxBWzH26gYOozaIBtqlcoIRFPf")

    def test_google_docs_share_url(self):
        url = "https://docs.google.com/document/d/ABC123XYZ/view"
        self.assertEqual(drive_read.extract_file_id_from_url(url), "ABC123XYZ")

    def test_google_sheets_url(self):
        url = "https://docs.google.com/spreadsheets/d/SHEET_ID_456/edit#gid=0"
        self.assertEqual(drive_read.extract_file_id_from_url(url), "SHEET_ID_456")

    def test_google_slides_url(self):
        url = "https://docs.google.com/presentation/d/SLIDES_ID_789/edit"
        self.assertEqual(drive_read.extract_file_id_from_url(url), "SLIDES_ID_789")

    def test_drive_file_url(self):
        url = "https://drive.google.com/file/d/FILE_ID_000/view?usp=sharing"
        self.assertEqual(drive_read.extract_file_id_from_url(url), "FILE_ID_000")

    def test_unrecognised_url_returns_none(self):
        self.assertIsNone(drive_read.extract_file_id_from_url("https://example.com/notadoc"))


class TestFetchPublicDoc(unittest.TestCase):
    def test_returns_text_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"Hello from a public doc"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertEqual(drive_read.fetch_public_doc("SOME_ID"), "Hello from a public doc")

    def test_returns_none_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            self.assertIsNone(drive_read.fetch_public_doc("SOME_ID"))


class TestSearchDrive(unittest.TestCase):
    """search_drive normalizes gogcli output."""

    def test_search_returns_single_match(self):
        with patch.object(drive_read.gogcli, "run", return_value={
            "files": [{"id": "abc123", "name": "budget_2026.xlsx",
                       "mimeType": "application/vnd.google-apps.spreadsheet",
                       "modifiedTime": "2026-01-01"}]
        }):
            result = drive_read.search_drive("tok", "budget")
        self.assertEqual(result["id"], "abc123")
        self.assertNotIn("_other_matches", result)

    def test_search_returns_first_with_others_noted(self):
        with patch.object(drive_read.gogcli, "run", return_value={
            "files": [
                {"id": "id1", "name": "budget_2026.xlsx",
                 "mimeType": "application/vnd.google-apps.spreadsheet",
                 "modifiedTime": "2026-01-01"},
                {"id": "id2", "name": "budget_2025.xlsx",
                 "mimeType": "application/vnd.google-apps.spreadsheet",
                 "modifiedTime": "2025-01-01"},
            ]
        }):
            result = drive_read.search_drive("tok", "budget")
        self.assertEqual(result["id"], "id1")
        self.assertIn("_other_matches", result)
        self.assertIn("budget_2025.xlsx", result["_other_matches"])

    def test_search_no_results_returns_error(self):
        with patch.object(drive_read.gogcli, "run", return_value={"files": []}):
            result = drive_read.search_drive("tok", "nonexistent")
        self.assertIn("error", result)

    def test_search_argv_shape(self):
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"files": []}

        with patch.object(drive_read.gogcli, "run", side_effect=fake_run):
            drive_read.search_drive("tok", "Alex's document")
        # Query is passed as a positional arg — gogcli handles its own escaping.
        self.assertEqual(captured["args"][:2], ["drive", "search"])
        self.assertIn("Alex's document", captured["args"])
        self.assertIn("--max=5", captured["args"])


class TestSizeLimit(unittest.TestCase):
    """Pre-flight size cap prevents loading huge files into memory."""

    def test_under_limit_returns_none(self):
        meta = {"size": str(drive_read.MAX_DOWNLOAD_BYTES // 2)}
        self.assertIsNone(drive_read.check_size_limit(meta))

    def test_over_limit_returns_error(self):
        meta = {"size": str(drive_read.MAX_DOWNLOAD_BYTES + 1)}
        msg = drive_read.check_size_limit(meta)
        self.assertIsNotNone(msg)
        self.assertIn("too large", msg.lower())

    def test_at_limit_passes(self):
        meta = {"size": str(drive_read.MAX_DOWNLOAD_BYTES)}
        self.assertIsNone(drive_read.check_size_limit(meta))

    def test_missing_size_returns_none(self):
        """Google Docs sometimes omit `size`; trust the caller."""
        self.assertIsNone(drive_read.check_size_limit({}))
        self.assertIsNone(drive_read.check_size_limit({"size": ""}))
        self.assertIsNone(drive_read.check_size_limit({"size": "0"}))

    def test_malformed_size_returns_none(self):
        self.assertIsNone(drive_read.check_size_limit({"size": "not-a-number"}))
        self.assertIsNone(drive_read.check_size_limit({"size": None}))


class TestGetById(unittest.TestCase):
    def test_unwraps_file_envelope(self):
        with patch.object(drive_read.gogcli, "run", return_value={
            "file": {"id": "f1", "name": "Doc", "mimeType": "application/pdf"}
        }):
            result = drive_read.get_by_id("tok", "f1")
        self.assertEqual(result, {"id": "f1", "name": "Doc", "mimeType": "application/pdf"})

    def test_returns_none_on_runtime_error(self):
        with patch.object(drive_read.gogcli, "run", side_effect=RuntimeError("not found")):
            self.assertIsNone(drive_read.get_by_id("tok", "missing"))


if __name__ == "__main__":
    unittest.main()

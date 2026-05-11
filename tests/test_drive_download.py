"""
Tests for drive_download.py — verifies file download to tmp/ and correct export routing.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import drive_download


class TestSafeFilename(unittest.TestCase):
    def test_adds_hash_and_extension(self):
        result = drive_download.safe_filename("budget", ".csv", "fileid123")
        self.assertTrue(result.endswith(".csv"))
        self.assertIn("_", result)  # hash appended

    def test_same_file_id_gives_same_name(self):
        a = drive_download.safe_filename("budget", ".csv", "fileid123")
        b = drive_download.safe_filename("budget", ".csv", "fileid123")
        self.assertEqual(a, b)

    def test_different_file_ids_give_different_names(self):
        a = drive_download.safe_filename("budget", ".csv", "fileid_aaa")
        b = drive_download.safe_filename("budget", ".csv", "fileid_bbb")
        self.assertNotEqual(a, b)

    def test_sanitizes_path_separators(self):
        result = drive_download.safe_filename("folder/file", ".csv", "x")
        self.assertNotIn("/", result)

    def test_sanitizes_null_bytes(self):
        result = drive_download.safe_filename("file\x00name", ".txt", "x")
        self.assertNotIn("\x00", result)

    def test_strips_wrong_extension_before_hashing(self):
        result = drive_download.safe_filename("budget.xlsx", ".csv", "x")
        self.assertTrue(result.endswith(".csv"))
        self.assertNotIn(".xlsx", result)


class TestDownloadMapCoverage(unittest.TestCase):
    def test_google_sheet_exports_as_csv(self):
        ftype, export_mime, ext = drive_download.DOWNLOAD_MAP["application/vnd.google-apps.spreadsheet"]
        self.assertEqual(export_mime, "text/csv")
        self.assertEqual(ext, ".csv")

    def test_google_doc_exports_as_txt(self):
        ftype, export_mime, ext = drive_download.DOWNLOAD_MAP["application/vnd.google-apps.document"]
        self.assertEqual(export_mime, "text/plain")
        self.assertEqual(ext, ".txt")

    def test_native_csv_raw_download(self):
        ftype, export_mime, ext = drive_download.DOWNLOAD_MAP["text/csv"]
        self.assertIsNone(export_mime)
        self.assertEqual(ext, ".csv")

    def test_pdf_raw_download(self):
        ftype, export_mime, ext = drive_download.DOWNLOAD_MAP["application/pdf"]
        self.assertIsNone(export_mime)
        self.assertEqual(ext, ".pdf")


class TestDownloadFile(unittest.TestCase):
    def _make_mock_downloader(self, data: bytes):
        """Simulates MediaIoBaseDownload writing data in a single chunk."""
        def fake_downloader(f, req, chunksize=None):
            mock = MagicMock()
            def next_chunk():
                f.write(data)
                return MagicMock(), True
            mock.next_chunk = next_chunk
            return mock
        return fake_downloader

    def test_native_csv_written_to_tmp(self):
        fake_csv = b"date,amount\n2026-01-01,100\n"
        mock_service = MagicMock()
        mock_service.files().get_media.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch.object(drive_download, "TMP_DIR", tmp_path):
                with patch("googleapiclient.http.MediaIoBaseDownload",
                           self._make_mock_downloader(fake_csv)):
                    result = drive_download.download_file(
                        mock_service, "fake-id", "text/csv", "transactions.csv"
                    )

        self.assertNotIn("error", result)
        self.assertTrue(result["name"].endswith(".csv"))
        self.assertIn("transactions", result["name"])
        self.assertEqual(result["size_bytes"], len(fake_csv))
        self.assertIn("sandbox_path", result)
        self.assertTrue(result["sandbox_path"].startswith("/home/sandbox/data/"))

    def test_google_sheet_uses_export_media(self):
        fake_csv = b"Month,Income\nJan,5000\n"
        mock_service = MagicMock()
        mock_service.files().export_media.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(drive_download, "TMP_DIR", Path(tmpdir)):
                with patch("googleapiclient.http.MediaIoBaseDownload",
                           self._make_mock_downloader(fake_csv)):
                    result = drive_download.download_file(
                        mock_service, "fake-id",
                        "application/vnd.google-apps.spreadsheet", "budget"
                    )

        mock_service.files().export_media.assert_called_once()
        self.assertNotIn("error", result)
        self.assertTrue(result["name"].endswith(".csv"))

    def test_unsupported_mime_returns_error(self):
        mock_service = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(drive_download, "TMP_DIR", Path(tmpdir)):
                result = drive_download.download_file(
                    mock_service, "fake-id", "application/x-unknown", "file.bin"
                )
        self.assertIn("error", result)

    def test_same_file_id_produces_same_filename(self):
        """Downloading the same Drive file twice gives the same local filename."""
        fake_csv = b"a,b\n1,2\n"
        mock_service = MagicMock()
        mock_service.files().get_media.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(drive_download, "TMP_DIR", Path(tmpdir)):
                with patch("googleapiclient.http.MediaIoBaseDownload",
                           self._make_mock_downloader(fake_csv)):
                    r1 = drive_download.download_file(
                        mock_service, "stable-file-id", "text/csv", "budget.csv"
                    )
                    r2 = drive_download.download_file(
                        mock_service, "stable-file-id", "text/csv", "budget.csv"
                    )
        self.assertEqual(r1["name"], r2["name"])

    def test_different_file_ids_produce_different_filenames(self):
        """Two Drive files with the same name get distinct local filenames."""
        fake_csv = b"a,b\n1,2\n"
        mock_service = MagicMock()
        mock_service.files().get_media.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(drive_download, "TMP_DIR", Path(tmpdir)):
                with patch("googleapiclient.http.MediaIoBaseDownload",
                           self._make_mock_downloader(fake_csv)):
                    r1 = drive_download.download_file(
                        mock_service, "file-id-2024", "text/csv", "budget.csv"
                    )
                    r2 = drive_download.download_file(
                        mock_service, "file-id-2025", "text/csv", "budget.csv"
                    )
        self.assertNotEqual(r1["name"], r2["name"])

    def test_size_limit_aborts_and_returns_error(self):
        """Files exceeding MAX_DOWNLOAD_BYTES are rejected and not left on disk."""
        # Simulate a downloader that writes more than the limit in one chunk
        oversized = b"x" * (drive_download.MAX_DOWNLOAD_BYTES + 1)

        def fake_downloader(f, req, chunksize=None):
            mock = MagicMock()
            def next_chunk():
                f.write(oversized)
                return MagicMock(), True
            mock.next_chunk = next_chunk
            return mock

        mock_service = MagicMock()
        mock_service.files().get_media.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch.object(drive_download, "TMP_DIR", tmp_path):
                with patch("googleapiclient.http.MediaIoBaseDownload", fake_downloader):
                    result = drive_download.download_file(
                        mock_service, "fake-id", "text/csv", "huge.csv"
                    )
            # Partial file must be cleaned up
            self.assertEqual(list(tmp_path.iterdir()), [])

        self.assertIn("error", result)
        self.assertIn("too large", result["error"].lower())


class TestPurgeOldTmpFiles(unittest.TestCase):
    def test_old_files_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            old_file = tmp_path / "old.csv"
            old_file.write_bytes(b"old")
            # Set mtime to 25 hours ago
            old_mtime = time.time() - (25 * 3600)
            import os
            os.utime(old_file, (old_mtime, old_mtime))

            with patch.object(drive_download, "TMP_DIR", tmp_path):
                drive_download.purge_old_tmp_files()

            self.assertFalse(old_file.exists())

    def test_recent_files_kept(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            new_file = tmp_path / "recent.csv"
            new_file.write_bytes(b"recent")

            with patch.object(drive_download, "TMP_DIR", tmp_path):
                drive_download.purge_old_tmp_files()

            self.assertTrue(new_file.exists())

    def test_nonexistent_tmp_dir_is_noop(self):
        with patch.object(drive_download, "TMP_DIR", Path("/nonexistent/tmp/dir")):
            drive_download.purge_old_tmp_files()  # must not raise


class TestSearchDrive(unittest.TestCase):
    def test_single_match_no_other_matches(self):
        mock_service = MagicMock()
        mock_service.files().list().execute.return_value = {
            "files": [{"id": "abc", "name": "budget.csv", "mimeType": "text/csv", "modifiedTime": ""}]
        }
        result = drive_download.search_drive(mock_service, "budget")
        self.assertEqual(result["id"], "abc")
        self.assertNotIn("_other_matches", result)

    def test_multiple_matches_notes_others(self):
        mock_service = MagicMock()
        mock_service.files().list().execute.return_value = {
            "files": [
                {"id": "id1", "name": "budget_2026.csv", "mimeType": "text/csv", "modifiedTime": ""},
                {"id": "id2", "name": "budget_2025.csv", "mimeType": "text/csv", "modifiedTime": ""},
            ]
        }
        result = drive_download.search_drive(mock_service, "budget")
        self.assertEqual(result["id"], "id1")
        self.assertIn("_other_matches", result)
        self.assertIn("budget_2025.csv", result["_other_matches"])

    def test_no_results_returns_error(self):
        mock_service = MagicMock()
        mock_service.files().list().execute.return_value = {"files": []}
        result = drive_download.search_drive(mock_service, "nonexistent")
        self.assertIn("error", result)


class TestChunkSize(unittest.TestCase):
    def test_downloader_uses_1mb_chunksize(self):
        """MediaIoBaseDownload must be called with chunksize=1MB to actually stream."""
        fake_csv = b"a,b\n1,2\n"
        mock_service = MagicMock()
        mock_service.files().get_media.return_value = MagicMock()

        captured = {}

        def fake_downloader(f, req, chunksize=None):
            captured["chunksize"] = chunksize
            mock = MagicMock()
            def next_chunk():
                f.write(fake_csv)
                return MagicMock(), True
            mock.next_chunk = next_chunk
            return mock

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(drive_download, "TMP_DIR", Path(tmpdir)):
                with patch("googleapiclient.http.MediaIoBaseDownload", fake_downloader):
                    drive_download.download_file(mock_service, "fake-id", "text/csv", "data.csv")

        self.assertEqual(captured["chunksize"], 1024 * 1024)


class TestExtractFileIdFromUrl(unittest.TestCase):
    def test_spreadsheet_url(self):
        url = "https://docs.google.com/spreadsheets/d/SHEET_ID/edit"
        self.assertEqual(drive_download.extract_file_id_from_url(url), "SHEET_ID")

    def test_drive_file_url(self):
        url = "https://drive.google.com/file/d/FILE123/view"
        self.assertEqual(drive_download.extract_file_id_from_url(url), "FILE123")

    def test_unrecognised_returns_none(self):
        self.assertIsNone(drive_download.extract_file_id_from_url("https://example.com/"))


if __name__ == "__main__":
    unittest.main()

"""
Tests for sheets.py — Google Sheets read/write.

Tests mock the Sheets API service so no real API calls are made.
Verifies correct API calls, response parsing, value handling, and error cases.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import sheets


def make_service():
    """Return a mock Sheets API service."""
    return MagicMock()


# ── create ───────────────────────────────────────────────────────────────────

class TestDoCreate(unittest.TestCase):

    def test_returns_sheet_id_url_and_tabs(self):
        service = make_service()
        service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "abc123",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc123/edit",
            "properties": {"title": "Family Budget 2026"},
            "sheets": [
                {"properties": {"title": "Expenses"}},
                {"properties": {"title": "Income"}},
            ],
        }
        with patch("builtins.print") as mock_print:
            sheets.do_create("Family Budget 2026", service, ["Expenses", "Income"])
            out = json.loads(mock_print.call_args[0][0])

        self.assertEqual(out["sheet_id"], "abc123")
        self.assertEqual(out["url"], "https://docs.google.com/spreadsheets/d/abc123/edit")
        self.assertEqual(out["title"], "Family Budget 2026")
        self.assertEqual(out["sheets"], ["Expenses", "Income"])

    def test_default_tab_when_no_names_given(self):
        service = make_service()
        service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "xyz",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/xyz/edit",
            "properties": {"title": "Grocery List"},
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }
        with patch("builtins.print") as mock_print:
            sheets.do_create("Grocery List", service, None)
            out = json.loads(mock_print.call_args[0][0])

        call_body = service.spreadsheets().create.call_args[1]["body"]
        self.assertEqual(call_body["sheets"][0]["properties"]["title"], "Sheet1")

    def test_correct_body_sent_to_api(self):
        service = make_service()
        service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "id1",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/id1/edit",
            "properties": {"title": "Test"},
            "sheets": [{"properties": {"title": "Tasks"}}, {"properties": {"title": "Done"}}],
        }
        with patch("builtins.print"):
            sheets.do_create("Test", service, ["Tasks", "Done"])

        body = service.spreadsheets().create.call_args[1]["body"]
        self.assertEqual(body["properties"]["title"], "Test")
        self.assertEqual(body["sheets"][0]["properties"]["title"], "Tasks")
        self.assertEqual(body["sheets"][0]["properties"]["index"], 0)
        self.assertEqual(body["sheets"][1]["properties"]["title"], "Done")
        self.assertEqual(body["sheets"][1]["properties"]["index"], 1)

    def test_api_error_exits(self):
        service = make_service()
        service.spreadsheets().create().execute.side_effect = Exception("Quota exceeded")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_create("Test", service, None)
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)


# ── info ──────────────────────────────────────────────────────────────────────

class TestDoInfo(unittest.TestCase):

    def test_returns_title_and_sheet_list(self):
        service = make_service()
        service.spreadsheets().get().execute.return_value = {
            "properties": {"title": "Family Budget 2026"},
            "sheets": [
                {"properties": {"title": "Expenses", "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
                {"properties": {"title": "Income",   "gridProperties": {"rowCount": 500,  "columnCount": 10}}},
            ],
        }
        with patch("builtins.print") as mock_print:
            sheets.do_info("sheet-id-123", service)
            out = json.loads(mock_print.call_args[0][0])

        self.assertEqual(out["title"], "Family Budget 2026")
        self.assertEqual(len(out["sheets"]), 2)
        self.assertEqual(out["sheets"][0]["name"], "Expenses")
        self.assertEqual(out["sheets"][0]["rows"], 1000)
        self.assertEqual(out["sheets"][0]["cols"], 26)
        self.assertEqual(out["sheets"][1]["name"], "Income")

    def test_spreadsheet_id_passed_to_api(self):
        service = make_service()
        service.spreadsheets().get().execute.return_value = {
            "properties": {"title": "Test"},
            "sheets": [],
        }
        with patch("builtins.print"):
            sheets.do_info("my-sheet-id", service)

        call_kwargs = service.spreadsheets().get.call_args
        self.assertEqual(call_kwargs[1]["spreadsheetId"], "my-sheet-id")

    def test_api_error_exits(self):
        service = make_service()
        service.spreadsheets().get().execute.side_effect = Exception("API error")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_info("bad-id", service)
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)

    def test_empty_sheets_list(self):
        service = make_service()
        service.spreadsheets().get().execute.return_value = {
            "properties": {"title": "Empty"},
            "sheets": [],
        }
        with patch("builtins.print") as mock_print:
            sheets.do_info("sheet-id", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertEqual(out["sheets"], [])


# ── read ──────────────────────────────────────────────────────────────────────

class TestDoRead(unittest.TestCase):

    def test_returns_range_and_values(self):
        service = make_service()
        service.spreadsheets().values().get().execute.return_value = {
            "range": "Expenses!A1:D3",
            "values": [
                ["Date", "Category", "Amount", "Notes"],
                ["2026-03-01", "Groceries", "143.50", "Kroger"],
                ["2026-03-05", "Gas", "62.00", "Shell"],
            ],
        }
        with patch("builtins.print") as mock_print:
            sheets.do_read("sheet-id", "Expenses!A1:D3", service)
            out = json.loads(mock_print.call_args[0][0])

        self.assertEqual(out["range"], "Expenses!A1:D3")
        self.assertEqual(len(out["values"]), 3)
        self.assertEqual(out["values"][0], ["Date", "Category", "Amount", "Notes"])
        self.assertEqual(out["values"][1][2], "143.50")

    def test_correct_params_passed_to_api(self):
        service = make_service()
        service.spreadsheets().values().get().execute.return_value = {
            "range": "Sheet1!A1:B2", "values": []
        }
        with patch("builtins.print"):
            sheets.do_read("my-sheet-id", "Sheet1!A1:B2", service)

        call_kwargs = service.spreadsheets().values().get.call_args[1]
        self.assertEqual(call_kwargs["spreadsheetId"], "my-sheet-id")
        self.assertEqual(call_kwargs["range"], "Sheet1!A1:B2")
        self.assertEqual(call_kwargs["valueRenderOption"], "FORMATTED_VALUE")

    def test_empty_range_returns_empty_values(self):
        service = make_service()
        service.spreadsheets().values().get().execute.return_value = {
            "range": "Sheet1!A1:A1",
        }
        with patch("builtins.print") as mock_print:
            sheets.do_read("sheet-id", "Sheet1!A1:A1", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertEqual(out["values"], [])

    def test_api_error_exits(self):
        service = make_service()
        service.spreadsheets().values().get().execute.side_effect = Exception("Forbidden")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_read("sheet-id", "Sheet1!A1", service)
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)


# ── write ─────────────────────────────────────────────────────────────────────

class TestDoWrite(unittest.TestCase):

    def test_returns_update_stats(self):
        service = make_service()
        service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Tasks!C5",
            "updatedRows": 1,
            "updatedColumns": 1,
            "updatedCells": 1,
        }
        with patch("builtins.print") as mock_print:
            sheets.do_write("sheet-id", "Tasks!C5", [["Complete"]], service)
            out = json.loads(mock_print.call_args[0][0])

        self.assertEqual(out["updated_range"], "Tasks!C5")
        self.assertEqual(out["updated_rows"], 1)
        self.assertEqual(out["updated_cols"], 1)
        self.assertEqual(out["updated_cells"], 1)

    def test_correct_params_passed_to_api(self):
        service = make_service()
        service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A1", "updatedRows": 1, "updatedColumns": 1, "updatedCells": 1,
        }
        values = [["Alice", "30"], ["Bob", "25"]]
        with patch("builtins.print"):
            sheets.do_write("my-sheet-id", "Sheet1!A1", values, service)

        call_kwargs = service.spreadsheets().values().update.call_args[1]
        self.assertEqual(call_kwargs["spreadsheetId"], "my-sheet-id")
        self.assertEqual(call_kwargs["range"], "Sheet1!A1")
        self.assertEqual(call_kwargs["valueInputOption"], "USER_ENTERED")
        self.assertEqual(call_kwargs["body"]["values"], values)

    def test_multi_row_write(self):
        service = make_service()
        service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A2:B3",
            "updatedRows": 2,
            "updatedColumns": 2,
            "updatedCells": 4,
        }
        with patch("builtins.print") as mock_print:
            sheets.do_write("sheet-id", "Sheet1!A2", [["Alice", "30"], ["Bob", "25"]], service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertEqual(out["updated_rows"], 2)
        self.assertEqual(out["updated_cells"], 4)

    def test_api_error_exits(self):
        service = make_service()
        service.spreadsheets().values().update().execute.side_effect = Exception("Write failed")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_write("sheet-id", "Sheet1!A1", [["x"]], service)
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)


# ── append ────────────────────────────────────────────────────────────────────

class TestDoAppend(unittest.TestCase):

    def test_returns_update_stats(self):
        service = make_service()
        service.spreadsheets().values().append().execute.return_value = {
            "updates": {
                "updatedRange": "Expenses!A11:D11",
                "updatedRows": 1,
                "updatedCells": 4,
            }
        }
        with patch("builtins.print") as mock_print:
            sheets.do_append("sheet-id", "Expenses", [["2026-03-16", "Gas", "55.00", "BP"]], service)
            out = json.loads(mock_print.call_args[0][0])

        self.assertEqual(out["updated_range"], "Expenses!A11:D11")
        self.assertEqual(out["updated_rows"], 1)
        self.assertEqual(out["updated_cells"], 4)

    def test_correct_params_passed_to_api(self):
        service = make_service()
        service.spreadsheets().values().append().execute.return_value = {"updates": {}}
        values = [["2026-03-16", "Groceries", "143.50"]]
        with patch("builtins.print"):
            sheets.do_append("my-sheet-id", "Expenses", values, service)

        call_kwargs = service.spreadsheets().values().append.call_args[1]
        self.assertEqual(call_kwargs["spreadsheetId"], "my-sheet-id")
        self.assertEqual(call_kwargs["range"], "Expenses")
        self.assertEqual(call_kwargs["valueInputOption"], "USER_ENTERED")
        self.assertEqual(call_kwargs["insertDataOption"], "INSERT_ROWS")
        self.assertEqual(call_kwargs["body"]["values"], values)

    def test_append_multiple_rows(self):
        service = make_service()
        service.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "Tasks!A5:B6", "updatedRows": 2, "updatedCells": 4}
        }
        with patch("builtins.print") as mock_print:
            sheets.do_append("sheet-id", "Tasks",
                             [["Buy filters", "Pending"], ["Call plumber", "Pending"]], service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertEqual(out["updated_rows"], 2)

    def test_api_error_exits(self):
        service = make_service()
        service.spreadsheets().values().append().execute.side_effect = Exception("Append failed")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_append("sheet-id", "Sheet1", [["x"]], service)
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)


# ── note ──────────────────────────────────────────────────────────────────────

class TestDoNote(unittest.TestCase):

    def _make_service_with_sheet(self, sheet_id=42, title="Sheet1"):
        service = make_service()
        service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"sheetId": sheet_id, "title": title}}]
        }
        service.spreadsheets().batchUpdate().execute.return_value = {}
        return service

    def test_single_cell_success(self):
        service = self._make_service_with_sheet()
        with patch("builtins.print") as mock_print:
            sheets.do_note("sheet-id", "Sheet1!B2", "Needs review", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["range"], "Sheet1!B2")
        self.assertEqual(out["note"], "Needs review")

    def test_batchupdate_payload_single_cell(self):
        service = self._make_service_with_sheet(sheet_id=99)
        with patch("builtins.print"):
            sheets.do_note("sheet-id", "Sheet1!B2", "Check this", service)
        body = service.spreadsheets().batchUpdate.call_args[1]["body"]
        req = body["requests"][0]["repeatCell"]
        self.assertEqual(req["range"]["sheetId"], 99)
        self.assertEqual(req["range"]["startRowIndex"], 1)   # row 2 → index 1
        self.assertEqual(req["range"]["endRowIndex"], 2)     # exclusive
        self.assertEqual(req["range"]["startColumnIndex"], 1)  # B → 1
        self.assertEqual(req["range"]["endColumnIndex"], 2)
        self.assertEqual(req["cell"]["note"], "Check this")
        self.assertEqual(req["fields"], "note")

    def test_range_covers_multiple_cells(self):
        service = self._make_service_with_sheet()
        with patch("builtins.print"):
            sheets.do_note("sheet-id", "Sheet1!B2:C5", "Estimated", service)
        body = service.spreadsheets().batchUpdate.call_args[1]["body"]
        req = body["requests"][0]["repeatCell"]["range"]
        self.assertEqual(req["startRowIndex"], 1)
        self.assertEqual(req["endRowIndex"], 5)    # row 5 exclusive
        self.assertEqual(req["startColumnIndex"], 1)  # B
        self.assertEqual(req["endColumnIndex"], 3)    # C+1

    def test_empty_note_clears_note(self):
        service = self._make_service_with_sheet()
        with patch("builtins.print") as mock_print:
            sheets.do_note("sheet-id", "Sheet1!A1", "", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["note"], "")
        body = service.spreadsheets().batchUpdate.call_args[1]["body"]
        self.assertEqual(body["requests"][0]["repeatCell"]["cell"]["note"], "")

    def test_no_sheet_prefix_uses_first_sheet(self):
        service = self._make_service_with_sheet(sheet_id=7, title="Expenses")
        with patch("builtins.print"):
            sheets.do_note("sheet-id", "A1", "note", service)
        body = service.spreadsheets().batchUpdate.call_args[1]["body"]
        self.assertEqual(body["requests"][0]["repeatCell"]["range"]["sheetId"], 7)

    def test_sheet_not_found_exits(self):
        service = self._make_service_with_sheet(title="Other")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_note("sheet-id", "Missing!A1", "note", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertIn("error", out)

    def test_invalid_range_exits(self):
        service = make_service()
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_note("sheet-id", "!!!bad", "note", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertIn("Invalid A1 range", out["error"])

    def test_partial_range_does_not_silently_match(self):
        """Sheet1!A1:B (missing row on end) must error, not silently apply to A1."""
        service = make_service()
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_note("sheet-id", "Sheet1!A1:B", "note", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertIn("Invalid A1 range", out["error"])

    def test_api_error_on_get_exits(self):
        service = make_service()
        service.spreadsheets().get().execute.side_effect = Exception("API down")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_note("sheet-id", "Sheet1!A1", "note", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertIn("error", out)

    def test_api_error_on_batchupdate_exits(self):
        service = self._make_service_with_sheet()
        service.spreadsheets().batchUpdate().execute.side_effect = Exception("Write denied")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                sheets.do_note("sheet-id", "Sheet1!A1", "note", service)
            out = json.loads(mock_print.call_args[0][0])
        self.assertIn("error", out)

    def test_col_to_num_multi_letter(self):
        """AA should map to column index 26."""
        service = self._make_service_with_sheet()
        with patch("builtins.print"):
            sheets.do_note("sheet-id", "Sheet1!AA1", "note", service)
        body = service.spreadsheets().batchUpdate.call_args[1]["body"]
        self.assertEqual(body["requests"][0]["repeatCell"]["range"]["startColumnIndex"], 26)


# ── note mode (main) ───────────────────────────────────────────────────────────

class TestNoteModeMain(unittest.TestCase):

    def _run_main(self, argv, service):
        with patch("sys.argv", argv):
            with patch("sheets.get_service", return_value=service):
                with patch("builtins.print") as mock_print:
                    with self.assertRaises(SystemExit):
                        sheets.main()
                    return json.loads(mock_print.call_args[0][0])

    def test_missing_sheet_id_exits(self):
        out = self._run_main(
            ["sheets.py", "--mode", "note", "--range", "Sheet1!A1", "--note", "x"],
            make_service(),
        )
        self.assertIn("error", out)

    def test_missing_range_exits(self):
        out = self._run_main(
            ["sheets.py", "--mode", "note", "--sheet-id", "abc", "--note", "x"],
            make_service(),
        )
        self.assertIn("error", out)

    def test_missing_note_flag_exits(self):
        out = self._run_main(
            ["sheets.py", "--mode", "note", "--sheet-id", "abc", "--range", "Sheet1!A1"],
            make_service(),
        )
        self.assertIn("error", out)


# ── values JSON parsing (in main) ─────────────────────────────────────────────

class TestValuesJsonParsing(unittest.TestCase):
    """Verify that invalid --values JSON exits cleanly before hitting the API."""

    def _run_main(self, argv):
        with patch("sys.argv", argv):
            with patch("sheets.get_service", return_value=make_service()):
                with patch("builtins.print") as mock_print:
                    with self.assertRaises(SystemExit):
                        sheets.main()
                    return json.loads(mock_print.call_args[0][0])

    def test_create_missing_title_exits_with_error(self):
        out = self._run_main(["sheets.py", "--mode", "create"])
        self.assertIn("error", out)

    def test_invalid_json_exits_with_error(self):
        out = self._run_main([
            "sheets.py", "--mode", "write",
            "--sheet-id", "abc", "--range", "Sheet1!A1",
            "--values", "not-json",
        ])
        self.assertIn("Invalid JSON", out["error"])

    def test_missing_values_exits_with_error(self):
        out = self._run_main([
            "sheets.py", "--mode", "write",
            "--sheet-id", "abc", "--range", "Sheet1!A1",
        ])
        self.assertIn("error", out)

    def test_missing_range_exits_with_error(self):
        out = self._run_main([
            "sheets.py", "--mode", "write",
            "--sheet-id", "abc",
            "--values", '[["x"]]',
        ])
        self.assertIn("error", out)

    def test_append_missing_sheet_exits_with_error(self):
        out = self._run_main([
            "sheets.py", "--mode", "append",
            "--sheet-id", "abc",
            "--values", '[["x"]]',
        ])
        self.assertIn("error", out)


# ── --values-file support ────────────────────────────────────────────────────

class TestValuesFile(unittest.TestCase):
    """Verify --values-file reads JSON from a file instead of CLI arg."""

    def _write_tmp(self, content, tmpdir):
        """Write content to a temp file inside the given directory."""
        import os
        path = os.path.join(tmpdir, "values.json")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_values_file_used_for_append(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = self._write_tmp('[["2026-03-16","Chase","Groceries","150.00"]]', tmpdir)
            service = make_service()
            service.spreadsheets().values().append().execute.return_value = {
                "updates": {"updatedRange": "Expenses!A2:D2", "updatedRows": 1, "updatedCells": 4}
            }
            with patch("sheets.ALLOWED_VALUES_DIR", Path(tmpdir)):
                with patch("sys.argv", [
                    "sheets.py", "--mode", "append",
                    "--sheet-id", "abc", "--sheet", "Expenses",
                    "--values-file", tmp_path,
                ]):
                    with patch("sheets.get_service", return_value=service):
                        with patch("builtins.print") as mock_print:
                            sheets.main()
                            out = json.loads(mock_print.call_args[0][0])
            self.assertEqual(out["updated_rows"], 1)
            call_kwargs = service.spreadsheets().values().append.call_args[1]
            self.assertEqual(call_kwargs["body"]["values"], [["2026-03-16", "Chase", "Groceries", "150.00"]])

    def test_values_file_used_for_write(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = self._write_tmp('[["Done"]]', tmpdir)
            service = make_service()
            service.spreadsheets().values().update().execute.return_value = {
                "updatedRange": "Sheet1!B2", "updatedRows": 1,
                "updatedColumns": 1, "updatedCells": 1,
            }
            with patch("sheets.ALLOWED_VALUES_DIR", Path(tmpdir)):
                with patch("sys.argv", [
                    "sheets.py", "--mode", "write",
                    "--sheet-id", "abc", "--range", "Sheet1!B2",
                    "--values-file", tmp_path,
                ]):
                    with patch("sheets.get_service", return_value=service):
                        with patch("builtins.print") as mock_print:
                            sheets.main()
                            out = json.loads(mock_print.call_args[0][0])
            self.assertEqual(out["updated_cells"], 1)

    def test_values_file_not_found_exits(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = str(Path(tmpdir) / "missing.json")
            with patch("sheets.ALLOWED_VALUES_DIR", Path(tmpdir)):
                with patch("sys.argv", [
                    "sheets.py", "--mode", "append",
                    "--sheet-id", "abc", "--sheet", "Expenses",
                    "--values-file", missing,
                ]):
                    with patch("sheets.get_service", return_value=make_service()):
                        with patch("builtins.print") as mock_print:
                            with self.assertRaises(SystemExit):
                                sheets.main()
                            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("not found", out["error"])

    def test_values_file_overrides_values_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = self._write_tmp('[["from-file"]]', tmpdir)
            service = make_service()
            service.spreadsheets().values().update().execute.return_value = {
                "updatedRange": "Sheet1!A1", "updatedRows": 1,
                "updatedColumns": 1, "updatedCells": 1,
            }
            with patch("sheets.ALLOWED_VALUES_DIR", Path(tmpdir)):
                with patch("sys.argv", [
                    "sheets.py", "--mode", "write",
                    "--sheet-id", "abc", "--range", "Sheet1!A1",
                    "--values", '[["from-cli"]]',
                    "--values-file", tmp_path,
                ]):
                    with patch("sheets.get_service", return_value=service):
                        with patch("builtins.print"):
                            sheets.main()
            call_kwargs = service.spreadsheets().values().update.call_args[1]
            self.assertEqual(call_kwargs["body"]["values"], [["from-file"]])

    def test_values_file_outside_allowed_dir_exits(self):
        """Files outside ALLOWED_VALUES_DIR must be rejected."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('[["sneaky"]]')
            f.flush()
            outside_path = f.name
        try:
            with patch("sys.argv", [
                "sheets.py", "--mode", "append",
                "--sheet-id", "abc", "--sheet", "Expenses",
                "--values-file", outside_path,
            ]):
                with patch("sheets.get_service", return_value=make_service()):
                    with patch("builtins.print") as mock_print:
                        with self.assertRaises(SystemExit):
                            sheets.main()
                        out = json.loads(mock_print.call_args[0][0])
            self.assertIn("must be inside", out["error"])
        finally:
            os.unlink(outside_path)

    def test_values_file_directory_path_exits(self):
        """Passing a directory instead of a file must return JSON error, not traceback."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("sheets.ALLOWED_VALUES_DIR", Path(tmpdir)):
                with patch("sys.argv", [
                    "sheets.py", "--mode", "append",
                    "--sheet-id", "abc", "--sheet", "Expenses",
                    "--values-file", tmpdir,  # a directory, not a file
                ]):
                    with patch("sheets.get_service", return_value=make_service()):
                        with patch("builtins.print") as mock_print:
                            with self.assertRaises(SystemExit):
                                sheets.main()
                            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)

    def test_values_file_invalid_json_exits(self):
        """Invalid JSON in values file must exit with structured error."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = self._write_tmp('not valid json [[[', tmpdir)
            with patch("sheets.ALLOWED_VALUES_DIR", Path(tmpdir)):
                with patch("sys.argv", [
                    "sheets.py", "--mode", "append",
                    "--sheet-id", "abc", "--sheet", "Expenses",
                    "--values-file", tmp_path,
                ]):
                    with patch("sheets.get_service", return_value=make_service()):
                        with patch("builtins.print") as mock_print:
                            with self.assertRaises(SystemExit):
                                sheets.main()
                            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("Invalid JSON", out["error"])


if __name__ == "__main__":
    unittest.main()

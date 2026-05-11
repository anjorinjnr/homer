---
name: sheets
description: "Read and write Google Sheets. Supports reading ranges, writing specific cells, and appending rows. Use when the user wants to view, update, or add data to a spreadsheet."
metadata: {"nanobot":{"emoji":"📋"}}
---

# Sheets Skill

Read and write Google Sheets. All four modes use the spreadsheet ID from the sheet URL:
`docs.google.com/spreadsheets/d/**<SHEET_ID>**/edit`

## Account selection

All modes accept `--account <name>` (default: `primary`, the household account).
Pass `--account <name>` (e.g. `--account personal`) when the sheet lives in a
different Google account.

```
exec python tools/sheets.py --mode read --sheet-id "<id>" --range "A1:C5" --account personal
```

## Modes at a glance

| Mode | When to use |
|------|-------------|
| `create` | Create a new spreadsheet with named tabs |
| `info` | Discover sheet names and dimensions before reading |
| `read` | Read a range of cells |
| `write` | Update specific cells |
| `append` | Add new rows to the bottom of a sheet |
| `note` | Attach a note/comment (yellow triangle) to a cell |

## create — Create a new spreadsheet

```
exec python tools/sheets.py --mode create --title "Family Budget 2026" --sheets "Expenses,Income,Summary"
```

`--sheets` is optional — omit for a single default tab called "Sheet1".

**Output:**
```json
{
  "sheet_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
  "url": "https://docs.google.com/spreadsheets/d/1Bxi.../edit",
  "title": "Family Budget 2026",
  "sheets": ["Expenses", "Income", "Summary"]
}
```

The `sheet_id` and `url` in the response can be shared with the user so they can open it immediately.

**Examples:**
```
# Simple single-tab sheet
exec python tools/sheets.py --mode create --title "Grocery List"

# Multi-tab tracker
exec python tools/sheets.py --mode create --title "Home Maintenance 2026" --sheets "Tasks,Completed,Vendors"
```

## info — List sheets and dimensions

```
exec python tools/sheets.py --mode info --sheet-id "<id>"
```

Use this first when you don't know the sheet/tab names or structure.

**Output:**
```json
{
  "title": "Family Budget 2026",
  "sheets": [
    { "name": "Expenses", "rows": 1000, "cols": 26 },
    { "name": "Income", "rows": 1000, "cols": 10 }
  ]
}
```

## read — Read a range

```
exec python tools/sheets.py --mode read --sheet-id "<id>" --range "Sheet1!A1:D20"
exec python tools/sheets.py --mode read --sheet-id "<id>" --range "Expenses"
```

`--range` uses A1 notation. Omitting the cell range (just the sheet name) reads all data.

**Output:**
```json
{
  "range": "Expenses!A1:D10",
  "values": [
    ["Date", "Category", "Amount", "Notes"],
    ["2026-03-01", "Groceries", "143.50", "Kroger"],
    ["2026-03-05", "Gas", "62.00", "Shell"]
  ]
}
```

**Examples:**
```
# Read first 20 rows of a specific range
exec python tools/sheets.py --mode read --sheet-id "<id>" --range "Expenses!A1:E20"

# Read entire sheet
exec python tools/sheets.py --mode read --sheet-id "<id>" --range "Budget"

# Read a single column
exec python tools/sheets.py --mode read --sheet-id "<id>" --range "Sheet1!A:A"
```

## write — Update specific cells

```
exec python tools/sheets.py --mode write --sheet-id "<id>" --range "Sheet1!B2" --values '[["Done"]]'
```

`--values` is a JSON 2D array — rows × columns. The top-left of `--range` is the anchor.

**Output:**
```json
{ "updated_range": "Sheet1!B2", "updated_rows": 1, "updated_cols": 1, "updated_cells": 1 }
```

**Examples:**
```
# Write a single cell
exec python tools/sheets.py --mode write --sheet-id "<id>" --range "Tasks!C5" --values '[["Complete"]]'

# Write multiple cells in a row
exec python tools/sheets.py --mode write --sheet-id "<id>" --range "Budget!B2:D2" --values '[["1200", "800", "400"]]'

# Write multiple rows
exec python tools/sheets.py --mode write --sheet-id "<id>" --range "Sheet1!A2" --values '[["Alice","30"],["Bob","25"]]'
```

## append — Add rows to a sheet

```
exec python tools/sheets.py --mode append --sheet-id "<id>" --sheet "Expenses" --values '[["2026-03-16","Groceries","150.00","Publix"]]'
```

Rows are inserted after the last row with data. `--sheet` is the tab name (no cell range needed).

**Output:**
```json
{ "updated_range": "Expenses!A11:D11", "updated_rows": 1, "updated_cells": 4 }
```

**Examples:**
```
# Log an expense
exec python tools/sheets.py --mode append --sheet-id "<id>" --sheet "Expenses" --values '[["2026-03-16","Gas","55.00","BP"]]'

# Add multiple rows at once
exec python tools/sheets.py --mode append --sheet-id "<id>" --sheet "Tasks" --values '[["Buy filters","Pending"],["Call plumber","Pending"]]'
```

## note — Attach a note/comment to a cell

```
exec python tools/sheets.py --mode note --sheet-id "<id>" --range "Sheet1!B2" --note "Needs review"
```

Attaches a yellow triangle note (comment) to the specified cell or range. If a range is provided, the same note is attached to every cell in that range. To remove a note, pass an empty string: `--note ""`.

**Range must be fully bounded** (e.g. `B2` or `B2:C5`). Open-ended ranges like `A:A` or `1:10` are not supported for notes and will return an error.

**Output:**
```json
{ "status": "success", "range": "Sheet1!B2", "note": "Needs review" }
```

**Examples:**
```
# Flag a transaction for review
exec python tools/sheets.py --mode note --sheet-id "<id>" --range "Expenses!E15" --note "Double check this receipt"

# Label a range of cells with a note
exec python tools/sheets.py --mode note --sheet-id "<id>" --range "Budget!B2:B10" --note "Estimated based on 2025 data"
```

## Formulas

**Always use formulas for calculated values — never hardcode totals, averages, or counts.**

`USER_ENTERED` (used by default) means Google evaluates formulas exactly as if typed in the cell. This applies especially to summary sheets that aggregate data from other tabs.

### Common formula patterns

```
# Sum a column from another sheet
=SUM(Expenses!E2:E1000)

# Sum by category (SUMIF) — use this for category breakdowns in summary sheets
=SUMIF(Expenses!B:B,"Groceries",Expenses!E:E)

# Count rows matching a category
=COUNTIF(Expenses!B:B,"Groceries")

# Average of a column
=AVERAGE(Expenses!E2:E1000)

# Total of all category subtotals
=SUM(B2:B7)

# Cross-sheet arithmetic (e.g. balance)
=Income!B2-Expenses!E2
```

### Summary sheet example

When creating a Summary tab that aggregates an Expenses tab (Date | Category | Description | Vendor | Amount):

```
# Write category labels and SUMIF formulas together — one write call per row
exec python tools/sheets.py --mode write --sheet-id "<id>" --range "Summary!A2" \
  --values '[["Groceries","=SUMIF(Expenses!B:B,\"Groceries\",Expenses!E:E)"],["Utilities","=SUMIF(Expenses!B:B,\"Utilities\",Expenses!E:E)"],["Dining","=SUMIF(Expenses!B:B,\"Dining\",Expenses!E:E)"]]'

# Then write the grand total referencing the formula cells above
exec python tools/sheets.py --mode write --sheet-id "<id>" --range "Summary!A5" \
  --values '[["TOTAL","=SUM(B2:B4)"]]'
```

**Rule:** if a value can be expressed as a formula referencing the source data, it must be. Only write raw values (strings, dates, numbers) for source data rows — never for derived/calculated cells.

## Passing values via file (recommended for bulk data)

When writing or appending many rows (e.g., CSV imports, batch transactions), use `--values-file` instead of `--values` to avoid shell quoting issues with brackets and special characters.

1. Use the native `write_file` tool to save the JSON 2D array to `{HOMER_WORKSPACE}/tmp/values.json`
2. Pass the file path with `--values-file` via `exec`

```
# Step 1: use the write_file tool (path + content parameters) to create:
#   path: {HOMER_WORKSPACE}/tmp/values.json
#   content: [["2026-03-16","Chase","Groceries","150.00"],["2026-03-17","Chase","Gas","42.50"]]

# Step 2: append using the file
exec {HOMER_VENV} {HOMER_TOOLS}/sheets.py --mode append --sheet-id "<id>" --sheet "Expenses" --values-file {HOMER_WORKSPACE}/tmp/values.json
```

`--values-file` works with both `write` and `append` modes. If both `--values` and `--values-file` are provided, `--values-file` takes precedence.

**Use `--values-file` whenever the values contain special characters, or when appending more than a few rows.** Use inline `--values` only for simple single-cell writes like `'[["Done"]]'`.

## Tips

- **Use `info` first** if you don't know the sheet structure — it tells you tab names and dimensions
- **Use `read` before `write`** to check current values and find the right range
- **`--values` is always a 2D array** — even a single cell needs `[["value"]]`
- **`append` is safer than `write`** for logs and trackers — it never overwrites existing data
- **Sheet ID is in the URL** — share it with Homer or paste the full URL and Homer will extract the ID

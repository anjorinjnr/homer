---
name: finance
description: Answer questions about household finances — account balances, spending, transactions, and monthly reports. Uses live Plaid data from the family joint spending account.
metadata: {"nanobot":{"always":false,"emoji":"💰"}}
---

# Finance Skill

Homer answers on-demand finance questions using live Plaid data. All data is fetched live — never estimated or recalled from memory. Any household member can ask about finances.

## Rules

- Always fetch live — never guess or use prior conversation context for balances or amounts.
- Do NOT save Plaid output to memory.
- If plaid_fetch.py fails, suggest running `python tools/plaid_link.py --institution ally` to re-authenticate.
- Amounts are in USD. Positive transactions = outflow (debits). Negative = inflow (credits).

## On-demand questions

**Balance:**
```
{HOMER_VENV} {HOMER_TOOLS}/plaid_fetch.py --balances
```
Answer: "Your Ally family account balance is $X,XXX."

**Recent spending summary:**
```
{HOMER_VENV} {HOMER_TOOLS}/plaid_fetch.py --summary --days 30
{HOMER_VENV} {HOMER_TOOLS}/plaid_fetch.py --summary --days 7
```
Answer from the `spending_by_category` field. Summarize naturally — do not dump raw JSON.

**Transactions:**
```
{HOMER_VENV} {HOMER_TOOLS}/plaid_fetch.py --transactions --days 30
```
Use to answer "what did I spend on X" or "show me recent transactions".

**Spending report (historical or ad-hoc):**
```
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --month YYYY-MM           # specific month
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --period biweekly --anchor 2026-05-01
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --period weekly
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --sheet-id <id>           # use a specific sheet
```
If asked about a specific past period, run with the matching flags. Output includes `period_label`, full breakdown, `sheet_id`, and `sheet_url`. Format the response the same way as the heartbeat report message.

## budget_check.py

```
{HOMER_VENV} {HOMER_TOOLS}/budget_check.py --status
{HOMER_VENV} {HOMER_TOOLS}/budget_check.py --check-alerts
{HOMER_VENV} {HOMER_TOOLS}/budget_check.py --institution ally
```
Checks spending against the Google Sheet budget.
On error: returns `{"error": "..."}` and exits 1.

## plaid_balance_check.py

```
{HOMER_VENV} {HOMER_TOOLS}/plaid_balance_check.py
{HOMER_VENV} {HOMER_TOOLS}/plaid_balance_check.py --threshold 25000
{HOMER_VENV} {HOMER_TOOLS}/plaid_balance_check.py --institution chase --account-mask 1234
```
Checks the account balance against a threshold. Outputs JSON or `SKIP`.
SKIP → tick, silence. Otherwise send to each recipient:
```
⚠️ Family account balance is $[balance] — below the $[threshold] threshold. Please review.
```

## plaid_monthly_report.py (heartbeat message format)

```
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py                                       # previous month, defaults
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --month 2026-02
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --institution chase --account-mask 1234
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --period biweekly --anchor 2026-05-01
{HOMER_VENV} {HOMER_TOOLS}/plaid_monthly_report.py --sheet-id <id>                       # bind to a specific sheet
```
Output: JSON summary (`period_label`, `inflow`, `outflow`, `breakdown`, `uncategorized`, `sheet_id`, `sheet_url`) or `SKIP: <reason>`. Monthly runs also emit `month` for legacy callers.

**Sheet handling:** if neither `--sheet-id` nor the `PLAID_SPENDING_SHEET_ID` env var is set, the tool creates a new spreadsheet on first run and emits `created_sheet: true` alongside the new `sheet_id`. When that flag is present, persist the id onto the recurring task so future runs append to the same sheet:
```
{HOMER_VENV} {HOMER_TOOLS}/tasks_update.py --edit "<task name>" --field SheetId=<id>
```

The tool writes to two tabs: `Summary` and `Transactions`. Tenants migrating from an older sheet that has a tab named `Monthly Summary` should rename it to `Summary` so the period rows continue to accumulate in the same place; otherwise the tool will create a fresh `Summary` tab alongside the old one.

SKIP → tick, silence. Otherwise format and send to each recipient:
```
📊 [period_label] Spending Report

Inflow:  $X,XXX
Outflow: $X,XXX

[Category]: $X,XXX
[Category]: $X,XXX
...

[If uncategorized] ❓ [N] transaction(s) need labels — reply with a label for each, e.g. "Check Paid → Personal Checks".

Full report: [sheet_url]
```

**Payee labeling follow-up:** When the report includes uncategorized transactions, display them to the user and ask them to provide a label for each. When the user replies with labels, call `payee_label_add.py` once per label. Future reports will categorize these automatically.

Example exchange:
> ❓ 2 transactions need labels:
> - Check Paid #1012: $962.43
> - ZELLE PMT: $500.00
> What category should I use for each?

User replies → call `payee_label_add.py --payee "Check Paid" --label "Personal Checks"` and `payee_label_add.py --payee "ZELLE PMT" --label "Transfers"` → confirm back.

## Automated tasks (heartbeat — do not run on-demand)

- **Daily balance check** — runs via heartbeat at 9am. Alerts if balance < $20k.
- **Monthly spending report** — runs via heartbeat on 1st of each month at 8am. Writes to Google Sheets and sends summary.

Each task has an `Account` field (last 4 digits of the account) which is passed as `--account-mask` to the script.

To change recipients, frequency, threshold, or account: edit `agent/HEARTBEAT.md` directly. No code changes needed.

## Setting up a new automated task for a different account

If asked to set up a balance check or monthly report for a different account:

1. Fetch all linked accounts to find the mask:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/plaid_fetch.py --balances
   ```
2. Identify the account by name — the `mask` field is the last 4 digits.
3. Confirm with the user: "I found [Account Name] ending in XXXX. Should I set up the [task] for that account?"
4. Add the task to `agent/HEARTBEAT.md` with `Account: XXXX` (the mask from step 2).

Example task entry:
```
### Balance check — Chase
Type: system
Schedule: 2026-04-01 09:00
Recur: every 1 day
Recipients: primary:whatsapp
Account: 1234
Institution: chase
```

For a spending report task, the additional fields are:
```
### Spending report
Type: system
Schedule: 2026-06-01 08:00
Recur: every 1 month            # or "every 2 weeks", "every 1 week"
Recipients: primary:whatsapp
Account: 1234
Institution: chase
SheetId: <google_sheet_id>      # populated after the first run (created_sheet: true)
Period: monthly                 # monthly | biweekly | weekly
Anchor: 2026-05-01              # only required for biweekly
```
On first run leave `SheetId` blank — the tool will create the sheet and the heartbeat handler should write the returned id back to the task with `tasks_update.py --field SheetId=<id>`.

## Budget tracking

Homer tracks spending against the household budget using the family Google Sheet.

### Storage rules
- **Google Sheets is the single source of truth** — never duplicate budget data into local files or SQLite.
- All budget reads go through `sheets.py` (or `budget_check.py` which reads the sheet directly).
- Homer stores only the sheet ID (not the budget data itself) — in `context/finance.md` using `context_updater.py` (see flows below).
- Never write budget category amounts back to local files.

### Required sheet structure

`budget_check.py` reads a tab named exactly **`Budget`** (case-sensitive) from the configured sheet:

```
Tab name:  Budget  (exact, case-sensitive)
Column A:  Category name  (e.g. "Groceries", "Gas", "Mortgage")
Column B:  Monthly budget amount  (numeric, e.g. 800 or $800 — $ and commas stripped automatically)
Row 1:     Optional header row (auto-skipped if Column A is non-numeric)
```

The sheet ID is stored in `context/finance.md` via `context_updater.py` and read back by `budget_check.py` on startup.

### Category matching

`budget_check.py` matches Plaid transaction categories to budget sheet categories using three passes (in order):
1. Exact match (case-insensitive)
2. Normalized match (lowercase, strip spaces)
3. Substring match (either direction — "food" matches "Food and Drink", "Food and Drink" matches "Food")

Categories that appear in Plaid spending but have no matching budget line are reported as `unbudgeted`. Homer must flag these to the user so they can add new budget lines or rename existing ones to improve matching.

### Budget setup flows

#### Flow 1: No budget exists
When the user says they have no budget or want to create one from scratch:

1. Run Plaid to get a realistic picture of their spending categories:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/plaid_fetch.py --summary --days 90
   ```
2. From the `spending_by_category` output, propose a starter budget — list each category and a suggested monthly amount based on the 90-day average. Ask the user to confirm or adjust amounts before creating anything.
3. Once confirmed, create a new Google Sheet with a "Budget" tab:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/sheets.py --mode create --title "Family Budget" --sheets "Budget"
   ```
   Note the `sheet_id` and `url` from the JSON output.
4. Write the confirmed categories and amounts to `Budget!A:B`:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/sheets.py --mode write --sheet-id <id> --range "Budget!A1" --values '[["Category","Monthly Budget"],["Groceries",800],["Gas",200],...]'
   ```
5. Store the sheet ID in `context/finance.md`:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/context_updater.py --file finance --section Accounts --key Budget-Sheet-ID --value <id> --source Homer
   ```
6. Share the sheet URL with the user: "Your budget sheet is ready at <url> — you can edit it directly anytime."
7. Confirm it works: `{HOMER_VENV} {HOMER_TOOLS}/budget_check.py --status`

#### Flow 2: User has an existing budget sheet (unknown format)
When the user says they already have a budget in Google Sheets:

1. Ask for the sheet URL or ID.
2. Inspect the sheet structure:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/sheets.py --mode info --sheet-id <id>
   ```
3. Check whether a "Budget" tab exists in the `sheets` list from the output.
4. If a "Budget" tab exists, read its contents:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/sheets.py --mode read --sheet-id <id> --range "Budget!A:B"
   ```
   Evaluate: does Column A contain category names? Does Column B contain numeric amounts?
5. **If the structure is correct** — store the ID and verify:
   ```
   {HOMER_VENV} {HOMER_TOOLS}/context_updater.py --file finance --section Accounts --key Budget-Sheet-ID --value <id> --source Homer
   {HOMER_VENV} {HOMER_TOOLS}/budget_check.py --status
   ```
   Confirm to the user: "Your existing budget sheet is connected and working."
6. **If the structure is wrong or there is no "Budget" tab** — explain what is needed:
   - "Homer needs a tab named exactly `Budget` with Category in column A and monthly amount in column B."
   - Describe what was found vs. what is needed.
   - Propose how to restructure (e.g., rename the tab, move data to columns A and B), preserving existing data.
   - Ask for confirmation before making any changes.
7. If the user confirms restructuring:
   - Use `--mode write` to populate the correctly-structured `Budget` tab with the categories and amounts sourced from wherever they live in the existing sheet.
   - Store the sheet ID: `{HOMER_VENV} {HOMER_TOOLS}/context_updater.py --file finance --section Accounts --key Budget-Sheet-ID --value <id> --source Homer`
   - Verify with `budget_check.py --status`.

#### Flow 3: Sheet ID not configured (budget_check returns error)
When `budget_check.py` returns `{"error": "budget_sheet_id not configured"}`:

- Ask the user: "Do you have an existing budget in Google Sheets, or would you like to create one?"
- If they have one: follow **Flow 2**.
- If they don't: follow **Flow 1**.

### On-demand budget queries

**Full budget status (how are we doing this month?):**
```
{HOMER_VENV} {HOMER_TOOLS}/budget_check.py --status
```
Answer from the `categories` field. For each category report: budget, actual, remaining, % used, projected end-of-month total, and status (on_track/warning/over). Surface `unbudgeted` categories and `unmapped_budget_lines` if present.

Example reply format:
```
📊 March 2026 Budget — Day 22 of 31

Groceries: $612 of $800 (77%) — ⚠️ warning (projected $861)
Gas:       $145 of $200 (73%) — ✅ on track
Utilities:  $310 of $300 (103%) — 🔴 over budget

Unbudgeted spend: ENTERTAINMENT $45
Total: $8,432 of $12,000 budget
```

**Budget for a specific category:**
Run `--status` and filter the output to the category the user asked about.

**Updating a budget category:**
Homer does NOT have a budget write tool. Direct the user to edit the Google Sheet directly at the URL stored in `context/finance.md`. Never duplicate the budget amount into a local file.

### Alert behavior
- The heartbeat "Budget alert check" task runs every 3 days via `budget_check.py --check-alerts`.
- Alerts fire only when a category status worsens (on_track→warning, warning→over, on_track→over).
- If `alerts` is empty, Homer stays silent — no "nothing to report" message.
- Alert dedup state is stored in `data/budget_alert_state.json` (auto-created, gitignored).

Alert message format (when `alerts` is non-empty):
```
⚠️ Budget Alert — [Month]

[Category]: $[actual] spent of $[budget] budget ([pct_used]%) — [status]
...

Reply "budget status" for the full breakdown.
```
Note: Each alert includes a `"month"` field. When alerts span two months (e.g. days 1–3 of a new month), the header shows the current month; append the alert's month in parentheses after the status for any alert from the prior month.

## Analyzing Chase CSV exports

Chase credit cards and checking accounts export different column names:
- Credit card: `Transaction Date`, `Post Date`, `Description`, `Category`, `Type`, `Amount`
- Checking:    `Details`, `Posting Date`, `Description`, `Amount`, `Type`, `Balance`

When writing sandbox scripts that analyze Chase CSVs, detect the format first:
```python
import pandas as pd

df = pd.read_csv("/home/sandbox/data/transactions.csv")
cols_lower = {{c.strip().lower() for c in df.columns}}

if "transaction date" in cols_lower:  # credit card
    df["date"] = pd.to_datetime(df["Transaction Date"])
    df["description"] = df["Description"].str.strip()
    df["amount"] = pd.to_numeric(df["Amount"])
    df["type"] = ""
    df["category"] = df.get("Category", pd.Series([""] * len(df))).fillna("").str.strip()
    df["balance"] = ""
elif "posting date" in cols_lower:    # checking
    df["date"] = pd.to_datetime(df["Posting Date"])
    df["description"] = df["Description"].str.strip()
    df["amount"] = pd.to_numeric(df["Amount"])
    df["type"] = df.get("Details", pd.Series([""] * len(df))).fillna("").str.strip()
    df["category"] = ""
    df["balance"] = pd.to_numeric(df["Balance"])
else:
    raise ValueError(f"Unrecognized Chase CSV format. Columns: {{list(df.columns)}}")
```

## Payee labels

Unknown transaction payees are stored in `context/payee_labels.json` as `{"payee substring": "Category"}`.
Matching is case-insensitive substring. To add or update a mapping:
```
{HOMER_VENV} {HOMER_TOOLS}/payee_label_add.py --payee "Check Paid" --label "Personal Checks"
```
Output: `{"status": "added"|"updated", "payee": "...", "label": "..."}` or `{"error": "..."}` + exit 1.

The household's existing payee → category mappings live in `context/payee_labels.json` (per-tenant). On a fresh setup this file may be empty; teach Homer over time as the user labels uncategorized transactions in the report follow-up flow.

## Examples

### Checking the balance
User: "How much is in the family account?"
Homer: "Ally family account balance is $24,312.50."

### Spending breakdown
User: "What did we spend on groceries this month?"
Homer: "Groceries this month: $623.18 across 12 transactions. Kroger ($342), Publix ($189), Costco ($92). You're at 78% of the $800 budget with 9 days left — on track."

### Budget status
User: "How's the budget looking?"
Homer: "March 2026 Budget — Day 22 of 31

Groceries: $623 of $800 (78%) — on track
Gas: $145 of $200 (73%) — on track
Utilities: $310 of $300 (103%) — over budget
Dining Out: $278 of $250 (111%) — over budget

Total: $6,840 of $9,500 budget

Utilities went over from the EMC bill. Dining out passed — the anniversary dinner pushed it."

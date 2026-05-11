#!/usr/bin/env python3
"""
plaid_monthly_report.py — Spending report for a household account, written to Google Sheets.

Despite the legacy filename, supports monthly, biweekly, and weekly periods.

Usage:
    python tools/plaid_monthly_report.py                                # previous month, defaults
    python tools/plaid_monthly_report.py --month 2026-02                # explicit month
    python tools/plaid_monthly_report.py --period biweekly --anchor 2026-05-01
    python tools/plaid_monthly_report.py --period weekly                # last ISO week
    python tools/plaid_monthly_report.py --sheet-id <id>                # use a specific sheet
    python tools/plaid_monthly_report.py --dry-run                      # skip Sheets write

If neither --sheet-id nor PLAID_SPENDING_SHEET_ID is set, a new spreadsheet is
created on first run. Capture sheet_id from the JSON output and persist it on
the recurring task so subsequent runs append to the same sheet.

Output:
    SKIP: <reason>          — nothing to report
    <JSON>                  — structured summary for Homer to send

JSON shape:
    {
      "period_label": "April 2026" | "Apr 1 – Apr 14, 2026" | ...,
      "period_start": "2026-04-01",
      "period_end": "2026-04-30",
      "month": "April 2026",                  # back-compat: present for monthly only
      "inflow": 19005.63,
      "outflow": 25678.41,
      "breakdown": [{"category": "Mortgage", "amount": 8212.22}, ...],
      "uncategorized": [{"date": "...", "name": "...", "amount": ...}, ...],
      "sheet_id": "1aBcD...",
      "sheet_url": "https://docs.google.com/spreadsheets/d/...",
      "created_sheet": true                   # only present when this run created the sheet
    }

Required env (in secrets/.env):
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV, PLAID_ACCESS_TOKEN_<INSTITUTION>

Sheet auth: routed through google_auth.build_service_or_exit (multi-account tokens).
"""

import argparse
import calendar
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from google_auth import build_service_or_exit
from plaid_utils import FAMILY_SPEND_MASK, DEFAULT_INSTITUTION, load_env, get_plaid_client, account_matches

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE_DIR = REPO_ROOT / "context" / ".nanobot_workspace"
PAYEE_LABELS_FILE = WORKSPACE_DIR / "state" / "payee_labels.json"

SHEET_TAB_SUMMARY = "Summary"
SHEET_TAB_TRANSACTIONS = "Transactions"
SHEET_URL_TEMPLATE = "https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
DEFAULT_SHEET_TITLE = "Homer Spending Tracker"

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

PERIOD_MONTHLY = "monthly"
PERIOD_BIWEEKLY = "biweekly"
PERIOD_WEEKLY = "weekly"


# ── Payee labels ──────────────────────────────────────────────────────────────

def load_payee_labels() -> dict:
    """Load tenant-defined payee → category mappings.

    No baked-in defaults: tenants teach Homer over time via payee_label_add.py.
    Existing labels live in <workspace>/state/payee_labels.json.
    """
    if not PAYEE_LABELS_FILE.exists():
        return {}
    try:
        return json.loads(PAYEE_LABELS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ── Plaid txn fetch ──────────────────────────────────────────────────────────

def fetch_transactions(client, access_token: str, start: date, end: date,
                       account_ids: list[str] | None = None) -> list[dict]:
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

    options_kwargs: dict = {"count": 500, "offset": 0}
    if account_ids:
        options_kwargs["account_ids"] = account_ids

    request = TransactionsGetRequest(
        access_token=access_token,
        start_date=start,
        end_date=end,
        options=TransactionsGetRequestOptions(**options_kwargs),
    )
    response = client.transactions_get(request)

    txns = []
    for t in response["transactions"]:
        if t.get("pending"):
            continue
        txns.append({
            "date": str(t["date"]),
            "name": t.get("name", ""),
            "merchant": t.get("merchant_name") or t.get("name", ""),
            "amount": float(t["amount"]),
            "plaid_category": (t.get("personal_finance_category") or {}).get("primary", "")
                               or (t.get("category") or [""])[0],
            "account": t.get("account_id", ""),
        })

    return sorted(txns, key=lambda x: x["date"])


# ── Categorization ────────────────────────────────────────────────────────────

def categorize_transaction(txn: dict, payee_labels: dict) -> str | None:
    name = txn.get("name", "") or txn.get("merchant", "")
    name_lower = name.lower()
    for payee, label in payee_labels.items():
        if payee.lower() in name_lower:
            return label
    return None


# ── Period windows ────────────────────────────────────────────────────────────

def previous_month() -> tuple[int, int]:
    today = date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def monthly_window(year: int, month: int) -> tuple[date, date, str]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day), f"{MONTH_NAMES[month]} {year}"


def biweekly_window(anchor: date, today: date | None = None) -> tuple[date, date, str]:
    """Most recent completed 14-day window aligned to anchor.

    If anchor is today or earlier but no full 14d period has elapsed since,
    returns the 14d window ending today (catch-up for the first run).
    """
    today = today or date.today()
    days_since = (today - anchor).days
    completed_periods = days_since // 14
    if completed_periods <= 0:
        end = today
    else:
        end = anchor + timedelta(days=completed_periods * 14 - 1)
    start = end - timedelta(days=13)
    return start, end, f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"


def weekly_window(today: date | None = None) -> tuple[date, date, str]:
    """Last completed ISO week (Mon–Sun)."""
    today = today or date.today()
    # ISO weekday: Mon=1..Sun=7. Last Sunday = today - (weekday % 7)
    days_to_last_sunday = today.isoweekday() % 7
    end = today - timedelta(days=days_to_last_sunday) if days_to_last_sunday else today - timedelta(days=7)
    start = end - timedelta(days=6)
    return start, end, f"Week of {start.strftime('%b %-d, %Y')}"


def resolve_period(args) -> tuple[date, date, str]:
    if args.month:
        try:
            year_s, month_s = args.month.split("-")
            y, m = int(year_s), int(month_s)
            if not (1 <= m <= 12):
                raise ValueError
        except (ValueError, AttributeError):
            print(f"SKIP: Invalid --month '{args.month}'. Use YYYY-MM format.")
            sys.exit(0)
        return monthly_window(y, m)

    if args.period == PERIOD_BIWEEKLY:
        if not args.anchor:
            print("SKIP: --period biweekly requires --anchor YYYY-MM-DD")
            sys.exit(0)
        try:
            anchor = date.fromisoformat(args.anchor)
        except ValueError:
            print(f"SKIP: Invalid --anchor '{args.anchor}'. Use YYYY-MM-DD.")
            sys.exit(0)
        if anchor > date.today():
            # Future anchor: no period has started yet. Don't silently invent a window.
            print(f"SKIP: --anchor {args.anchor} is in the future; no biweekly period has started.")
            sys.exit(0)
        return biweekly_window(anchor)

    if args.period == PERIOD_WEEKLY:
        return weekly_window()

    # default: previous calendar month
    y, m = previous_month()
    return monthly_window(y, m)


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheets_service():
    """Returns a Sheets v4 service using the multi-account token store."""
    return build_service_or_exit("sheets", "primary")


def create_spreadsheet(service, title: str) -> dict:
    body = {
        "properties": {"title": title},
        "sheets": [
            {"properties": {"title": SHEET_TAB_SUMMARY, "index": 0}},
            {"properties": {"title": SHEET_TAB_TRANSACTIONS, "index": 1}},
        ],
    }
    try:
        result = service.spreadsheets().create(body=body).execute()
    except Exception as e:
        print(json.dumps({"error": f"failed to create spreadsheet: {e}"}))
        sys.exit(1)
    return {
        "sheet_id": result.get("spreadsheetId", ""),
        "sheet_url": result.get("spreadsheetUrl", ""),
    }


def ensure_tabs_exist(service, sheet_id: str, required_tabs: list[str]) -> None:
    spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"] for s in spreadsheet.get("sheets", [])}
    missing = [t for t in required_tabs if t not in existing]
    if missing:
        requests = [{"addSheet": {"properties": {"title": t}}} for t in missing]
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": requests},
        ).execute()


def write_to_sheets(service, sheet_id: str, period_label: str,
                    txns: list[dict], payee_labels: dict,
                    inflow: float, outflow: float, outflow_by_category: dict) -> None:
    ensure_tabs_exist(service, sheet_id, [SHEET_TAB_SUMMARY, SHEET_TAB_TRANSACTIONS])

    # ── Summary tab ───────────────────────────────────────────────────────────
    try:
        existing = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB_SUMMARY}!1:1000",
        ).execute()
    except Exception:
        existing = {}

    existing_rows = existing.get("values", [])
    existing_col_a = [r[0] if r else "" for r in existing_rows]
    existing_header = existing_rows[0] if existing_rows else []

    if not existing_header:
        categories = sorted(outflow_by_category.keys())
        existing_header = ["Period", "Inflow", "Outflow"] + categories
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB_SUMMARY}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [existing_header]},
        ).execute()
    else:
        new_cats = [c for c in sorted(outflow_by_category.keys()) if c not in existing_header]
        if new_cats:
            existing_header = existing_header + new_cats
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{SHEET_TAB_SUMMARY}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [existing_header]},
            ).execute()

    if period_label not in existing_col_a:
        row = [period_label, round(inflow, 2), round(outflow, 2)]
        for col in existing_header[3:]:
            row.append(round(outflow_by_category.get(col, 0), 2))
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=SHEET_TAB_SUMMARY,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    # ── Transactions tab ──────────────────────────────────────────────────────
    try:
        existing_t = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB_TRANSACTIONS}!A:A",
        ).execute()
    except Exception:
        existing_t = {}

    existing_t_col_a = [r[0] if r else "" for r in existing_t.get("values", [])]

    if not existing_t_col_a:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB_TRANSACTIONS}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [["Period", "Date", "Payee", "Amount", "Direction", "Category"]]},
        ).execute()

    if period_label not in existing_t_col_a:
        rows = []
        for txn in txns:
            direction = "Inflow" if txn["amount"] < 0 else "Outflow"
            category = categorize_transaction(txn, payee_labels) or "Uncategorized"
            rows.append([
                period_label, txn["date"], txn["name"],
                abs(txn["amount"]), direction, category,
            ])
        if rows:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=SHEET_TAB_TRANSACTIONS,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Spending report — writes to Sheets, outputs JSON for Homer.")
    parser.add_argument("--month", metavar="YYYY-MM",
                        help="Specific month to report on (overrides --period). Default: previous calendar month.")
    parser.add_argument("--period", choices=[PERIOD_MONTHLY, PERIOD_BIWEEKLY, PERIOD_WEEKLY],
                        default=PERIOD_MONTHLY,
                        help="Reporting cadence (default: %(default)s). Ignored if --month is set.")
    parser.add_argument("--anchor", metavar="YYYY-MM-DD",
                        help="Reference date for biweekly windows (e.g. last paycheck date).")
    parser.add_argument("--account-mask", default=FAMILY_SPEND_MASK,
                        help="Last 4 digits of the account to report on (default: %(default)s)")
    parser.add_argument("--institution", default=DEFAULT_INSTITUTION,
                        help="Institution to query (default: %(default)s)")
    parser.add_argument("--sheet-id",
                        help="Target Google Sheet id. Falls back to PLAID_SPENDING_SHEET_ID env. "
                             "If neither set, a new sheet is created and its id is emitted in the JSON output.")
    parser.add_argument("--sheet-title", default=DEFAULT_SHEET_TITLE,
                        help="Title used when a new sheet is created (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print JSON output, skip Sheets write (and skip sheet creation)")
    args = parser.parse_args()

    period_start, period_end, period_label = resolve_period(args)

    env = load_env()

    token_var = f"PLAID_ACCESS_TOKEN_{args.institution.upper()}"
    for var in ("PLAID_CLIENT_ID", "PLAID_SECRET", token_var):
        if not env.get(var):
            print(f"SKIP: {var} not set in secrets/.env")
            sys.exit(0)

    sheet_id = (args.sheet_id or env.get("PLAID_SPENDING_SHEET_ID", "")).strip()
    payee_labels = load_payee_labels()

    try:
        client = get_plaid_client(env)
    except ImportError:
        print(json.dumps({"error": "plaid-python not installed. Run: pip install plaid-python"}))
        sys.exit(1)
    access_token = env[token_var]

    from plaid.model.accounts_get_request import AccountsGetRequest
    accts = client.accounts_get(AccountsGetRequest(access_token=access_token))["accounts"]
    account_id = ""
    for a in accts:
        if account_matches(a, args.account_mask):
            account_id = a.get("account_id", "")
            break
    if not account_id:
        print(f"SKIP: could not find account ending in {args.account_mask} for institution '{args.institution}'.")
        sys.exit(0)

    txns = fetch_transactions(
        client, access_token, period_start, period_end,
        account_ids=[account_id],
    )

    if not txns:
        print(f"SKIP: no transactions found for {period_label}")
        sys.exit(0)

    inflow_total = 0.0
    outflow_total = 0.0
    outflow_by_category: dict[str, float] = defaultdict(float)
    uncategorized: list[dict] = []

    for txn in txns:
        amount = txn["amount"]
        if amount < 0:
            inflow_total += abs(amount)
        else:
            outflow_total += amount
            category = categorize_transaction(txn, payee_labels)
            if category:
                outflow_by_category[category] += amount
            else:
                uncategorized.append(txn)

    # Resolve sheet — create on demand if neither flag nor env supplied one.
    created_sheet = False
    sheets_service = None
    if not args.dry_run:
        sheets_service = get_sheets_service()
        if not sheet_id:
            new = create_spreadsheet(sheets_service, args.sheet_title)
            sheet_id = new["sheet_id"]
            created_sheet = True
        write_to_sheets(
            sheets_service, sheet_id, period_label,
            txns, payee_labels, inflow_total, outflow_total, dict(outflow_by_category),
        )

    sheet_url = SHEET_URL_TEMPLATE.format(sheet_id=sheet_id) if sheet_id else None

    output = {
        "period_label": period_label,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "inflow": round(inflow_total, 2),
        "outflow": round(outflow_total, 2),
        "breakdown": [
            {"category": cat, "amount": round(amt, 2)}
            for cat, amt in sorted(outflow_by_category.items(), key=lambda x: x[1], reverse=True)
        ],
        "uncategorized": [
            {"date": t["date"], "name": t["name"], "amount": round(t["amount"], 2)}
            for t in uncategorized
        ],
        "sheet_id": sheet_id or None,
        "sheet_url": sheet_url,
    }
    # back-compat: monthly runs still emit `month` for any callers that look for it.
    if args.period == PERIOD_MONTHLY and not args.anchor:
        output["month"] = period_label
    if created_sheet:
        output["created_sheet"] = True

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

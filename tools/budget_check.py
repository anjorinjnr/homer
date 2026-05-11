#!/usr/bin/env python3
"""
budget_check.py — Budget vs. actual spending comparison for the household.

Fetches current-month Plaid transactions and compares them against the
household budget Google Sheet. Outputs structured JSON to stdout.

No messaging — Homer handles all communication based on JSON output.

Usage:
    python tools/budget_check.py --status           # full budget comparison
    python tools/budget_check.py --check-alerts     # only threshold crossings since last check

Output (stdout):
    <JSON>              — structured comparison for Homer to present or act on
    {"error": "..."}    — fatal error; exits 1

JSON shape (--status):
    {
      "as_of": "2026-03-22",
      "month": "March 2026",
      "days_elapsed": 22,
      "days_in_month": 31,
      "categories": [
        {
          "category": "Groceries",
          "budget": 800.00,
          "actual": 612.34,
          "remaining": 187.66,
          "pct_used": 76.5,
          "projected_eom": 861.28,
          "status": "warning"
        },
        ...
      ],
      "unbudgeted": [
        {"category": "ENTERTAINMENT", "actual": 45.00}
      ],
      "unmapped_budget_lines": ["Pet Care", "Vacation"],
      "total_budget": 12000.00,
      "total_actual": 8432.10,
      "total_remaining": 3567.90
    }

JSON shape (--check-alerts):
    {
      "as_of": "2026-03-22",
      "alerts": [
        {
          "category": "Groceries",
          "budget": 800.00,
          "actual": 812.34,
          "pct_used": 101.5,
          "status": "over",
          "previous_status": "warning"
        }
      ]
    }
    (empty alerts list = no new threshold crossings)

Status values:
    on_track  — actual < 75% of budget (prorated)
    warning   — projected EOMonth total is between 90-110% of budget
    over      — actual has already exceeded budget

Requires in environment (pre-loaded by nanobot from secrets/.env):
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV
    PLAID_ACCESS_TOKEN_ALLY

Optional in environment:
    PLAID_ACCOUNT_ID_FAMILY_SPEND

Sheet ID source (in priority order):
    1. --sheet-id argument
    2. context/finance.md  (line matching "Budget-Sheet-ID: <id>")
    If neither is set, returns {"error": "budget_sheet_id not configured", ...}

Budget sheet format (read from the "Budget" tab):
    Column A: Category name
    Column B: Monthly budget amount (numeric)
    (header row is auto-detected if first cell is non-numeric)
"""

import argparse
import calendar
import json
import os
import pickle
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOKEN_FILE = REPO_ROOT / "secrets" / "google_token.pickle"
ALERT_STATE_FILE = REPO_ROOT / "data" / "budget_alert_state.json"
FINANCE_CONTEXT_FILE = REPO_ROOT / "context" / "finance.md"

FAMILY_SPEND_MASK = "5733"
DEFAULT_INSTITUTION = "ally"
BUDGET_SHEET_TAB = "Budget"

# Plaid categories that are not real spending
SKIP_CATEGORIES = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "Bank Fees", "Transfer"}

WARNING_THRESHOLD = 0.90   # 90% projected → warning
OVER_THRESHOLD = 1.0       # 100% actual → over


# ── Env ───────────────────────────────────────────────────────────────────────

def load_env() -> dict:
    return dict(os.environ)


# ── Sheet ID resolution ────────────────────────────────────────────────────────

def read_sheet_id_from_context() -> str:
    """
    Read the budget sheet ID from context/finance.md.
    Matches both plain and context_updater.py markdown-list format:
      Budget-Sheet-ID: <id>
      - **Budget-Sheet-ID**: <id>
    Returns the sheet ID string, or "" if not found.
    """
    if not FINANCE_CONTEXT_FILE.exists():
        return ""
    content = FINANCE_CONTEXT_FILE.read_text(encoding="utf-8")
    m = re.search(r"(?:-\s*\*\*)?Budget-Sheet-ID(?:\*\*)?:\s*(.+)", content)
    return m.group(1).strip() if m else ""


# ── Plaid ─────────────────────────────────────────────────────────────────────

def get_plaid_client(env: dict):
    try:
        import plaid
        from plaid.api import plaid_api
        from plaid.configuration import Configuration
        from plaid.api_client import ApiClient
    except ImportError:
        print(json.dumps({"error": "plaid-python not installed. Run: pip install plaid-python"}))
        sys.exit(1)

    plaid_env = env.get("PLAID_ENV", "production").lower()
    host_map = {
        "sandbox": plaid.Environment.Sandbox,
        "production": plaid.Environment.Production,
    }
    config = Configuration(
        host=host_map.get(plaid_env, plaid.Environment.Production),
        api_key={
            "clientId": env["PLAID_CLIENT_ID"],
            "secret": env["PLAID_SECRET"],
        },
    )
    return plaid_api.PlaidApi(ApiClient(config))


def resolve_account_id(client, access_token: str, mask: str) -> str | None:
    """Return account_id for the given account mask, or None."""
    from plaid.model.accounts_get_request import AccountsGetRequest
    try:
        response = client.accounts_get(AccountsGetRequest(access_token=access_token))
    except Exception as e:
        print(json.dumps({"error": f"Plaid API error: {e}"}))
        sys.exit(1)
    for acct in response["accounts"]:
        if str(acct.get("mask", "")) == mask:
            return acct.get("account_id", "")
    return None


def fetch_current_month_transactions(client, access_token: str,
                                     account_ids: list[str] | None = None,
                                     start_date: date | None = None,
                                     end_date: date | None = None) -> list[dict]:
    """Fetch all non-pending transactions for the given date range (default: current month).

    Paginates automatically when total_transactions > 500 (Plaid's per-request cap).
    """
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

    if start_date is None or end_date is None:
        today = date.today()
        start_date = date(today.year, today.month, 1)
        end_date = today

    def _make_request(offset: int):
        options_kwargs: dict = {"count": 500, "offset": offset}
        if account_ids:
            options_kwargs["account_ids"] = account_ids
        return TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(**options_kwargs),
        )

    try:
        response = client.transactions_get(_make_request(0))
    except Exception as e:
        print(json.dumps({"error": f"Plaid API error: {e}"}))
        sys.exit(1)

    all_transactions = list(response["transactions"])
    total = response.total_transactions

    while len(all_transactions) < total:
        try:
            response = client.transactions_get(_make_request(len(all_transactions)))
        except Exception as e:
            print(json.dumps({"error": f"Plaid API error (pagination): {e}"}))
            sys.exit(1)
        all_transactions.extend(response["transactions"])

    txns = []
    for t in all_transactions:
        if t.get("pending"):
            continue
        txns.append({
            "date": str(t["date"]),
            "name": t.get("name", ""),
            "merchant": t.get("merchant_name") or t.get("name", ""),
            "amount": float(t["amount"]),
            "category": (t.get("personal_finance_category") or {}).get("primary", "")
                        or (t.get("category") or [""])[0],
            "account": t.get("account_id", ""),
        })
    return txns


# ── Google Sheets ──────────────────────────────────────────────────────────────

def get_sheets_service():
    try:
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        print(json.dumps({"error": "google-auth not installed. Run: pip install google-auth google-api-python-client"}))
        sys.exit(1)

    if not TOKEN_FILE.exists():
        print(json.dumps({"error": f"Google token not found at {TOKEN_FILE}. Run: python tools/google_auth.py"}))
        sys.exit(1)

    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)

    try:
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(json.dumps({"error": f"Google Sheets auth error: {e}"}))
        sys.exit(1)


def read_budget_from_sheet(sheet_id: str) -> dict[str, float]:
    """
    Read the Budget tab from the given sheet.
    Returns {category: monthly_budget_amount}.
    Skips the header row (detected if column A value is non-numeric).
    """
    service = get_sheets_service()
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{BUDGET_SHEET_TAB}!A:B",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
    except Exception as e:
        print(json.dumps({"error": f"Failed to read budget sheet: {e}"}))
        sys.exit(1)

    rows = result.get("values", [])
    budget: dict[str, float] = {}

    for row in rows:
        if len(row) < 2:
            continue
        cat_raw = str(row[0]).strip()
        amt_raw = str(row[1]).strip().replace(",", "").replace("$", "")
        # Skip header rows (non-numeric amount or blank category)
        if not cat_raw:
            continue
        try:
            amt = float(amt_raw)
        except ValueError:
            continue  # header row or malformed — skip
        if amt > 0:
            budget[cat_raw] = amt

    return budget


# ── Spending aggregation ───────────────────────────────────────────────────────

def aggregate_spending(transactions: list[dict]) -> dict[str, float]:
    """
    Aggregate spending by Plaid category for current month.
    Excludes skip categories. Credits/refunds (negative amounts) net against
    spending in the same category. Categories with zero or negative net spend
    (full refund or pure credit) are dropped from the result.
    Returns {plaid_category: total_spent}.
    """
    spending: dict[str, float] = {}
    for t in transactions:
        cat = t.get("category") or "Other"
        if cat in SKIP_CATEGORIES:
            continue
        spending[cat] = spending.get(cat, 0.0) + t["amount"]
    # Drop categories where net spend is zero or negative (full refund / pure credit)
    return {k: round(v, 2) for k, v in spending.items() if v > 0}


# ── Budget comparison ──────────────────────────────────────────────────────────

def compute_status(budget_amt: float, actual: float, projected_eom: float) -> str:
    """Determine on_track / warning / over status."""
    if actual > budget_amt:
        return "over"
    if projected_eom >= budget_amt * WARNING_THRESHOLD:
        return "warning"
    return "on_track"


def build_comparison(budget: dict[str, float], actual: dict[str, float],
                     days_elapsed: int, days_in_month: int) -> dict:
    """
    Match budget lines to actual spending categories using a two-pass approach:
      Pass 1 — exact matches (case-insensitive) across all budget lines first.
      Pass 2 — word-boundary substring matching for remaining unmatched lines.

    Two passes ensure an exact match always wins over a substring match regardless
    of dict insertion order (e.g. "Fast Food" in actuals is claimed by the
    "Fast Food" budget line, not by "Food" via substring, even if "Food" comes
    first in the budget dict).
    """
    budget_norm = {k.lower(): (k, v) for k, v in budget.items()}
    actual_norm = {k.lower(): (k, v) for k, v in actual.items()}

    matched_budget_keys: set[str] = set()
    matched_actual_keys: set[str] = set()
    budget_actual_map: dict[str, float] = {}   # b_lower -> matched actual amount

    # Pass 1: exact matches (case-insensitive) — claim globally before any substring work
    for b_lower in budget_norm:
        if b_lower in actual_norm and b_lower not in matched_actual_keys:
            _, a_amt = actual_norm[b_lower]
            budget_actual_map[b_lower] = a_amt
            matched_actual_keys.add(b_lower)
            matched_budget_keys.add(b_lower)

    # Pass 2: word-boundary substring matching for unmatched budget lines
    # Aggregates all matching actual categories into a single budget line.
    # Avoids "gas" matching "gasoline" by requiring whole-word boundaries.
    for b_lower in budget_norm:
        if b_lower in matched_budget_keys:
            continue
        a_amt = 0.0
        found_any = False
        for a_lower_k, (_, a_amt_k) in actual_norm.items():
            if a_lower_k in matched_actual_keys:
                continue
            if (re.search(r'\b' + re.escape(b_lower) + r'\b', a_lower_k, re.IGNORECASE) or
                    re.search(r'\b' + re.escape(a_lower_k) + r'\b', b_lower, re.IGNORECASE)):
                a_amt += a_amt_k
                matched_actual_keys.add(a_lower_k)
                found_any = True
        if found_any:
            budget_actual_map[b_lower] = a_amt
            matched_budget_keys.add(b_lower)

    # Build categories list from the resolved amounts
    categories = []
    for b_lower, (b_key, b_amt) in budget_norm.items():
        a_amt = budget_actual_map.get(b_lower, 0.0)
        projected_eom = (a_amt / days_elapsed * days_in_month) if days_elapsed > 0 else a_amt
        projected_eom = round(projected_eom, 2)
        remaining = round(b_amt - a_amt, 2)
        pct_used = round((a_amt / b_amt * 100) if b_amt > 0 else 0.0, 1)
        status = compute_status(b_amt, a_amt, projected_eom)

        categories.append({
            "category": b_key,
            "budget": round(b_amt, 2),
            "actual": round(a_amt, 2),
            "remaining": remaining,
            "pct_used": pct_used,
            "projected_eom": projected_eom,
            "status": status,
        })

    # Sort: over first, then warning, then on_track; within each group by pct_used desc
    status_order = {"over": 0, "warning": 1, "on_track": 2}
    categories.sort(key=lambda x: (status_order.get(x["status"], 9), -x["pct_used"]))

    # Unbudgeted categories (actual spend with no matching budget line)
    unbudgeted = []
    for a_lower, (a_key, a_amt) in actual_norm.items():
        if a_lower not in matched_actual_keys:
            unbudgeted.append({"category": a_key, "actual": round(a_amt, 2)})
    unbudgeted.sort(key=lambda x: x["actual"], reverse=True)

    # Unmapped budget lines (budget lines with zero actual and no actual match)
    unmapped_budget_lines = []
    for b_lower, (b_key, b_amt) in budget_norm.items():
        if b_lower not in matched_budget_keys:
            unmapped_budget_lines.append(b_key)

    total_budget = round(sum(budget.values()), 2)
    total_actual = round(sum(actual.values()), 2)
    total_remaining = round(total_budget - total_actual, 2)

    return {
        "categories": categories,
        "unbudgeted": unbudgeted,
        "unmapped_budget_lines": unmapped_budget_lines,
        "total_budget": total_budget,
        "total_actual": total_actual,
        "total_remaining": total_remaining,
    }


# ── Alert state ────────────────────────────────────────────────────────────────

def load_alert_state() -> dict:
    """Load persisted alert state. Returns {} if file doesn't exist or is corrupt."""
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_alert_state(state: dict) -> None:
    """Persist alert state to disk."""
    ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))


def compute_alerts(categories: list[dict], prev_state: dict) -> tuple[list[dict], dict]:
    """
    Compare current statuses against previous alert state.
    Returns (new_alerts, updated_state).
    An alert fires when status worsens: on_track→warning, on_track→over, warning→over.
    Also fires if a category was never seen before and is already at warning/over.
    """
    status_rank = {"on_track": 0, "warning": 1, "over": 2}
    alerts = []
    new_state: dict = {}

    for cat in categories:
        name = cat["category"]
        current_status = cat["status"]
        previous_status = prev_state.get(name, "on_track")

        if status_rank.get(current_status, 0) > status_rank.get(previous_status, 0):
            alerts.append({
                "category": name,
                "budget": cat["budget"],
                "actual": cat["actual"],
                "pct_used": cat["pct_used"],
                "status": current_status,
                "previous_status": previous_status,
            })

        new_state[name] = current_status

    return alerts, new_state


# ── Month helpers ──────────────────────────────────────────────────────────────

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def current_month_info(year: int | None = None, month: int | None = None) -> tuple[str, int, int]:
    """Returns (month_label, days_elapsed, days_in_month).

    If year/month are provided, days_elapsed is the full month length (for past months)
    unless the month is the current calendar month, in which case it uses today.day.
    """
    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month
    days_in_month = calendar.monthrange(year, month)[1]
    if year == today.year and month == today.month:
        days_elapsed = today.day
    else:
        days_elapsed = days_in_month  # past month: use full month
    month_label = f"{MONTH_NAMES[month]} {year}"
    return month_label, days_elapsed, days_in_month


# ── Alert helpers ─────────────────────────────────────────────────────────────

def run_check_alerts(year: int, month: int, client, access_token: str,
                     account_ids: list[str] | None, budget: dict[str, float],
                     alert_state: dict) -> dict:
    """Run alert detection for a specific year/month.

    Alert state keys are namespaced as "<YYYY-MM>:<category>" to prevent
    March and April alerts from colliding in budget_alert_state.json.

    Returns a dict with keys:
        month_label  — "March 2026"
        alerts       — list of alert dicts (may be empty)
        new_state    — updated alert state slice (namespaced keys)
    """
    days_in_month = calendar.monthrange(year, month)[1]
    today = date.today()
    if year == today.year and month == today.month:
        days_elapsed = today.day
        end_date = today
    else:
        days_elapsed = days_in_month
        end_date = date(year, month, days_in_month)

    start_date = date(year, month, 1)
    month_label = f"{MONTH_NAMES[month]} {year}"
    namespace = f"{year}-{month:02d}"

    transactions = fetch_current_month_transactions(
        client, access_token,
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
    )
    actual_by_category = aggregate_spending(transactions)
    comparison = build_comparison(budget, actual_by_category, days_elapsed, days_in_month)

    # Extract the namespaced slice of the global alert state for this month
    prev_state_slice = {
        k[len(namespace) + 1:]: v
        for k, v in alert_state.items()
        if k.startswith(namespace + ":")
    }

    alerts, new_state_slice = compute_alerts(comparison["categories"], prev_state_slice)

    # Stamp each alert with its month so mixed-month output (days 1-3) is unambiguous
    for alert in alerts:
        alert["month"] = month_label

    # Re-namespace the updated state
    new_state = {f"{namespace}:{k}": v for k, v in new_state_slice.items()}

    return {
        "month_label": month_label,
        "alerts": alerts,
        "new_state": new_state,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        parser = argparse.ArgumentParser(description="Budget vs. actual comparison from Plaid + Google Sheets.")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--status", action="store_true",
                           help="Full budget comparison for current month")
        group.add_argument("--check-alerts", action="store_true",
                           help="Only return categories that crossed a threshold since last check")
        parser.add_argument("--institution", default=DEFAULT_INSTITUTION,
                            help="Institution to query (default: %(default)s)")
        parser.add_argument("--account-mask", default=FAMILY_SPEND_MASK,
                            help="Last 4 digits of the spending account (default: %(default)s)")
        parser.add_argument("--sheet-id", default=None,
                            help="Google Sheets ID for the household budget sheet "
                                 "(overrides context/finance.md lookup)")
        args = parser.parse_args()

        env = load_env()

        # Validate required env vars
        token_var = f"PLAID_ACCESS_TOKEN_{args.institution.upper()}"
        for var in ("PLAID_CLIENT_ID", "PLAID_SECRET", token_var):
            if not env.get(var):
                print(json.dumps({"error": f"{var} not set. Run: python tools/plaid_link.py --institution {args.institution}"}))
                sys.exit(1)

        # Resolve sheet ID: --sheet-id arg takes priority, then context/finance.md
        sheet_id = (args.sheet_id or "").strip() or read_sheet_id_from_context()
        if not sheet_id:
            print(json.dumps({
                "error": "budget_sheet_id not configured",
                "hint": "Ask Homer to search Drive for the budget sheet and store it in context/finance.md",
            }))
            sys.exit(1)

        # Fetch Plaid transactions for current month
        client = get_plaid_client(env)
        access_token = env[token_var]

        account_id = resolve_account_id(client, access_token, args.account_mask)
        if not account_id:
            print(json.dumps({"error": f"Account ending in {args.account_mask} not found for institution '{args.institution}'."}))
            sys.exit(1)

        today = date.today()
        today_str = today.isoformat()

        if args.status:
            transactions = fetch_current_month_transactions(client, access_token, account_ids=[account_id])
            actual_by_category = aggregate_spending(transactions)

            # Read budget from Google Sheets
            budget = read_budget_from_sheet(sheet_id)
            if not budget:
                print(json.dumps({"error": f"Budget sheet '{sheet_id}' returned no budget lines. Check that the '{BUDGET_SHEET_TAB}' tab exists and has Category/Amount columns."}))
                sys.exit(1)

            month_label, days_elapsed, days_in_month = current_month_info()
            comparison = build_comparison(budget, actual_by_category, days_elapsed, days_in_month)

            output = {
                "as_of": today_str,
                "month": month_label,
                "days_elapsed": days_elapsed,
                "days_in_month": days_in_month,
                **comparison,
            }
            print(json.dumps(output, indent=2))

        elif args.check_alerts:
            # Read budget from Google Sheets
            budget = read_budget_from_sheet(sheet_id)
            if not budget:
                print(json.dumps({"error": f"Budget sheet '{sheet_id}' returned no budget lines. Check that the '{BUDGET_SHEET_TAB}' tab exists and has Category/Amount columns."}))
                sys.exit(1)

            alert_state = load_alert_state()

            # Run alerts for the current month
            result = run_check_alerts(
                today.year, today.month,
                client, access_token, [account_id],
                budget, alert_state,
            )
            all_alerts = result["alerts"]
            new_state = result["new_state"]
            month_label = result["month_label"]

            # If we're in the first 3 days of a new month, also check the previous month
            # to catch spending that occurred after the last heartbeat (end-of-month gap).
            if today.day <= 3:
                prev = today.replace(day=1) - timedelta(days=1)
                prev_result = run_check_alerts(
                    prev.year, prev.month,
                    client, access_token, [account_id],
                    budget, alert_state,
                )
                all_alerts.extend(prev_result["alerts"])
                new_state.update(prev_result["new_state"])

            # Persist updated alert state, pruning keys from months no longer being tracked.
            # Only keep namespaces for months that were just checked; this prevents the
            # state file from accumulating stale entries for every past month.
            active_namespaces: set[str] = {f"{today.year}-{today.month:02d}"}
            if today.day <= 3:
                prev_mo = (today.replace(day=1) - timedelta(days=1))
                active_namespaces.add(f"{prev_mo.year}-{prev_mo.month:02d}")
            merged_state = {
                k: v for k, v in alert_state.items()
                if any(k.startswith(ns + ":") for ns in active_namespaces)
            }
            merged_state.update(new_state)
            save_alert_state(merged_state)

            if not all_alerts:
                print("SKIP: No new budget alerts")
                sys.exit(0)

            output = {
                "as_of": today_str,
                "month": month_label,
                "alerts": all_alerts,
            }
            print(json.dumps(output, indent=2))

    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"error": f"Unexpected error: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

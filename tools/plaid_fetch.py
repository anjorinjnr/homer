#!/usr/bin/env python3
"""
plaid_fetch.py — Fetch Plaid balances and transactions for a linked institution.

On-demand tool — Homer calls this when the user asks about finances.
No cron/heartbeat scheduling; outputs JSON to stdout.

Usage:
    python tools/plaid_fetch.py --balances                         # all accounts for institution
    python tools/plaid_fetch.py --transactions                     # last 30 days, scoped to account-mask
    python tools/plaid_fetch.py --transactions --days 7            # last N days
    python tools/plaid_fetch.py --summary                          # balances + spending by category
    python tools/plaid_fetch.py --summary --days 30                # (default)
    python tools/plaid_fetch.py --institution chase --balances     # different institution

Requires in secrets/.env:
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV
    PLAID_ACCESS_TOKEN_ALLY  (or PLAID_ACCESS_TOKEN_CHASE, etc.)

Optional in secrets/.env:
    PLAID_ACCOUNT_ID_FAMILY_SPEND  (if not set, falls back to --account-mask)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, date

from plaid_utils import FAMILY_SPEND_MASK, DEFAULT_INSTITUTION, load_env, get_plaid_client


def token_env_var(institution: str) -> str:
    return f"PLAID_ACCESS_TOKEN_{institution.upper()}"


def fetch_balances(client, access_token: str) -> list[dict]:
    """Fetch balances for all accounts under this access token."""
    from plaid.model.accounts_get_request import AccountsGetRequest

    request = AccountsGetRequest(access_token=access_token)
    response = client.accounts_get(request)

    accounts = []
    for acct in response["accounts"]:
        balances = acct["balances"]
        accounts.append({
            "name": acct.get("name", ""),
            "official_name": acct.get("official_name", ""),
            "type": str(acct.get("type", "")),
            "subtype": str(acct.get("subtype", "")),
            "mask": acct.get("mask", ""),
            "account_id": acct.get("account_id", ""),
            "current": float(balances.get("current") or 0),
            "available": float(balances.get("available") or 0) if balances.get("available") is not None else None,
            "currency": balances.get("iso_currency_code", "USD"),
        })
    return accounts


def fetch_transactions(client, access_token: str, days: int = 30,
                       account_ids: list[str] | None = None,
                       start_date: date | None = None,
                       end_date: date | None = None) -> list[dict]:
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=days))

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
        # Skip pending duplicates
        if t.get("pending"):
            continue
        txns.append({
            "date": str(t["date"]),
            "name": t.get("name", ""),
            "merchant": t.get("merchant_name") or t.get("name", ""),
            "amount": float(t["amount"]),  # positive = debit (spending), negative = credit
            "category": (t.get("personal_finance_category") or {}).get("primary", "")
                        or (t.get("category") or [""])[0],
            "account": t.get("account_id", ""),
        })

    return sorted(txns, key=lambda x: x["date"], reverse=True)


def spending_by_category(transactions: list[dict]) -> list[dict]:
    """Aggregate spending by category. Excludes credits (negative amounts) and transfers."""
    SKIP_CATEGORIES = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "Bank Fees", "Transfer"}

    totals: dict[str, float] = {}
    for t in transactions:
        cat = t["category"] or "Other"
        if cat in SKIP_CATEGORIES:
            continue
        if t["amount"] > 0:  # spending only (positive = debit)
            totals[cat] = round(totals.get(cat, 0) + t["amount"], 2)

    return sorted(
        [{"category": k, "total": v} for k, v in totals.items()],
        key=lambda x: x["total"],
        reverse=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Plaid balances and transactions.")
    parser.add_argument("--balances", action="store_true", help="Fetch current account balances")
    parser.add_argument("--transactions", action="store_true", help="Fetch recent transactions")
    parser.add_argument("--summary", action="store_true", help="Balances + spending by category")
    parser.add_argument("--days", type=int, default=30, help="Lookback days for transactions (default: 30)")
    parser.add_argument("--institution", default=DEFAULT_INSTITUTION,
                        help="Institution to query (default: %(default)s)")
    parser.add_argument("--account-mask", default=FAMILY_SPEND_MASK,
                        help="Last 4 digits to scope transactions to (default: %(default)s)")
    args = parser.parse_args()

    if not any([args.balances, args.transactions, args.summary]):
        parser.print_help()
        sys.exit(1)

    env = load_env()

    token_var = token_env_var(args.institution)
    for var in ("PLAID_CLIENT_ID", "PLAID_SECRET", token_var):
        if not env.get(var):
            print(json.dumps({"error": f"{var} not set in secrets/.env. Run: python tools/plaid_link.py --institution {args.institution}"}))
            sys.exit(1)

    try:
        client = get_plaid_client(env)
    except ImportError:
        print(json.dumps({"error": "plaid-python not installed. Run: pip install plaid-python"}))
        sys.exit(1)
    access_token = env[token_var]

    output: dict = {"as_of": datetime.now().strftime("%Y-%m-%d"), "institution": args.institution}

    if args.balances or args.summary:
        output["accounts"] = fetch_balances(client, access_token)

    if args.transactions or args.summary:
        # Resolve account ID from mask — fail closed if not found
        all_accounts = output.get("accounts") or fetch_balances(client, access_token)
        account_id = next(
            (a["account_id"] for a in all_accounts if str(a.get("mask", "")) == args.account_mask),
            None,
        )
        if not account_id:
            print(json.dumps({"error": f"Account ending in {args.account_mask} not found for institution '{args.institution}'."}))
            sys.exit(1)

        txns = fetch_transactions(client, access_token, days=args.days, account_ids=[account_id])
        output["transactions"] = txns
        output["days"] = args.days
        if args.summary:
            output["spending_by_category"] = spending_by_category(txns)
            total_spend = sum(t["amount"] for t in txns if t["amount"] > 0)
            output["total_spend"] = round(total_spend, 2)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

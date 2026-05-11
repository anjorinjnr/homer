#!/usr/bin/env python3
"""
plaid_balance_check.py — Daily balance check for the family joint account.

Fetches the current balance and outputs structured JSON if below threshold,
or SKIP if balance is healthy. Homer handles all messaging via the heartbeat pattern.

Usage:
    python tools/plaid_balance_check.py                     # default $20k threshold
    python tools/plaid_balance_check.py --threshold 25000   # override threshold

Output (stdout):
    SKIP: <reason>      — balance is at or above threshold
    <JSON>              — balance is below threshold; Homer sends the alert

JSON shape:
    {
      "balance": 18450.00,
      "threshold": 20000.00,
      "account": "FAMILY JOINT Spending Acc"
    }

Requires in secrets/.env:
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV
    PLAID_ACCESS_TOKEN_ALLY  (or PLAID_ACCESS_TOKEN_CHASE, etc.)
"""

import argparse
import json
import sys

from plaid_utils import FAMILY_SPEND_MASK, DEFAULT_INSTITUTION, load_env, get_plaid_client, account_matches

DEFAULT_THRESHOLD = 20_000.0


def fetch_account(client, access_token: str, identifier: str) -> dict | None:
    """Fetch an account by mask (last 4 digits), name, or account number."""
    from plaid.model.accounts_get_request import AccountsGetRequest

    response = client.accounts_get(AccountsGetRequest(access_token=access_token))

    for acct in response["accounts"]:
        if account_matches(acct, identifier):
            current = acct["balances"].get("current")
            if current is not None:
                return {"balance": float(current), "account": acct.get("name", "")}
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Check account balance against a threshold.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--account-mask", default=FAMILY_SPEND_MASK,
                        help="Last 4 digits of the account to check (default: %(default)s)")
    parser.add_argument("--institution", default=DEFAULT_INSTITUTION,
                        help="Institution to query (default: %(default)s)")
    args = parser.parse_args()

    env = load_env()

    token_var = f"PLAID_ACCESS_TOKEN_{args.institution.upper()}"
    for var in ("PLAID_CLIENT_ID", "PLAID_SECRET", token_var):
        if not env.get(var):
            print(f"SKIP: {var} not set.")
            sys.exit(0)

    try:
        client = get_plaid_client(env)
    except ImportError:
        print("SKIP: plaid-python not installed.")
        sys.exit(0)
    result = fetch_account(client, env[token_var], args.account_mask)

    if result is None:
        print(f"SKIP: could not find account ending in {args.account_mask}.")
        sys.exit(0)

    if result["balance"] >= args.threshold:
        print(f"SKIP: balance ${result['balance']:,.2f} is above threshold ${args.threshold:,.0f}.")
        sys.exit(0)

    print(json.dumps({
        "balance": result["balance"],
        "threshold": args.threshold,
        "account": result["account"],
    }))


if __name__ == "__main__":
    main()

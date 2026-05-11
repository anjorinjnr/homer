"""Shared Plaid utilities used by plaid_balance_check, plaid_fetch, plaid_monthly_report, and plaid_link."""

import os

FAMILY_SPEND_MASK = "5733"
DEFAULT_INSTITUTION = "ally"


def load_env() -> dict:
    return dict(os.environ)


def get_plaid_client(env: dict):
    """Create a Plaid API client. Raises ImportError if plaid-python is not installed."""
    import plaid
    from plaid.api import plaid_api
    from plaid.configuration import Configuration
    from plaid.api_client import ApiClient

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


def account_matches(acct: dict, identifier: str) -> bool:
    """Match an account by mask (last 4 digits), name, or account number suffix."""
    mask = str(acct.get("mask", ""))
    if mask == identifier:
        return True
    if acct.get("name", "").lower() == identifier.lower():
        return True
    if mask and len(identifier) > len(mask) and identifier.endswith(mask):
        return True
    return False

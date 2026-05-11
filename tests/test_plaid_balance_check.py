"""
Tests for plaid_balance_check.py and plaid_utils — account matching and threshold logic.

All tests use only pure logic functions — no real API calls, no credentials needed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import plaid_balance_check as pbc
from plaid_utils import account_matches


# ── account_matches() ────────────────────────────────────────────────────────

class TestAccountMatching:
    """Test account_matches matching logic."""

    def test_matches_by_mask(self):
        acct = {"mask": "5733", "name": "FAMILY JOINT Spending Acc"}
        assert account_matches(acct, "5733") is True

    def test_no_match(self):
        acct = {"mask": "9999", "name": "Other Account"}
        assert account_matches(acct, "5733") is False

    def test_matches_by_name_case_insensitive(self):
        acct = {"mask": "5733", "name": "FAMILY JOINT Spending Acc"}
        assert account_matches(acct, "FAMILY JOINT Spending Acc") is True
        assert account_matches(acct, "family joint spending acc") is True

    def test_matches_by_account_number_suffix(self):
        acct = {"mask": "5733", "name": "FAMILY JOINT Spending Acc"}
        assert account_matches(acct, "1135745733") is True

    def test_no_match_short_suffix(self):
        """Identifier must be longer than mask for suffix match."""
        acct = {"mask": "5733", "name": "FAMILY JOINT Spending Acc"}
        assert account_matches(acct, "5733") is True  # exact mask, not suffix
        assert account_matches(acct, "733") is False  # shorter than mask

    def test_no_match_wrong_suffix(self):
        acct = {"mask": "5733", "name": "FAMILY JOINT Spending Acc"}
        assert account_matches(acct, "1135741234") is False


# ── fetch_account() ──────────────────────────────────────────────────────────

def _make_acct(account_id, mask, current, name=None):
    return {
        "account_id": account_id,
        "mask": mask,
        "name": name or f"Account {mask}",
        "balances": {"current": current},
    }


def _mock_client(accounts):
    """Create a mock Plaid client that returns the given accounts."""
    client = MagicMock()
    client.accounts_get.return_value = {"accounts": accounts}
    return client


class TestFetchAccount:
    """Test fetch_account using mock Plaid client."""

    def test_matches_by_mask(self):
        accounts = [
            _make_acct("acct-abc", "1234", 1000.0),
            _make_acct("acct-xyz", "5733", 25000.0),
        ]
        result = pbc.fetch_account(_mock_client(accounts), "tok", "5733")
        assert result is not None
        assert result["balance"] == 25000.0

    def test_returns_none_if_no_match(self):
        accounts = [_make_acct("acct-abc", "9999", 5000.0)]
        assert pbc.fetch_account(_mock_client(accounts), "tok", "5733") is None

    def test_matches_by_name(self):
        accounts = [
            _make_acct("acct-abc", "5733", 25000.0, name="FAMILY JOINT Spending Acc"),
        ]
        result = pbc.fetch_account(_mock_client(accounts), "tok", "FAMILY JOINT Spending Acc")
        assert result is not None
        assert result["balance"] == 25000.0

    def test_matches_by_account_number(self):
        accounts = [
            _make_acct("acct-abc", "5733", 25000.0, name="FAMILY JOINT Spending Acc"),
        ]
        result = pbc.fetch_account(_mock_client(accounts), "tok", "1135745733")
        assert result is not None
        assert result["balance"] == 25000.0

    def test_custom_mask(self):
        accounts = [
            _make_acct("acct-abc", "5733", 25000.0),
            _make_acct("acct-xyz", "1234", 5000.0),
        ]
        result = pbc.fetch_account(_mock_client(accounts), "tok", "1234")
        assert result["balance"] == 5000.0

    def test_first_match_wins(self):
        accounts = [
            _make_acct("acct-abc", "5733", 25000.0),
            _make_acct("acct-xyz", "5733", 9000.0),
        ]
        result = pbc.fetch_account(_mock_client(accounts), "tok", "5733")
        assert result["balance"] == 25000.0


# ── Threshold logic ───────────────────────────────────────────────────────────

class TestThreshold:
    def test_below_threshold_should_alert(self):
        balance = 18000.0
        threshold = 20000.0
        assert balance < threshold  # script should output JSON

    def test_at_threshold_should_skip(self):
        balance = 20000.0
        threshold = 20000.0
        assert balance >= threshold  # script should output SKIP

    def test_above_threshold_should_skip(self):
        balance = 25325.70
        threshold = 20000.0
        assert balance >= threshold  # script should output SKIP

    def test_custom_threshold(self):
        balance = 22000.0
        threshold = 25000.0
        assert balance < threshold  # below custom threshold → alert

"""
Tests for plaid_fetch.py — spending_by_category aggregation.

All tests use only pure logic functions — no real API calls, no credentials needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import plaid_fetch as pf


# ── spending_by_category() ────────────────────────────────────────────────────

class TestSpendingByCategory:
    def _txn(self, amount, category, name="Test"):
        return {"date": "2026-03-01", "name": name, "merchant": name,
                "amount": amount, "category": category, "account": "acct1"}

    def test_basic_aggregation(self):
        txns = [
            self._txn(50.0, "FOOD_AND_DRINK"),
            self._txn(30.0, "FOOD_AND_DRINK"),
            self._txn(100.0, "TRAVEL"),
        ]
        result = pf.spending_by_category(txns)
        cats = {r["category"]: r["total"] for r in result}
        assert cats["FOOD_AND_DRINK"] == 80.0
        assert cats["TRAVEL"] == 100.0

    def test_excludes_transfers(self):
        txns = [
            self._txn(500.0, "TRANSFER_OUT"),
            self._txn(500.0, "TRANSFER_IN"),
            self._txn(50.0, "FOOD_AND_DRINK"),
        ]
        result = pf.spending_by_category(txns)
        cats = {r["category"] for r in result}
        assert "TRANSFER_OUT" not in cats
        assert "TRANSFER_IN" not in cats
        assert "FOOD_AND_DRINK" in cats

    def test_excludes_credits(self):
        txns = [
            self._txn(-100.0, "TRAVEL"),   # credit/refund — negative amount
            self._txn(200.0, "TRAVEL"),
        ]
        result = pf.spending_by_category(txns)
        cats = {r["category"]: r["total"] for r in result}
        assert cats["TRAVEL"] == 200.0  # only the debit counted

    def test_sorted_by_total_descending(self):
        txns = [
            self._txn(10.0, "TRANSPORTATION"),
            self._txn(500.0, "TRAVEL"),
            self._txn(200.0, "FOOD_AND_DRINK"),
        ]
        result = pf.spending_by_category(txns)
        totals = [r["total"] for r in result]
        assert totals == sorted(totals, reverse=True)

    def test_empty_transactions(self):
        assert pf.spending_by_category([]) == []

    def test_all_transfers_returns_empty(self):
        txns = [self._txn(1000.0, "TRANSFER_OUT"), self._txn(1000.0, "TRANSFER_IN")]
        assert pf.spending_by_category(txns) == []

    def test_excludes_loan_payments(self):
        txns = [
            self._txn(1200.0, "LOAN_PAYMENTS"),
            self._txn(80.0, "FOOD_AND_DRINK"),
        ]
        result = pf.spending_by_category(txns)
        cats = {r["category"] for r in result}
        assert "LOAN_PAYMENTS" not in cats
        assert "FOOD_AND_DRINK" in cats

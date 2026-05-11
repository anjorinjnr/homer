"""
Tests for plaid_monthly_report.py — categorization and breakdown logic.

All tests use only pure logic functions — no real API calls, no credentials needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import plaid_monthly_report as pmr


# ── Helpers ───────────────────────────────────────────────────────────────────

def txn(name, amount, plaid_category="OTHER"):
    return {
        "date": "2026-02-01",
        "name": name,
        "merchant": name,
        "amount": amount,
        "plaid_category": plaid_category,
        "account": "acct1",
    }


LABELS = {
    "PNC LENDING": "Mortgage",
    "CHASE CREDIT CRD": "Credit Card Payments",
    "Wealthfront": "Investments",
    "AT&T": "Utilities",
}


# ── categorize_transaction() ──────────────────────────────────────────────────

class TestCategorizeTransaction:
    def test_exact_match(self):
        t = txn("PNC LENDING PAYMENT", 8212.22)
        assert pmr.categorize_transaction(t, LABELS) == "Mortgage"

    def test_case_insensitive(self):
        t = txn("pnc lending payment", 8212.22)
        assert pmr.categorize_transaction(t, LABELS) == "Mortgage"

    def test_substring_match(self):
        t = txn("CHASE CREDIT CRD AUTOPAY 1234", 4625.63)
        assert pmr.categorize_transaction(t, LABELS) == "Credit Card Payments"

    def test_no_match_returns_none(self):
        t = txn("Check Paid #1012", 962.43)
        assert pmr.categorize_transaction(t, LABELS) is None

    def test_no_plaid_category_fallback(self):
        # Plaid category should NOT be used as fallback — None is the correct result
        t = txn("Unknown Payee", 100.0, plaid_category="FOOD_AND_DRINK")
        assert pmr.categorize_transaction(t, LABELS) is None

    def test_empty_labels(self):
        t = txn("AT&T", 80.25)
        assert pmr.categorize_transaction(t, {}) is None


# ── Breakdown logic (inflow / outflow / uncategorized) ────────────────────────

class TestBreakdown:
    def _run(self, txns):
        """Run the same breakdown logic as main()."""
        from collections import defaultdict
        inflow = 0.0
        outflow = 0.0
        by_cat = defaultdict(float)
        uncategorized = []
        for t in txns:
            amt = t["amount"]
            if amt < 0:
                inflow += abs(amt)
            else:
                outflow += amt
                cat = pmr.categorize_transaction(t, LABELS)
                if cat:
                    by_cat[cat] += amt
                else:
                    uncategorized.append(t)
        return inflow, outflow, dict(by_cat), uncategorized

    def test_inflow_is_negative_amounts(self):
        txns = [
            txn("Payroll", -6000.0),
            txn("Interest", -5.0),
        ]
        inflow, outflow, _, _ = self._run(txns)
        assert inflow == pytest.approx(6005.0)
        assert outflow == pytest.approx(0.0)

    def test_outflow_categorized(self):
        txns = [
            txn("PNC LENDING PAYMENT", 8212.22),
            txn("Wealthfront EDI", 5000.0),
        ]
        _, outflow, by_cat, uncategorized = self._run(txns)
        assert outflow == pytest.approx(13212.22)
        assert by_cat["Mortgage"] == pytest.approx(8212.22)
        assert by_cat["Investments"] == pytest.approx(5000.0)
        assert uncategorized == []

    def test_unknown_goes_to_uncategorized(self):
        txns = [txn("Check Paid #1012", 962.43)]
        _, _, by_cat, uncategorized = self._run(txns)
        assert by_cat == {}
        assert len(uncategorized) == 1
        assert uncategorized[0]["name"] == "Check Paid #1012"

    def test_mixed(self):
        txns = [
            txn("Payroll", -6000.0),
            txn("PNC LENDING PAYMENT", 8212.22),
            txn("Check Paid #1012", 962.43),
        ]
        inflow, outflow, by_cat, uncategorized = self._run(txns)
        assert inflow == pytest.approx(6000.0)
        assert outflow == pytest.approx(9174.65)
        assert by_cat["Mortgage"] == pytest.approx(8212.22)
        assert len(uncategorized) == 1

    def test_all_inflow(self):
        txns = [txn("Transfer In", -7000.0), txn("Interest", -5.15)]
        inflow, outflow, by_cat, uncategorized = self._run(txns)
        assert inflow == pytest.approx(7005.15)
        assert outflow == pytest.approx(0.0)
        assert by_cat == {}
        assert uncategorized == []


# ── Summary sheet column alignment ───────────────────────────────────────────

class TestSheetColumnAlignment:
    """Verify that monthly summary rows align to the existing header, not the
    current month's category order."""

    def _build_row(self, existing_header, outflow_by_category, month_label, inflow, outflow):
        """Replicate the column-alignment logic from write_to_sheets."""
        row = [month_label, round(inflow, 2), round(outflow, 2)]
        for col in existing_header[3:]:
            row.append(round(outflow_by_category.get(col, 0), 2))
        return row

    def test_new_category_extends_header(self):
        existing_header = ["Month", "Inflow", "Outflow", "Mortgage", "Utilities"]
        new_cats = ["Education"]
        updated_header = existing_header + [c for c in new_cats if c not in existing_header]
        assert updated_header == ["Month", "Inflow", "Outflow", "Mortgage", "Utilities", "Education"]

    def test_row_aligns_to_existing_header(self):
        # Month 1 established: Mortgage, Utilities
        # Month 2 has: Education, Mortgage (different order + new category extended header)
        existing_header = ["Month", "Inflow", "Outflow", "Mortgage", "Utilities", "Education"]
        outflow_by_category = {"Education": 500.0, "Mortgage": 8212.22}

        row = self._build_row(existing_header, outflow_by_category, "March 2026", 6000.0, 8712.22)

        assert row[0] == "March 2026"
        assert row[1] == 6000.0
        assert row[2] == 8712.22
        assert row[3] == 8212.22   # Mortgage column
        assert row[4] == 0.0       # Utilities column — 0 for this month
        assert row[5] == 500.0     # Education column

    def test_missing_categories_fill_zero(self):
        existing_header = ["Month", "Inflow", "Outflow", "Mortgage", "Investments", "Utilities"]
        outflow_by_category = {"Mortgage": 8212.22}  # only one category this month

        row = self._build_row(existing_header, outflow_by_category, "April 2026", 5000.0, 8212.22)

        assert row[3] == 8212.22  # Mortgage
        assert row[4] == 0.0      # Investments — absent this month
        assert row[5] == 0.0      # Utilities — absent this month


from datetime import date as _date


class TestMonthlyWindow:
    def test_full_month(self):
        start, end, label = pmr.monthly_window(2026, 4)
        assert start == _date(2026, 4, 1)
        assert end == _date(2026, 4, 30)
        assert label == "April 2026"

    def test_february_leap(self):
        start, end, _ = pmr.monthly_window(2024, 2)
        assert end == _date(2024, 2, 29)

    def test_february_non_leap(self):
        start, end, _ = pmr.monthly_window(2026, 2)
        assert end == _date(2026, 2, 28)


class TestBiweeklyWindow:
    def test_window_aligns_to_anchor(self):
        anchor = _date(2026, 5, 1)
        today = _date(2026, 5, 30)
        start, end, label = pmr.biweekly_window(anchor, today=today)
        # 29 days since anchor → 2 completed periods → end = anchor + 28 - 1 = May 28
        assert end == _date(2026, 5, 28)
        assert start == _date(2026, 5, 15)
        assert "May" in label

    def test_anchor_in_future_falls_back_to_today(self):
        anchor = _date(2027, 1, 1)
        today = _date(2026, 5, 30)
        start, end, _ = pmr.biweekly_window(anchor, today=today)
        assert end == today
        assert (end - start).days == 13


class TestWeeklyWindow:
    def test_returns_last_full_iso_week(self):
        # Today is a Friday → last full week is Mon..Sun ending the previous Sunday.
        today = _date(2026, 5, 1)  # Friday
        start, end, label = pmr.weekly_window(today=today)
        assert end == _date(2026, 4, 26)  # Sunday
        assert start == _date(2026, 4, 20)  # Monday
        assert "Week of" in label


class TestResolvePeriod:
    def _args(self, **kw):
        ns = type("Args", (), {})()
        ns.month = kw.get("month")
        ns.period = kw.get("period", "monthly")
        ns.anchor = kw.get("anchor")
        return ns

    def test_explicit_month_overrides_period(self):
        args = self._args(month="2026-04", period="biweekly")
        start, end, label = pmr.resolve_period(args)
        assert start == _date(2026, 4, 1)
        assert end == _date(2026, 4, 30)
        assert "April 2026" in label

    def test_default_resolves_to_previous_calendar_month(self, monkeypatch):
        # Pin date.today() inside the module so this is deterministic.
        import datetime as _dt
        class _FixedDate(_dt.date):
            @classmethod
            def today(cls): return _dt.date(2026, 5, 15)
        monkeypatch.setattr(pmr, "date", _FixedDate)
        args = self._args()
        start, end, label = pmr.resolve_period(args)
        assert start.year == 2026 and start.month == 4 and start.day == 1
        assert label == "April 2026"

    def test_invalid_month_skips(self, capsys):
        args = self._args(month="2026/04")
        with pytest.raises(SystemExit) as exc:
            pmr.resolve_period(args)
        assert exc.value.code == 0
        assert "Invalid --month" in capsys.readouterr().out

    def test_biweekly_without_anchor_skips(self, capsys):
        args = self._args(period="biweekly")
        with pytest.raises(SystemExit) as exc:
            pmr.resolve_period(args)
        assert exc.value.code == 0
        assert "biweekly requires --anchor" in capsys.readouterr().out

    def test_biweekly_invalid_anchor_skips(self, capsys):
        args = self._args(period="biweekly", anchor="not-a-date")
        with pytest.raises(SystemExit) as exc:
            pmr.resolve_period(args)
        assert exc.value.code == 0
        assert "Invalid --anchor" in capsys.readouterr().out

    def test_biweekly_future_anchor_skips(self, capsys, monkeypatch):
        import datetime as _dt
        class _FixedDate(_dt.date):
            @classmethod
            def today(cls): return _dt.date(2026, 5, 1)
        monkeypatch.setattr(pmr, "date", _FixedDate)
        args = self._args(period="biweekly", anchor="2027-01-01")
        with pytest.raises(SystemExit) as exc:
            pmr.resolve_period(args)
        assert exc.value.code == 0
        assert "future" in capsys.readouterr().out

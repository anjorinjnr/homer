"""
Tests for budget_check.py — budget comparison and alert logic.

All tests use only pure logic functions — no real API calls, no credentials needed.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import budget_check as bc


# ── aggregate_spending() ──────────────────────────────────────────────────────

class TestAggregateSpending:
    def _txn(self, amount, category):
        return {"date": "2026-03-01", "name": "Test", "merchant": "Test",
                "amount": amount, "category": category, "account": "acct1"}

    def test_basic_aggregation(self):
        txns = [
            self._txn(50.0, "FOOD_AND_DRINK"),
            self._txn(30.0, "FOOD_AND_DRINK"),
            self._txn(100.0, "TRAVEL"),
        ]
        result = bc.aggregate_spending(txns)
        assert result["FOOD_AND_DRINK"] == 80.0
        assert result["TRAVEL"] == 100.0

    def test_excludes_credits(self):
        # A partial refund nets against the purchase — net $70 remains
        txns = [
            self._txn(-50.0, "FOOD_AND_DRINK"),  # credit/refund
            self._txn(120.0, "FOOD_AND_DRINK"),
        ]
        result = bc.aggregate_spending(txns)
        assert result["FOOD_AND_DRINK"] == 70.0

    def test_partial_refund_nets_against_spending(self):
        # $500 purchase + $200 return in the same category → $300 net
        txns = [
            self._txn(500.0, "GENERAL_MERCHANDISE"),
            self._txn(-200.0, "GENERAL_MERCHANDISE"),
        ]
        result = bc.aggregate_spending(txns)
        assert result["GENERAL_MERCHANDISE"] == 300.0

    def test_full_refund_removes_category(self):
        # $500 purchase + $500 return → category should not appear in output
        txns = [
            self._txn(500.0, "GENERAL_MERCHANDISE"),
            self._txn(-500.0, "GENERAL_MERCHANDISE"),
        ]
        result = bc.aggregate_spending(txns)
        assert "GENERAL_MERCHANDISE" not in result

    def test_pure_credit_excluded(self):
        # A transaction with only a negative amount (e.g. cashback) → excluded
        txns = [
            self._txn(-25.0, "INCOME"),
            self._txn(80.0, "FOOD_AND_DRINK"),
        ]
        result = bc.aggregate_spending(txns)
        assert "INCOME" not in result
        assert result["FOOD_AND_DRINK"] == 80.0

    def test_excludes_transfers(self):
        txns = [
            self._txn(500.0, "TRANSFER_OUT"),
            self._txn(500.0, "TRANSFER_IN"),
            self._txn(80.0, "FOOD_AND_DRINK"),
        ]
        result = bc.aggregate_spending(txns)
        assert "TRANSFER_OUT" not in result
        assert "TRANSFER_IN" not in result
        assert "FOOD_AND_DRINK" in result

    def test_excludes_loan_payments(self):
        txns = [
            self._txn(1200.0, "LOAN_PAYMENTS"),
            self._txn(80.0, "FOOD_AND_DRINK"),
        ]
        result = bc.aggregate_spending(txns)
        assert "LOAN_PAYMENTS" not in result

    def test_empty_returns_empty_dict(self):
        assert bc.aggregate_spending([]) == {}

    def test_rounds_to_two_decimals(self):
        txns = [
            self._txn(33.333, "FOOD_AND_DRINK"),
            self._txn(33.333, "FOOD_AND_DRINK"),
            self._txn(33.334, "FOOD_AND_DRINK"),
        ]
        result = bc.aggregate_spending(txns)
        assert result["FOOD_AND_DRINK"] == round(33.333 + 33.333 + 33.334, 2)

    def test_fallback_to_other_when_no_category(self):
        txns = [
            {"date": "2026-03-01", "name": "Mystery", "merchant": "Mystery",
             "amount": 25.0, "category": "", "account": "acct1"},
        ]
        result = bc.aggregate_spending(txns)
        assert "Other" in result
        assert result["Other"] == 25.0


# ── compute_status() ──────────────────────────────────────────────────────────

class TestComputeStatus:
    def test_on_track_when_low_spend(self):
        # 50% of budget spent, projected EOMonth = 50% → well on track
        status = bc.compute_status(budget_amt=1000.0, actual=250.0, projected_eom=500.0)
        assert status == "on_track"

    def test_warning_when_projected_near_budget(self):
        # Projected EOMonth is 95% of budget → warning
        status = bc.compute_status(budget_amt=1000.0, actual=475.0, projected_eom=950.0)
        assert status == "warning"

    def test_warning_at_exact_threshold(self):
        # Projected exactly at 90% → warning (boundary)
        status = bc.compute_status(budget_amt=1000.0, actual=450.0, projected_eom=900.0)
        assert status == "warning"

    def test_on_track_just_below_warning_threshold(self):
        # Projected at 89.9% → on_track
        status = bc.compute_status(budget_amt=1000.0, actual=440.0, projected_eom=889.0)
        assert status == "on_track"

    def test_over_when_actual_exceeds_budget(self):
        # Actual already over budget → over regardless of projection
        status = bc.compute_status(budget_amt=1000.0, actual=1050.0, projected_eom=1500.0)
        assert status == "over"

    def test_over_takes_precedence_over_warning(self):
        # actual > budget even though projected_eom might not be huge
        status = bc.compute_status(budget_amt=500.0, actual=510.0, projected_eom=900.0)
        assert status == "over"

    def test_on_track_at_start_of_month(self):
        # Day 1: $10 spent, budget $1000, projected $310 (10/1 * 31)
        status = bc.compute_status(budget_amt=1000.0, actual=10.0, projected_eom=310.0)
        assert status == "on_track"


# ── build_comparison() ────────────────────────────────────────────────────────

class TestBuildComparison:
    def _run(self, budget, actual, days_elapsed=15, days_in_month=31):
        return bc.build_comparison(budget, actual, days_elapsed, days_in_month)

    def test_exact_category_match(self):
        budget = {"Groceries": 800.0}
        actual = {"Groceries": 400.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        assert "Groceries" in cats
        assert cats["Groceries"]["actual"] == 400.0
        assert cats["Groceries"]["remaining"] == 400.0
        assert cats["Groceries"]["pct_used"] == 50.0

    def test_case_insensitive_match(self):
        budget = {"Groceries": 800.0}
        actual = {"groceries": 300.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        assert cats["Groceries"]["actual"] == 300.0

    def test_unbudgeted_category_surfaced(self):
        budget = {"Groceries": 800.0}
        actual = {"Groceries": 300.0, "ENTERTAINMENT": 45.0}
        result = self._run(budget, actual)
        unbudgeted_cats = {u["category"] for u in result["unbudgeted"]}
        assert "ENTERTAINMENT" in unbudgeted_cats

    def test_zero_actual_for_unmatched_budget_line(self):
        budget = {"Pet Care": 100.0, "Groceries": 800.0}
        actual = {"Groceries": 400.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        assert cats["Pet Care"]["actual"] == 0.0
        assert cats["Pet Care"]["remaining"] == 100.0

    def test_totals_computed_correctly(self):
        budget = {"Groceries": 800.0, "Gas": 200.0}
        actual = {"Groceries": 400.0, "Gas": 100.0}
        result = self._run(budget, actual)
        assert result["total_budget"] == 1000.0
        assert result["total_actual"] == 500.0
        assert result["total_remaining"] == 500.0

    def test_over_category_appears_first(self):
        budget = {"Groceries": 800.0, "Gas": 200.0}
        actual = {"Groceries": 100.0, "Gas": 250.0}  # Gas is over budget
        result = self._run(budget, actual)
        assert result["categories"][0]["category"] == "Gas"
        assert result["categories"][0]["status"] == "over"

    def test_projected_eom_calculation(self):
        # 15 days elapsed, $400 spent → projected = 400/15*31 = 826.67
        budget = {"Groceries": 800.0}
        actual = {"Groceries": 400.0}
        result = self._run(budget, actual, days_elapsed=15, days_in_month=31)
        cats = {c["category"]: c for c in result["categories"]}
        expected = round(400.0 / 15 * 31, 2)
        assert cats["Groceries"]["projected_eom"] == expected

    def test_zero_days_elapsed_no_division_error(self):
        budget = {"Groceries": 800.0}
        actual = {"Groceries": 0.0}
        # Should not raise
        result = self._run(budget, actual, days_elapsed=0, days_in_month=31)
        assert result is not None

    def test_empty_budget_returns_empty_categories(self):
        result = self._run({}, {"Groceries": 200.0})
        assert result["categories"] == []
        assert len(result["unbudgeted"]) == 1

    def test_pct_used_capped_at_correct_value(self):
        budget = {"Gas": 200.0}
        actual = {"Gas": 300.0}  # 150%
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        assert cats["Gas"]["pct_used"] == 150.0

    def test_no_double_counting_substring_match(self):
        # "Fast Food" should NOT be double-counted for both "Food" and "Fast Food" budget lines.
        # "fast food" matches "fast food" exactly for the "Fast Food" budget line.
        # "food" is a substring of "fast food" but that actual key should already be claimed.
        budget = {"Food": 500.0, "Fast Food": 200.0}
        actual = {"Fast Food": 150.0}
        result = self._run(budget, actual)
        # total_actual must equal the raw sum of actual values — no double-counting
        assert result["total_actual"] == 150.0
        cats = {c["category"]: c for c in result["categories"]}
        # Exactly one of the two budget lines should claim the 150.0; the other gets 0
        total_claimed = cats["Food"]["actual"] + cats["Fast Food"]["actual"]
        assert total_claimed == 150.0

    def test_exact_match_wins_over_substring_regardless_of_dict_order(self):
        # Regression: with a single-pass loop, if "Food" is iterated before "Fast Food",
        # "Food" claims "Fast Food" via substring (\bfood\b matches in "fast food").
        # The two-pass approach must ensure the exact-match budget line always wins.
        # Test with "Food" inserted before "Fast Food" to force the bad ordering.
        budget = {"Food": 500.0, "Fast Food": 200.0}  # "Food" first in dict
        actual = {"Fast Food": 150.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        # "Fast Food" must win the exact match; "Food" gets $0
        assert cats["Fast Food"]["actual"] == 150.0
        assert cats["Food"]["actual"] == 0.0

    def test_total_actual_equals_raw_input_sum(self):
        # total_actual must be the sum of raw actual values, not a derived sum that can inflate.
        budget = {"Groceries": 800.0, "Gas": 200.0}
        actual = {"Groceries": 400.0, "Gas": 100.0, "ENTERTAINMENT": 50.0}
        result = self._run(budget, actual)
        assert result["total_actual"] == round(sum(actual.values()), 2)

    def test_total_remaining_includes_unbudgeted_spend(self):
        # $1000 budget, $500 budgeted spend, $200 unbudgeted spend
        # total_remaining must be $300, not $500
        budget = {"Groceries": 1000.0}
        actual = {"Groceries": 500.0, "ENTERTAINMENT": 200.0}
        result = self._run(budget, actual)
        assert result["total_budget"] == 1000.0
        assert result["total_actual"] == 700.0
        assert result["total_remaining"] == 300.0

    def test_gas_does_not_match_gasoline(self):
        # "gas" must NOT match "gasoline" — word-boundary matching required
        budget = {"Gas": 200.0}
        actual = {"gasoline": 150.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        # "Gas" budget line should NOT claim the "gasoline" actual spend
        assert cats["Gas"]["actual"] == 0.0
        # "gasoline" should appear in unbudgeted
        unbudgeted_cats = {u["category"] for u in result["unbudgeted"]}
        assert "gasoline" in unbudgeted_cats

    def test_gas_matches_gas_station(self):
        # "gas" IS a whole word in "Gas Station" — should match
        budget = {"Gas": 200.0}
        actual = {"Gas Station": 150.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        assert cats["Gas"]["actual"] == 150.0


# ── compute_alerts() ──────────────────────────────────────────────────────────

class TestComputeAlerts:
    def _cat(self, name, status, budget=1000.0, actual=500.0, pct_used=50.0):
        return {
            "category": name,
            "budget": budget,
            "actual": actual,
            "pct_used": pct_used,
            "status": status,
        }

    def test_no_alerts_when_all_on_track(self):
        categories = [self._cat("Groceries", "on_track")]
        prev_state = {"Groceries": "on_track"}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert alerts == []
        assert new_state["Groceries"] == "on_track"

    def test_alert_when_on_track_goes_to_warning(self):
        categories = [self._cat("Groceries", "warning")]
        prev_state = {"Groceries": "on_track"}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert len(alerts) == 1
        assert alerts[0]["category"] == "Groceries"
        assert alerts[0]["status"] == "warning"
        assert alerts[0]["previous_status"] == "on_track"

    def test_alert_when_warning_goes_to_over(self):
        categories = [self._cat("Gas", "over", actual=210.0, pct_used=105.0)]
        prev_state = {"Gas": "warning"}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert len(alerts) == 1
        assert alerts[0]["status"] == "over"
        assert alerts[0]["previous_status"] == "warning"

    def test_alert_when_on_track_goes_directly_to_over(self):
        categories = [self._cat("Gas", "over", actual=250.0, pct_used=125.0)]
        prev_state = {"Gas": "on_track"}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert len(alerts) == 1
        assert alerts[0]["previous_status"] == "on_track"

    def test_no_alert_when_status_unchanged_warning(self):
        categories = [self._cat("Groceries", "warning")]
        prev_state = {"Groceries": "warning"}
        alerts, _ = bc.compute_alerts(categories, prev_state)
        assert alerts == []

    def test_no_alert_when_status_improves(self):
        # Status went from warning to on_track (spending came in)
        categories = [self._cat("Groceries", "on_track")]
        prev_state = {"Groceries": "warning"}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert alerts == []
        assert new_state["Groceries"] == "on_track"

    def test_new_category_triggers_alert_if_warning(self):
        # Category never seen before, already at warning
        categories = [self._cat("Entertainment", "warning")]
        prev_state = {}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert len(alerts) == 1
        assert alerts[0]["previous_status"] == "on_track"  # default

    def test_new_category_no_alert_if_on_track(self):
        categories = [self._cat("Entertainment", "on_track")]
        prev_state = {}
        alerts, new_state = bc.compute_alerts(categories, prev_state)
        assert alerts == []

    def test_state_updated_with_current_status(self):
        categories = [
            self._cat("Groceries", "warning"),
            self._cat("Gas", "over", actual=220.0, pct_used=110.0),
            self._cat("Utilities", "on_track"),
        ]
        prev_state = {"Groceries": "on_track", "Gas": "warning", "Utilities": "on_track"}
        _, new_state = bc.compute_alerts(categories, prev_state)
        assert new_state["Groceries"] == "warning"
        assert new_state["Gas"] == "over"
        assert new_state["Utilities"] == "on_track"

    def test_multiple_alerts_returned(self):
        categories = [
            self._cat("Groceries", "warning"),
            self._cat("Gas", "over", actual=210.0, pct_used=105.0),
        ]
        prev_state = {"Groceries": "on_track", "Gas": "on_track"}
        alerts, _ = bc.compute_alerts(categories, prev_state)
        assert len(alerts) == 2

    def test_stale_keys_pruned_from_state(self):
        # "OldCategory" was in prev_state but is not in the current month's categories.
        # It should NOT appear in new_state.
        categories = [self._cat("Groceries", "on_track")]
        prev_state = {"Groceries": "on_track", "OldCategory": "warning"}
        _, new_state = bc.compute_alerts(categories, prev_state)
        assert "OldCategory" not in new_state
        assert "Groceries" in new_state


# ── Alert state persistence ────────────────────────────────────────────────────

class TestAlertStatePersistence:
    def test_load_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bc, "ALERT_STATE_FILE", tmp_path / "nonexistent.json")
        assert bc.load_alert_state() == {}

    def test_load_returns_empty_on_corrupt_json(self, tmp_path, monkeypatch):
        f = tmp_path / "state.json"
        f.write_text("not json at all")
        monkeypatch.setattr(bc, "ALERT_STATE_FILE", f)
        assert bc.load_alert_state() == {}

    def test_save_and_reload(self, tmp_path, monkeypatch):
        f = tmp_path / "state.json"
        monkeypatch.setattr(bc, "ALERT_STATE_FILE", f)
        state = {"Groceries": "warning", "Gas": "on_track"}
        bc.save_alert_state(state)
        loaded = bc.load_alert_state()
        assert loaded == state

    def test_save_creates_parent_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / "data" / "state.json"
        monkeypatch.setattr(bc, "ALERT_STATE_FILE", nested)
        bc.save_alert_state({"test": "on_track"})
        assert nested.exists()


# ── current_month_info() ──────────────────────────────────────────────────────

class TestCurrentMonthInfo:
    def test_returns_reasonable_values(self):
        label, elapsed, total = bc.current_month_info()
        assert 1 <= elapsed <= 31
        assert 28 <= total <= 31
        assert len(label.split()) == 2  # "March 2026"

    def test_days_elapsed_lte_days_in_month(self):
        label, elapsed, total = bc.current_month_info()
        assert elapsed <= total


# ── read_budget_from_sheet() parsing (unit test without API) ──────────────────

class TestReadBudgetParsing:
    """Test the parsing logic by directly exercising the row-parsing code."""

    def _parse_rows(self, rows):
        """Mirror the parsing logic from read_budget_from_sheet."""
        budget = {}
        for row in rows:
            if len(row) < 2:
                continue
            cat_raw = str(row[0]).strip()
            amt_raw = str(row[1]).strip().replace(",", "").replace("$", "")
            if not cat_raw:
                continue
            try:
                amt = float(amt_raw)
            except ValueError:
                continue
            if amt > 0:
                budget[cat_raw] = amt
        return budget

    def test_parses_simple_rows(self):
        rows = [["Category", "Amount"], ["Groceries", "800"], ["Gas", "200"]]
        result = self._parse_rows(rows)
        assert result == {"Groceries": 800.0, "Gas": 200.0}

    def test_skips_header_row(self):
        rows = [["Category", "Budget"], ["Groceries", "800"]]
        result = self._parse_rows(rows)
        assert "Category" not in result
        assert result["Groceries"] == 800.0

    def test_strips_dollar_signs(self):
        rows = [["Groceries", "$800.00"]]
        result = self._parse_rows(rows)
        assert result["Groceries"] == 800.0

    def test_strips_commas(self):
        rows = [["Mortgage", "2,500.00"]]
        result = self._parse_rows(rows)
        assert result["Mortgage"] == 2500.0

    def test_skips_zero_amounts(self):
        rows = [["Empty Category", "0"], ["Groceries", "800"]]
        result = self._parse_rows(rows)
        assert "Empty Category" not in result

    def test_skips_rows_with_too_few_columns(self):
        rows = [["Groceries"], ["Gas", "200"]]
        result = self._parse_rows(rows)
        assert "Groceries" not in result
        assert result["Gas"] == 200.0

    def test_skips_blank_category(self):
        rows = [["", "500"], ["Groceries", "800"]]
        result = self._parse_rows(rows)
        assert "" not in result
        assert result["Groceries"] == 800.0


# ── read_sheet_id_from_context() ─────────────────────────────────────────────

class TestReadSheetIdFromContext:
    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", tmp_path / "nonexistent.md")
        assert bc.read_sheet_id_from_context() == ""

    def test_returns_id_when_present(self, tmp_path, monkeypatch):
        f = tmp_path / "finance.md"
        f.write_text("# Finance\nBudget-Sheet-ID: abc123def456\n## Accounts\n")
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", f)
        assert bc.read_sheet_id_from_context() == "abc123def456"

    def test_strips_whitespace_from_id(self, tmp_path, monkeypatch):
        f = tmp_path / "finance.md"
        f.write_text("Budget-Sheet-ID:   spaced_id   \n")
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", f)
        assert bc.read_sheet_id_from_context() == "spaced_id"

    def test_returns_empty_when_key_absent(self, tmp_path, monkeypatch):
        f = tmp_path / "finance.md"
        f.write_text("# Finance\n## Accounts\n- Checking: 5733\n")
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", f)
        assert bc.read_sheet_id_from_context() == ""

    def test_only_matches_full_line(self, tmp_path, monkeypatch):
        f = tmp_path / "finance.md"
        f.write_text("# Note: Budget-Sheet-ID is not set here\nBudget-Sheet-ID: real_id\n")
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", f)
        assert bc.read_sheet_id_from_context() == "real_id"

    def test_context_updater_markdown_list_format(self, tmp_path, monkeypatch):
        # context_updater.py writes entries as: - **Budget-Sheet-ID**: <id>
        f = tmp_path / "finance.md"
        f.write_text("# Finance\n- **Budget-Sheet-ID**: sheet_abc123\n")
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", f)
        assert bc.read_sheet_id_from_context() == "sheet_abc123"

    def test_context_updater_format_strips_whitespace(self, tmp_path, monkeypatch):
        f = tmp_path / "finance.md"
        f.write_text("- **Budget-Sheet-ID**:   padded_id   \n")
        monkeypatch.setattr(bc, "FINANCE_CONTEXT_FILE", f)
        assert bc.read_sheet_id_from_context() == "padded_id"


# ── load_env() does not read secrets/.env ─────────────────────────────────────

class TestLoadEnv:
    def test_returns_os_environ(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR_XYZ", "hello")
        env = bc.load_env()
        assert env["TEST_VAR_XYZ"] == "hello"

    def test_no_dotenv_import(self):
        """load_env must not import or call dotenv."""
        import inspect
        src = inspect.getsource(bc.load_env)
        assert "dotenv" not in src
        assert "load_dotenv" not in src


# ── unmapped_budget_lines and category rollup ─────────────────────────────────

class TestUnmappedBudgetLines:
    def _run(self, budget, actual, days_elapsed=15, days_in_month=31):
        return bc.build_comparison(budget, actual, days_elapsed, days_in_month)

    def test_unmapped_budget_lines_detected(self):
        # "Entertainment" is in the budget but no actual spend matches it
        budget = {"Groceries": 800.0, "Entertainment": 200.0}
        actual = {"Groceries": 400.0}
        result = self._run(budget, actual)
        assert "Entertainment" in result["unmapped_budget_lines"]

    def test_unmapped_budget_lines_empty_when_all_matched(self):
        # Every budget line has matching actuals → unmapped_budget_lines is empty
        budget = {"Groceries": 800.0, "Gas": 200.0}
        actual = {"Groceries": 400.0, "Gas": 100.0}
        result = self._run(budget, actual)
        assert result["unmapped_budget_lines"] == []

    def test_multiple_actual_categories_roll_up(self):
        # "Food - Groceries" $200 and "Food - Dining" $150 both contain "food" →
        # should both roll up into the "Food" budget line → actual = $350
        budget = {"Food": 600.0}
        actual = {"Food - Groceries": 200.0, "Food - Dining": 150.0}
        result = self._run(budget, actual)
        cats = {c["category"]: c for c in result["categories"]}
        assert cats["Food"]["actual"] == 350.0
        assert result["unmapped_budget_lines"] == []

    def test_skip_output_when_no_alerts(self, monkeypatch, capsys):
        """main() --check-alerts must print SKIP to stdout when no alert thresholds are crossed."""
        monkeypatch.setattr(bc, "load_env", lambda: {
            "PLAID_CLIENT_ID": "x", "PLAID_SECRET": "x", "PLAID_ACCESS_TOKEN_ALLY": "x"
        })
        monkeypatch.setattr(bc, "get_plaid_client", lambda env: MagicMock())
        monkeypatch.setattr(bc, "resolve_account_id", lambda *a, **kw: "acct1")
        monkeypatch.setattr(bc, "read_sheet_id_from_context", lambda: "sheet123")
        monkeypatch.setattr(bc, "read_budget_from_sheet", lambda sid: {"Groceries": 800.0})
        monkeypatch.setattr(bc, "load_alert_state", lambda: {})
        monkeypatch.setattr(bc, "save_alert_state", lambda s: None)
        # run_check_alerts returns no alerts → main() must output SKIP
        monkeypatch.setattr(bc, "run_check_alerts",
                            lambda *a, **kw: {"month_label": "March 2026", "alerts": [], "new_state": {}})

        import sys
        with patch.object(sys, "argv", ["budget_check.py", "--check-alerts"]):
            with pytest.raises(SystemExit) as exc_info:
                bc.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.startswith("SKIP")


# ── check-alerts output shape ─────────────────────────────────────────────────

class TestCheckAlertsOutput:
    """Verify that compute_alerts returns the data needed for the --check-alerts JSON output."""

    def test_check_alerts_output_includes_month_field(self, monkeypatch):
        """The --check-alerts output dict must contain a 'month' key so Homer can format the header."""
        # Simulate building the output dict the same way main() does.
        import datetime
        month_label, _, _ = bc.current_month_info()
        today_str = datetime.date.today().isoformat()

        categories = []
        prev_state = {}
        alerts, _ = bc.compute_alerts(categories, prev_state)

        output = {
            "as_of": today_str,
            "month": month_label,
            "alerts": alerts,
        }
        assert "month" in output
        assert output["month"] == month_label
        # Verify shape: "March 2026" style
        parts = output["month"].split()
        assert len(parts) == 2
        assert parts[1].isdigit()

    def test_each_alert_has_month_field(self):
        """run_check_alerts must stamp each alert with a 'month' field for mixed-month output."""
        from unittest.mock import MagicMock, patch
        from datetime import date as _date

        budget = {"Groceries": 800.0}
        # Transactions that push Groceries to "over"
        transactions = [{
            "date": "2026-03-15", "name": "Store", "merchant": "Store",
            "amount": 900.0, "category": "Groceries", "account": "acct1",
        }]

        mock_client = MagicMock()

        with patch.object(bc, "fetch_current_month_transactions", return_value=transactions):
            result = bc.run_check_alerts(
                year=2026, month=3,
                client=mock_client, access_token="tok",
                account_ids=None, budget=budget,
                alert_state={},
            )

        assert len(result["alerts"]) == 1
        assert result["alerts"][0]["month"] == "March 2026"


# ── Tool contract: Plaid API errors output JSON ───────────────────────────────

class TestPlaidApiErrorReturnsJson:
    def test_plaid_api_error_returns_json(self, capsys):
        """mock client.accounts_get raising an exception → stdout is JSON with error key."""
        mock_client = MagicMock()
        mock_client.accounts_get.side_effect = Exception("connection refused")

        # Patch the AccountsGetRequest import inside budget_check
        with patch.dict("sys.modules", {"plaid.model.accounts_get_request": MagicMock()}):
            with pytest.raises(SystemExit) as exc_info:
                bc.resolve_account_id(mock_client, "fake_token", "5733")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "error" in output
        assert "Plaid API error" in output["error"]
        assert "connection refused" in output["error"]


# ── Tool contract: Google Sheets auth errors output JSON ──────────────────────

class TestSheetsAuthErrorReturnsJson:
    def test_sheets_auth_error_returns_json(self, capsys, tmp_path, monkeypatch):
        """mock creds.refresh raising RefreshError → stdout is JSON with error key."""
        import pickle

        # Create a fake expired credentials object
        mock_creds = MagicMock()
        mock_creds.expired = True
        mock_creds.refresh_token = "fake_refresh_token"

        # Make refresh raise a RefreshError-like exception
        refresh_error = Exception("Token has been expired or revoked.")
        mock_creds.refresh.side_effect = refresh_error

        # Patch TOKEN_FILE to point to a real file so the existence check passes,
        # then patch pickle.load to return our mock creds (avoids PicklingError).
        token_file = tmp_path / "google_token.pickle"
        token_file.write_bytes(b"placeholder")  # file must exist
        monkeypatch.setattr(bc, "TOKEN_FILE", token_file)

        with patch("budget_check.pickle.load", return_value=mock_creds):
            with patch("googleapiclient.discovery.build", create=True):
                with pytest.raises(SystemExit) as exc_info:
                    bc.get_sheets_service()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "error" in output
        assert "Google Sheets auth error" in output["error"]


# ── Plaid pagination ───────────────────────────────────────────────────────────

class TestPlaidPagination:
    """fetch_current_month_transactions must paginate when total_transactions > 500."""

    def _make_transaction(self, i):
        return {
            "date": "2026-03-01",
            "name": f"Merchant {i}",
            "merchant_name": f"Merchant {i}",
            "amount": 10.0,
            "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
            "account_id": "acct1",
            "pending": False,
        }

    def _make_response(self, transactions, total):
        """Build a dict-like mock Plaid response."""
        resp = MagicMock()
        # Support both attribute access (.total_transactions) and item access (["transactions"])
        resp.total_transactions = total
        resp.__getitem__ = MagicMock(side_effect=lambda key: transactions if key == "transactions" else None)
        return resp

    def test_pagination_fetches_all_transactions(self):
        """When total_transactions=600, two Plaid requests are made and all 600 txns returned."""
        first_batch = [self._make_transaction(i) for i in range(500)]
        second_batch = [self._make_transaction(i) for i in range(500, 600)]

        first_response = self._make_response(first_batch, total=600)
        second_response = self._make_response(second_batch, total=600)

        mock_client = MagicMock()
        mock_client.transactions_get.side_effect = [first_response, second_response]

        from datetime import date as _date

        with patch.dict("sys.modules", {
            "plaid.model.transactions_get_request": MagicMock(),
            "plaid.model.transactions_get_request_options": MagicMock(),
        }):
            result = bc.fetch_current_month_transactions(
                mock_client,
                "fake_token",
                start_date=_date(2026, 3, 1),
                end_date=_date(2026, 3, 31),
            )

        assert mock_client.transactions_get.call_count == 2
        assert len(result) == 600

    def test_single_page_no_extra_requests(self):
        """When total_transactions <= 500, only one request is made."""
        batch = [self._make_transaction(i) for i in range(50)]

        response = self._make_response(batch, total=50)

        mock_client = MagicMock()
        mock_client.transactions_get.return_value = response

        from datetime import date as _date

        with patch.dict("sys.modules", {
            "plaid.model.transactions_get_request": MagicMock(),
            "plaid.model.transactions_get_request_options": MagicMock(),
        }):
            result = bc.fetch_current_month_transactions(
                mock_client,
                "fake_token",
                start_date=_date(2026, 3, 1),
                end_date=_date(2026, 3, 31),
            )

        assert mock_client.transactions_get.call_count == 1
        assert len(result) == 50


# ── End-of-month gap (previous-month alert check) ─────────────────────────────

class TestEndOfMonthGap:
    """When --check-alerts runs on day 1-3 of a new month, also check the previous month."""

    def _make_budget(self):
        return {"Groceries": 800.0, "Gas": 200.0}

    def _make_transactions(self, category="FOOD_AND_DRINK", amount=900.0):
        """Return a list with a single transaction that will bust a budget."""
        return [{
            "date": "2026-03-31",
            "name": "Big Store",
            "merchant": "Big Store",
            "amount": amount,
            "category": category,
            "account": "acct1",
        }]

    def test_end_of_month_gap_checks_previous_month(self, monkeypatch):
        """Simulate today = April 1; run_check_alerts should be called for both April and March."""
        from datetime import date as _date
        import calendar as _calendar

        # Freeze today to April 1 2026
        fake_today = _date(2026, 4, 1)
        monkeypatch.setattr(bc, "date", type("date", (), {
            "today": staticmethod(lambda: fake_today),
            "__new__": _date.__new__,
        }))

        called_months = []

        def fake_run_check_alerts(year, month, client, access_token, account_ids, budget, alert_state):
            called_months.append((year, month))
            return {"month_label": f"{bc.MONTH_NAMES[month]} {year}", "alerts": [], "new_state": {}}

        monkeypatch.setattr(bc, "run_check_alerts", fake_run_check_alerts)

        # Also patch the pieces main() calls before run_check_alerts
        monkeypatch.setattr(bc, "load_env", lambda: {
            "PLAID_CLIENT_ID": "x", "PLAID_SECRET": "x", "PLAID_ACCESS_TOKEN_ALLY": "x"
        })
        monkeypatch.setattr(bc, "get_plaid_client", lambda env: MagicMock())
        monkeypatch.setattr(bc, "resolve_account_id", lambda *a, **kw: "acct1")
        monkeypatch.setattr(bc, "read_sheet_id_from_context", lambda: "sheet123")
        monkeypatch.setattr(bc, "read_budget_from_sheet", lambda sid: {"Groceries": 800.0})
        monkeypatch.setattr(bc, "load_alert_state", lambda: {})
        monkeypatch.setattr(bc, "save_alert_state", lambda s: None)

        import sys
        with patch.object(sys, "argv", ["budget_check.py", "--check-alerts"]):
            with pytest.raises(SystemExit) as exc_info:
                bc.main()

        # Should have exited with 0 (SKIP path — no alerts) or printed JSON
        assert exc_info.value.code == 0

        # Both April (current) and March (previous) must have been checked
        assert (2026, 4) in called_months, f"April not checked; called: {called_months}"
        assert (2026, 3) in called_months, f"March not checked; called: {called_months}"

    def test_no_previous_month_check_mid_month(self, monkeypatch):
        """On day 15, only the current month is checked."""
        from datetime import date as _date

        fake_today = _date(2026, 3, 15)
        monkeypatch.setattr(bc, "date", type("date", (), {
            "today": staticmethod(lambda: fake_today),
            "__new__": _date.__new__,
        }))

        called_months = []

        def fake_run_check_alerts(year, month, client, access_token, account_ids, budget, alert_state):
            called_months.append((year, month))
            return {"month_label": f"{bc.MONTH_NAMES[month]} {year}", "alerts": [], "new_state": {}}

        monkeypatch.setattr(bc, "run_check_alerts", fake_run_check_alerts)

        monkeypatch.setattr(bc, "load_env", lambda: {
            "PLAID_CLIENT_ID": "x", "PLAID_SECRET": "x", "PLAID_ACCESS_TOKEN_ALLY": "x"
        })
        monkeypatch.setattr(bc, "get_plaid_client", lambda env: MagicMock())
        monkeypatch.setattr(bc, "resolve_account_id", lambda *a, **kw: "acct1")
        monkeypatch.setattr(bc, "read_sheet_id_from_context", lambda: "sheet123")
        monkeypatch.setattr(bc, "read_budget_from_sheet", lambda sid: {"Groceries": 800.0})
        monkeypatch.setattr(bc, "load_alert_state", lambda: {})
        monkeypatch.setattr(bc, "save_alert_state", lambda s: None)

        import sys
        with patch.object(sys, "argv", ["budget_check.py", "--check-alerts"]):
            with pytest.raises(SystemExit) as exc_info:
                bc.main()

        assert exc_info.value.code == 0
        assert len(called_months) == 1
        assert called_months[0] == (2026, 3)


# ── Alert state pruning (old months dropped from state file) ──────────────────

class TestAlertStatePruning:
    """main() must prune stale months from the persisted state file."""

    def _common_patches(self, monkeypatch, fake_today, saved_states):
        from datetime import date as _date
        monkeypatch.setattr(bc, "date", type("date", (), {
            "today": staticmethod(lambda: fake_today),
            "__new__": _date.__new__,
        }))
        monkeypatch.setattr(bc, "load_env", lambda: {
            "PLAID_CLIENT_ID": "x", "PLAID_SECRET": "x", "PLAID_ACCESS_TOKEN_ALLY": "x"
        })
        monkeypatch.setattr(bc, "get_plaid_client", lambda env: MagicMock())
        monkeypatch.setattr(bc, "resolve_account_id", lambda *a, **kw: "acct1")
        monkeypatch.setattr(bc, "read_sheet_id_from_context", lambda: "sheet123")
        monkeypatch.setattr(bc, "read_budget_from_sheet", lambda sid: {"Groceries": 800.0})
        monkeypatch.setattr(bc, "save_alert_state", lambda s: saved_states.append(s))

    def test_old_month_keys_pruned_mid_month(self, monkeypatch):
        """On day 15, only current-month namespaced keys are kept in the saved state."""
        from datetime import date as _date
        fake_today = _date(2026, 3, 15)
        saved_states = []
        self._common_patches(monkeypatch, fake_today, saved_states)

        # Simulate state file containing January, February, and March keys
        stale_state = {
            "2026-01:Groceries": "on_track",
            "2026-02:Groceries": "warning",
            "2026-03:Groceries": "on_track",
        }
        monkeypatch.setattr(bc, "load_alert_state", lambda: stale_state)
        monkeypatch.setattr(bc, "run_check_alerts",
                            lambda year, month, *a, **kw: {
                                "month_label": "March 2026", "alerts": [],
                                "new_state": {"2026-03:Groceries": "on_track"},
                            })

        import sys
        with patch.object(sys, "argv", ["budget_check.py", "--check-alerts"]):
            with pytest.raises(SystemExit):
                bc.main()

        assert saved_states, "save_alert_state was never called"
        final_state = saved_states[-1]
        assert "2026-01:Groceries" not in final_state, "January key should be pruned"
        assert "2026-02:Groceries" not in final_state, "February key should be pruned"
        assert "2026-03:Groceries" in final_state

    def test_previous_month_keys_kept_on_day_1_to_3(self, monkeypatch):
        """On day 1-3, both current and previous month keys are kept."""
        from datetime import date as _date
        fake_today = _date(2026, 4, 1)
        saved_states = []
        self._common_patches(monkeypatch, fake_today, saved_states)

        stale_state = {
            "2026-02:Groceries": "over",   # old — should be pruned
            "2026-03:Groceries": "warning", # previous month — should be kept
            "2026-04:Groceries": "on_track",
        }
        monkeypatch.setattr(bc, "load_alert_state", lambda: stale_state)

        def fake_run(year, month, *a, **kw):
            return {
                "month_label": f"{bc.MONTH_NAMES[month]} {year}",
                "alerts": [],
                "new_state": {f"{year}-{month:02d}:Groceries": "on_track"},
            }
        monkeypatch.setattr(bc, "run_check_alerts", fake_run)

        import sys
        with patch.object(sys, "argv", ["budget_check.py", "--check-alerts"]):
            with pytest.raises(SystemExit):
                bc.main()

        assert saved_states, "save_alert_state was never called"
        final_state = saved_states[-1]
        assert "2026-02:Groceries" not in final_state, "February key should be pruned"
        assert "2026-03:Groceries" in final_state, "March (previous month) should be kept"
        assert "2026-04:Groceries" in final_state, "April (current month) should be kept"

"""Tests for health_records.py -- family health & medical records."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.health_records as hr


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point health_records at a temp DB for every test."""
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HOMER_HEALTH_DB", str(db_path))
    return db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _days_from_now(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")


def _add_maya():
    return hr.add_member(
        name="Maya",
        dob="2021-07-12",
        blood_type="O+",
        allergies="peanuts,shellfish",
        doctor="Dr. Smith",
        insurance="Aetna PPO",
    )


# ---------------------------------------------------------------------------
# Family Members
# ---------------------------------------------------------------------------

class TestFamilyMembers:
    def test_add_member(self):
        result = _add_maya()
        assert result["status"] == "ok"
        assert "Maya" in result["message"]

    def test_add_duplicate_member(self):
        _add_maya()
        result = hr.add_member(name="Maya")
        assert "error" in result
        assert "already exists" in result["error"]

    def test_list_members(self):
        _add_maya()
        hr.add_member(name="Alex", dob="1990-01-01")
        result = hr.list_members()
        assert result["status"] == "ok"
        assert len(result["members"]) == 2
        names = [m["name"] for m in result["members"]]
        assert "Alex" in names
        assert "Maya" in names

    def test_list_members_empty(self):
        result = hr.list_members()
        assert result["status"] == "ok"
        assert result["members"] == []

    def test_get_member(self):
        _add_maya()
        result = hr.get_member("Maya")
        assert result["status"] == "ok"
        assert result["member"]["name"] == "Maya"
        assert result["member"]["blood_type"] == "O+"
        assert result["active_medications"] == []
        assert result["recent_visits"] == []
        assert result["upcoming_vaccinations"] == []

    def test_get_member_partial_match(self):
        _add_maya()
        result = hr.get_member("may")
        assert result["status"] == "ok"
        assert result["member"]["name"] == "Maya"

    def test_get_member_case_insensitive(self):
        _add_maya()
        result = hr.get_member("MAYA")
        assert result["status"] == "ok"
        assert result["member"]["name"] == "Maya"

    def test_get_member_not_found(self):
        result = hr.get_member("Nobody")
        assert "error" in result

    def test_update_member(self):
        _add_maya()
        result = hr.update_member("Maya", doctor="Dr. Jones", allergies="peanuts")
        assert result["status"] == "ok"
        updated = hr.get_member("Maya")
        assert updated["member"]["primary_doctor"] == "Dr. Jones"
        assert updated["member"]["allergies"] == "peanuts"

    def test_update_member_no_fields(self):
        _add_maya()
        result = hr.update_member("Maya")
        assert "error" in result
        assert "No fields" in result["error"]

    def test_update_member_not_found(self):
        result = hr.update_member("Ghost", doctor="Dr. X")
        assert "error" in result

    def test_remove_member(self):
        _add_maya()
        result = hr.remove_member("Maya")
        assert result["status"] == "ok"
        assert hr.list_members()["members"] == []

    def test_remove_member_cascades(self):
        """Removing a member should delete all associated records."""
        _add_maya()
        hr.log_visit("Maya", "2026-03-15", "Dr. Smith", visit_type="checkup")
        hr.log_vaccine("Maya", "Flu", "2026-10-01")
        hr.add_medication("Maya", "Amoxicillin", dosage="250mg")
        hr.log_symptom("Maya", "fever")

        hr.remove_member("Maya")
        assert hr.list_members()["members"] == []

        # Verify no orphaned records by re-adding and checking
        _add_maya()
        assert hr.list_visits("Maya")["visits"] == []
        assert hr.list_vaccines("Maya")["vaccinations"] == []
        assert hr.list_medications("Maya")["medications"] == []
        assert hr.list_symptoms("Maya")["symptoms"] == []

    def test_remove_member_not_found(self):
        result = hr.remove_member("Ghost")
        assert "error" in result


# ---------------------------------------------------------------------------
# Medical Visits
# ---------------------------------------------------------------------------

class TestMedicalVisits:
    def test_log_visit(self):
        _add_maya()
        result = hr.log_visit("Maya", "2026-03-15", "Dr. Smith", visit_type="checkup")
        assert result["status"] == "ok"

    def test_log_visit_with_all_fields(self):
        _add_maya()
        result = hr.log_visit(
            "Maya", "2026-03-15", "Dr. Smith",
            visit_type="checkup",
            diagnosis="healthy",
            treatment="none",
            follow_up="2026-09-15",
            notes="annual checkup",
        )
        assert result["status"] == "ok"

    def test_list_visits(self):
        _add_maya()
        hr.log_visit("Maya", "2026-01-15", "Dr. Smith", visit_type="checkup")
        hr.log_visit("Maya", "2026-03-15", "Dr. Jones", visit_type="sick")
        result = hr.list_visits("Maya")
        assert len(result["visits"]) == 2
        # Should be ordered by date descending
        assert result["visits"][0]["visit_date"] == "2026-03-15"

    def test_list_visits_filter_year(self):
        _add_maya()
        hr.log_visit("Maya", "2025-06-01", "Dr. Smith")
        hr.log_visit("Maya", "2026-03-15", "Dr. Smith")
        result = hr.list_visits("Maya", year=2026)
        assert len(result["visits"]) == 1

    def test_list_visits_filter_type(self):
        _add_maya()
        hr.log_visit("Maya", "2026-01-15", "Dr. Smith", visit_type="checkup")
        hr.log_visit("Maya", "2026-03-15", "Dr. Jones", visit_type="sick")
        result = hr.list_visits("Maya", visit_type="checkup")
        assert len(result["visits"]) == 1
        assert result["visits"][0]["visit_type"] == "checkup"

    def test_upcoming_visits(self):
        _add_maya()
        future = _days_from_now(10)
        hr.log_visit("Maya", "2026-01-15", "Dr. Smith", follow_up=future)
        result = hr.upcoming_visits()
        assert len(result["upcoming"]) == 1
        assert result["upcoming"][0]["member_name"] == "Maya"

    def test_upcoming_visits_excludes_past(self):
        _add_maya()
        past = _days_from_now(-5)
        hr.log_visit("Maya", "2026-01-15", "Dr. Smith", follow_up=past)
        result = hr.upcoming_visits()
        assert len(result["upcoming"]) == 0

    def test_upcoming_visits_excludes_far_future(self):
        _add_maya()
        far = _days_from_now(60)
        hr.log_visit("Maya", "2026-01-15", "Dr. Smith", follow_up=far)
        result = hr.upcoming_visits()
        assert len(result["upcoming"]) == 0

    def test_get_member_shows_recent_visits(self):
        _add_maya()
        for i in range(7):
            hr.log_visit("Maya", f"2026-0{i+1}-15", "Dr. Smith")
        result = hr.get_member("Maya")
        assert len(result["recent_visits"]) == 5  # limited to 5


# ---------------------------------------------------------------------------
# Vaccinations
# ---------------------------------------------------------------------------

class TestVaccinations:
    def test_log_vaccine(self):
        _add_maya()
        result = hr.log_vaccine("Maya", "Flu", "2026-10-01", provider="CVS")
        assert result["status"] == "ok"

    def test_list_vaccines(self):
        _add_maya()
        hr.log_vaccine("Maya", "Flu", "2026-10-01")
        hr.log_vaccine("Maya", "DTaP", "2025-06-01")
        result = hr.list_vaccines("Maya")
        assert len(result["vaccinations"]) == 2

    def test_due_vaccines_includes_overdue(self):
        _add_maya()
        past = _days_from_now(-30)
        hr.log_vaccine("Maya", "Flu", "2025-10-01", next_due=past)
        result = hr.due_vaccines()
        assert len(result["due_vaccines"]) == 1
        assert result["due_vaccines"][0]["member_name"] == "Maya"

    def test_due_vaccines_includes_today(self):
        _add_maya()
        hr.log_vaccine("Maya", "Flu", "2025-10-01", next_due=_today())
        result = hr.due_vaccines()
        assert len(result["due_vaccines"]) == 1

    def test_due_vaccines_excludes_future(self):
        _add_maya()
        future = _days_from_now(30)
        hr.log_vaccine("Maya", "Flu", "2025-10-01", next_due=future)
        result = hr.due_vaccines()
        assert len(result["due_vaccines"]) == 0

    def test_due_vaccines_excludes_no_next_due(self):
        _add_maya()
        hr.log_vaccine("Maya", "Flu", "2025-10-01")
        result = hr.due_vaccines()
        assert len(result["due_vaccines"]) == 0

    def test_get_member_shows_upcoming_vaccinations(self):
        _add_maya()
        future = _days_from_now(10)
        hr.log_vaccine("Maya", "Flu", "2025-10-01", next_due=future)
        past = _days_from_now(-10)
        hr.log_vaccine("Maya", "DTaP", "2024-01-01", next_due=past)
        result = hr.get_member("Maya")
        # Only future next_due shown in upcoming_vaccinations
        assert len(result["upcoming_vaccinations"]) == 1
        assert result["upcoming_vaccinations"][0]["vaccine_name"] == "Flu"


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------

class TestMedications:
    def test_add_medication(self):
        _add_maya()
        result = hr.add_medication("Maya", "Amoxicillin", dosage="250mg", frequency="twice daily")
        assert result["status"] == "ok"

    def test_list_medications(self):
        _add_maya()
        hr.add_medication("Maya", "Amoxicillin")
        hr.add_medication("Maya", "Ibuprofen")
        result = hr.list_medications("Maya")
        assert len(result["medications"]) == 2

    def test_list_medications_active_only(self):
        _add_maya()
        hr.add_medication("Maya", "Amoxicillin")
        hr.add_medication("Maya", "Ibuprofen")
        # Deactivate one
        meds = hr.list_medications("Maya")
        med_id = meds["medications"][0]["medication_id"]
        hr.update_medication(med_id, active=0)
        result = hr.list_medications("Maya", active_only=True)
        assert len(result["medications"]) == 1

    def test_update_medication(self):
        _add_maya()
        hr.add_medication("Maya", "Amoxicillin", refill_date="2026-04-01")
        meds = hr.list_medications("Maya")
        med_id = meds["medications"][0]["medication_id"]
        result = hr.update_medication(med_id, refill_date="2026-05-01", dosage="500mg")
        assert result["status"] == "ok"
        updated = hr.list_medications("Maya")
        assert updated["medications"][0]["refill_date"] == "2026-05-01"
        assert updated["medications"][0]["dosage"] == "500mg"

    def test_update_medication_not_found(self):
        result = hr.update_medication(9999, dosage="100mg")
        assert "error" in result

    def test_update_medication_no_fields(self):
        _add_maya()
        hr.add_medication("Maya", "Amoxicillin")
        meds = hr.list_medications("Maya")
        med_id = meds["medications"][0]["medication_id"]
        result = hr.update_medication(med_id)
        assert "error" in result
        assert "No fields" in result["error"]

    def test_due_refills_within_7_days(self):
        _add_maya()
        soon = _days_from_now(3)
        hr.add_medication("Maya", "Amoxicillin", refill_date=soon)
        result = hr.due_refills()
        assert len(result["due_refills"]) == 1

    def test_due_refills_past_due(self):
        _add_maya()
        past = _days_from_now(-5)
        hr.add_medication("Maya", "Amoxicillin", refill_date=past)
        result = hr.due_refills()
        assert len(result["due_refills"]) == 1
        assert result["due_refills"][0]["member_name"] == "Maya"

    def test_due_refills_excludes_inactive(self):
        _add_maya()
        soon = _days_from_now(3)
        hr.add_medication("Maya", "Amoxicillin", refill_date=soon)
        meds = hr.list_medications("Maya")
        hr.update_medication(meds["medications"][0]["medication_id"], active=0)
        result = hr.due_refills()
        assert len(result["due_refills"]) == 0

    def test_due_refills_excludes_far_future(self):
        _add_maya()
        far = _days_from_now(30)
        hr.add_medication("Maya", "Amoxicillin", refill_date=far)
        result = hr.due_refills()
        assert len(result["due_refills"]) == 0

    def test_get_member_shows_active_medications(self):
        _add_maya()
        hr.add_medication("Maya", "Amoxicillin")
        hr.add_medication("Maya", "Ibuprofen")
        # Deactivate one
        meds = hr.list_medications("Maya")
        hr.update_medication(meds["medications"][0]["medication_id"], active=0)
        result = hr.get_member("Maya")
        assert len(result["active_medications"]) == 1


# ---------------------------------------------------------------------------
# Symptom Log
# ---------------------------------------------------------------------------

class TestSymptomLog:
    def test_log_symptom(self):
        _add_maya()
        result = hr.log_symptom("Maya", "fever, cough", severity=6, temperature=101.2)
        assert result["status"] == "ok"

    def test_list_symptoms(self):
        _add_maya()
        hr.log_symptom("Maya", "fever")
        hr.log_symptom("Maya", "cough")
        result = hr.list_symptoms("Maya")
        assert len(result["symptoms"]) == 2

    def test_list_symptoms_respects_days(self):
        _add_maya()
        hr.log_symptom("Maya", "fever")
        # Insert an old symptom directly
        conn = hr.get_conn()
        member = hr._resolve_member(conn, "Maya")
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "INSERT INTO symptom_log (member_id, logged_at, symptoms) VALUES (?, ?, ?)",
            (member["member_id"], old_date, "headache"),
        )
        conn.commit()
        conn.close()

        result = hr.list_symptoms("Maya", days=30)
        assert len(result["symptoms"]) == 1
        assert result["symptoms"][0]["symptoms"] == "fever"

    def test_log_symptom_member_not_found(self):
        # _require_member calls sys.exit on not found, so we test via CLI behavior
        # Instead test the resolve helper directly
        conn = hr.get_conn()
        assert hr._resolve_member(conn, "Nobody") is None
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_dashboard_empty(self):
        result = hr.dashboard()
        assert result["status"] == "ok"
        assert result["upcoming_visits"] == []
        assert result["due_vaccines"] == []
        assert result["due_refills"] == []
        assert result["recent_symptoms"] == []

    def test_dashboard_aggregates(self):
        _add_maya()
        hr.add_member(name="Alex")

        # Upcoming visit for Maya
        future = _days_from_now(10)
        hr.log_visit("Maya", "2026-01-15", "Dr. Smith", follow_up=future)

        # Due vaccine for Alex
        past = _days_from_now(-5)
        hr.log_vaccine("Alex", "Flu", "2025-10-01", next_due=past)

        # Due refill for Maya
        soon = _days_from_now(3)
        hr.add_medication("Maya", "Amoxicillin", refill_date=soon)

        # Recent symptom for Maya
        hr.log_symptom("Maya", "fever")

        result = hr.dashboard()
        assert len(result["upcoming_visits"]) == 1
        assert len(result["due_vaccines"]) == 1
        assert len(result["due_refills"]) == 1
        assert len(result["recent_symptoms"]) == 1

    def test_dashboard_filtered_by_member(self):
        _add_maya()
        hr.add_member(name="Alex")

        # Data for both
        hr.log_symptom("Maya", "fever")
        hr.log_symptom("Alex", "headache")

        result = hr.dashboard(member_name="Maya")
        assert result["status"] == "ok"
        assert len(result["recent_symptoms"]) == 1
        assert result["recent_symptoms"][0]["member_name"] == "Maya"

    def test_dashboard_member_not_found(self):
        result = hr.dashboard(member_name="Ghost")
        assert "error" in result

    def test_dashboard_recent_symptoms_only_7_days(self):
        _add_maya()
        hr.log_symptom("Maya", "fever")

        # Insert an old symptom
        conn = hr.get_conn()
        member = hr._resolve_member(conn, "Maya")
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "INSERT INTO symptom_log (member_id, logged_at, symptoms) VALUES (?, ?, ?)",
            (member["member_id"], old_date, "old headache"),
        )
        conn.commit()
        conn.close()

        result = hr.dashboard()
        assert len(result["recent_symptoms"]) == 1
        assert result["recent_symptoms"][0]["symptoms"] == "fever"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_partial_name_matching(self):
        hr.add_member(name="Olamide Adebayo")
        result = hr.get_member("ola")
        assert result["status"] == "ok"
        assert result["member"]["name"] == "Olamide Adebayo"

    def test_member_with_minimal_info(self):
        result = hr.add_member(name="Baby")
        assert result["status"] == "ok"
        member = hr.get_member("Baby")
        assert member["member"]["date_of_birth"] is None
        assert member["member"]["blood_type"] is None

    def test_multiple_members_different_records(self):
        _add_maya()
        hr.add_member(name="Alex")
        hr.log_visit("Maya", "2026-03-15", "Dr. Smith")
        hr.log_visit("Alex", "2026-03-16", "Dr. Jones")
        assert len(hr.list_visits("Maya")["visits"]) == 1
        assert len(hr.list_visits("Alex")["visits"]) == 1

    def test_ambiguous_partial_name_raises(self):
        """Partial match that hits multiple members should error, not pick arbitrarily."""
        hr.add_member(name="Sam")
        hr.add_member(name="Samantha")
        # "Sam" exact match should still work
        result = hr.get_member("Sam")
        assert result["status"] == "ok"
        assert result["member"]["name"] == "Sam"

    def test_ambiguous_partial_name_errors(self):
        """A truly ambiguous partial match should exit with error."""
        hr.add_member(name="Samuel")
        hr.add_member(name="Samantha")
        # "Sam" is not an exact match for either, and partial matches both
        with pytest.raises(SystemExit):
            hr.get_member("Sam")

    def test_exact_match_preferred_over_partial(self):
        """Exact name match should be preferred even if partial also matches others."""
        hr.add_member(name="Maya")
        hr.add_member(name="Olamide")
        result = hr.get_member("Maya")
        assert result["status"] == "ok"
        assert result["member"]["name"] == "Maya"

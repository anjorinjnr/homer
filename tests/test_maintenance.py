"""Tests for maintenance.py — SQLite-backed home maintenance tracker."""

import json
import sys
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pytest

import tools.maintenance as m


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point maintenance at a temp DB for every test."""
    db_path = tmp_path / "maintenance.db"
    monkeypatch.setenv("HOMER_MAINTENANCE_DB", str(db_path))
    return db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_main(argv: list[str]) -> dict:
    """Run main() with given argv and return parsed JSON output."""
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        m.main(argv)
    finally:
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
    return json.loads(output)


def _today() -> str:
    return date.today().isoformat()


def _days_from_now(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


# ── Maintenance Tasks ──────────────────────────────────────────────────────

class TestMaintenanceTasks:
    def test_add_task(self):
        result = m.add_task("Replace HVAC filter", "HVAC", 90)
        assert result["status"] == "created"
        assert result["task_id"] == 1
        assert result["name"] == "Replace HVAC filter"
        assert result["system"] == "HVAC"
        assert result["frequency_days"] == 90
        assert result["next_due"] == _days_from_now(90)

    def test_add_task_with_notes(self):
        result = m.add_task("Clean gutters", "Exterior", 180, notes="Use ladder")
        assert result["status"] == "created"
        tasks = m.list_tasks()
        assert tasks[0]["notes"] == "Use ladder"

    def test_add_multiple_tasks(self):
        m.add_task("Task A", "HVAC", 30)
        m.add_task("Task B", "Pool", 7)
        tasks = m.list_tasks()
        assert len(tasks) == 2

    def test_complete_task(self):
        res = m.add_task("Filter", "HVAC", 90)
        tid = res["task_id"]
        comp = m.complete_task(tid, done_by="DIY", cost=25.0, notes="Done")
        assert comp["status"] == "completed"
        assert comp["completed_date"] == _today()
        assert comp["next_due"] == _days_from_now(90)

    def test_complete_task_with_date(self):
        res = m.add_task("Filter", "HVAC", 90)
        tid = res["task_id"]
        comp = m.complete_task(tid, completed_date="2026-01-15")
        assert comp["completed_date"] == "2026-01-15"
        assert comp["next_due"] == "2026-04-15"

    def test_complete_task_updates_last_completed(self):
        res = m.add_task("Filter", "HVAC", 90)
        tid = res["task_id"]
        m.complete_task(tid)
        tasks = m.list_tasks()
        assert tasks[0]["last_completed"] == _today()

    def test_complete_task_not_found(self):
        with pytest.raises(SystemExit):
            m.complete_task(999)

    def test_task_history(self):
        res = m.add_task("Filter", "HVAC", 90)
        tid = res["task_id"]
        m.complete_task(tid, done_by="DIY", cost=25.0)
        m.complete_task(tid, done_by="Pro", cost=50.0)
        history = m.task_history(tid)
        assert len(history) == 2
        # Most recent first
        assert history[0]["done_by"] == "Pro"

    def test_task_history_not_found(self):
        with pytest.raises(SystemExit):
            m.task_history(999)

    def test_task_history_empty(self):
        res = m.add_task("Filter", "HVAC", 90)
        history = m.task_history(res["task_id"])
        assert history == []

    def test_list_tasks_empty(self):
        assert m.list_tasks() == []

    def test_list_tasks_filter_system(self):
        m.add_task("Filter", "HVAC", 90)
        m.add_task("Chemicals", "Pool", 14)
        hvac = m.list_tasks(system="HVAC")
        assert len(hvac) == 1
        assert hvac[0]["system"] == "HVAC"

    def test_list_tasks_filter_system_case_insensitive(self):
        m.add_task("Filter", "HVAC", 90)
        assert len(m.list_tasks(system="hvac")) == 1

    def test_list_tasks_due(self):
        m.add_task("Soon", "HVAC", 3)    # due in 3 days
        m.add_task("Later", "Pool", 30)  # due in 30 days
        due = m.list_tasks(due=True)
        assert len(due) == 1
        assert due[0]["name"] == "Soon"

    def test_list_tasks_due_includes_overdue(self):
        res = m.add_task("Overdue", "HVAC", 1)
        # Force next_due to yesterday
        with m.get_conn() as conn:
            conn.execute(
                "UPDATE maintenance_tasks SET next_due = ? WHERE task_id = ?",
                ((date.today() - timedelta(days=1)).isoformat(), res["task_id"]),
            )
            conn.commit()
        due = m.list_tasks(due=True)
        assert len(due) == 1

    def test_list_tasks_ordered_by_next_due(self):
        m.add_task("Later", "Pool", 30)
        m.add_task("Sooner", "HVAC", 5)
        tasks = m.list_tasks()
        assert tasks[0]["name"] == "Sooner"
        assert tasks[1]["name"] == "Later"

    # CLI integration
    def test_cli_add_task(self):
        result = _capture_main([
            "--add-task", "--name", "Filter", "--system", "HVAC", "--frequency", "90"
        ])
        assert result["status"] == "created"

    def test_cli_list_tasks(self):
        m.add_task("Filter", "HVAC", 90)
        result = _capture_main(["--list-tasks"])
        assert len(result) == 1

    def test_cli_complete_task(self):
        res = m.add_task("Filter", "HVAC", 90)
        result = _capture_main([
            "--complete-task", "--task-id", str(res["task_id"]),
            "--done-by", "DIY", "--cost", "25.0"
        ])
        assert result["status"] == "completed"

    def test_cli_add_task_missing_name(self):
        with pytest.raises(SystemExit):
            _capture_main(["--add-task", "--system", "HVAC", "--frequency", "90"])

    def test_cli_add_task_missing_system(self):
        with pytest.raises(SystemExit):
            _capture_main(["--add-task", "--name", "Filter", "--frequency", "90"])

    def test_cli_add_task_missing_frequency(self):
        with pytest.raises(SystemExit):
            _capture_main(["--add-task", "--name", "Filter", "--system", "HVAC"])

    def test_complete_task_invalid_date(self):
        res = m.add_task("Filter", "HVAC", 90)
        with pytest.raises(SystemExit):
            m.complete_task(res["task_id"], completed_date="Jan 15")


# ── Service Providers ──────────────────────────────────────────────────────

class TestServiceProviders:
    def test_add_provider(self):
        result = m.add_provider("Malcolm", "HVAC", phone="770-555-1234", rating=5)
        assert result["status"] == "created"
        assert result["provider_id"] == 1

    def test_add_provider_minimal(self):
        result = m.add_provider("Joe", "Plumbing")
        assert result["status"] == "created"

    def test_list_providers_empty(self):
        assert m.list_providers() == []

    def test_list_providers(self):
        m.add_provider("Malcolm", "HVAC")
        m.add_provider("Joe", "Plumbing")
        providers = m.list_providers()
        assert len(providers) == 2

    def test_list_providers_filter_specialty(self):
        m.add_provider("Malcolm", "HVAC")
        m.add_provider("Joe", "Plumbing")
        hvac = m.list_providers(specialty="HVAC")
        assert len(hvac) == 1
        assert hvac[0]["name"] == "Malcolm"

    def test_list_providers_filter_case_insensitive(self):
        m.add_provider("Malcolm", "HVAC")
        assert len(m.list_providers(specialty="hvac")) == 1

    def test_update_provider(self):
        res = m.add_provider("Malcolm", "HVAC", rating=4)
        result = m.update_provider(res["provider_id"], rating=5, phone="770-555-9999")
        assert result["status"] == "updated"
        providers = m.list_providers()
        assert providers[0]["rating"] == 5
        assert providers[0]["phone"] == "770-555-9999"

    def test_update_provider_not_found(self):
        with pytest.raises(SystemExit):
            m.update_provider(999, name="Nobody")

    def test_update_provider_no_fields(self):
        res = m.add_provider("Malcolm", "HVAC")
        with pytest.raises(SystemExit):
            m.update_provider(res["provider_id"])

    def test_update_provider_invalid_rating(self):
        res = m.add_provider("Malcolm", "HVAC")
        with pytest.raises(SystemExit):
            m.update_provider(res["provider_id"], rating=6)

    def test_remove_provider(self):
        res = m.add_provider("Malcolm", "HVAC")
        result = m.remove_provider(res["provider_id"])
        assert result["status"] == "removed"
        assert m.list_providers() == []

    def test_remove_provider_not_found(self):
        with pytest.raises(SystemExit):
            m.remove_provider(999)

    def test_add_provider_invalid_rating(self):
        with pytest.raises(SystemExit):
            m.add_provider("Bad", "HVAC", rating=0)

    def test_add_provider_rating_high(self):
        with pytest.raises(SystemExit):
            m.add_provider("Bad", "HVAC", rating=6)

    # CLI
    def test_cli_add_provider(self):
        result = _capture_main([
            "--add-provider", "--name", "Malcolm", "--specialty", "HVAC",
            "--phone", "770-555-1234", "--rating", "5"
        ])
        assert result["status"] == "created"

    def test_cli_list_providers(self):
        m.add_provider("Malcolm", "HVAC")
        result = _capture_main(["--list-providers"])
        assert len(result) == 1

    def test_cli_update_provider(self):
        res = m.add_provider("Malcolm", "HVAC")
        result = _capture_main([
            "--update-provider", "--provider-id", str(res["provider_id"]),
            "--rating", "3"
        ])
        assert result["status"] == "updated"

    def test_cli_remove_provider(self):
        res = m.add_provider("Malcolm", "HVAC")
        result = _capture_main([
            "--remove-provider", "--provider-id", str(res["provider_id"])
        ])
        assert result["status"] == "removed"


# ── Appliances ─────────────────────────────────────────────────────────────

class TestAppliances:
    def test_add_appliance(self):
        result = m.add_appliance("Refrigerator", brand="Samsung", model="RF28",
                                  location="Kitchen")
        assert result["status"] == "created"
        assert result["appliance_id"] == 1

    def test_add_appliance_minimal(self):
        result = m.add_appliance("Dishwasher")
        assert result["status"] == "created"

    def test_add_appliance_full(self):
        result = m.add_appliance(
            "Water Heater", brand="Rheem", model="XE50",
            serial_number="SN123", install_date="2024-01-01",
            warranty_until="2034-01-01", location="Garage",
            notes="50 gallon tank"
        )
        assert result["status"] == "created"
        appliances = m.list_appliances()
        assert appliances[0]["serial_number"] == "SN123"
        assert appliances[0]["notes"] == "50 gallon tank"

    def test_list_appliances_empty(self):
        assert m.list_appliances() == []

    def test_list_appliances(self):
        m.add_appliance("Fridge", location="Kitchen")
        m.add_appliance("Washer", location="Laundry")
        assert len(m.list_appliances()) == 2

    def test_list_appliances_filter_location(self):
        m.add_appliance("Fridge", location="Kitchen")
        m.add_appliance("Washer", location="Laundry")
        kitchen = m.list_appliances(location="Kitchen")
        assert len(kitchen) == 1
        assert kitchen[0]["name"] == "Fridge"

    def test_list_appliances_filter_case_insensitive(self):
        m.add_appliance("Fridge", location="Kitchen")
        assert len(m.list_appliances(location="kitchen")) == 1

    def test_update_appliance(self):
        res = m.add_appliance("Fridge", brand="LG")
        result = m.update_appliance(res["appliance_id"], brand="Samsung",
                                     warranty_until="2030-01-01")
        assert result["status"] == "updated"
        appliances = m.list_appliances()
        assert appliances[0]["brand"] == "Samsung"
        assert appliances[0]["warranty_until"] == "2030-01-01"

    def test_update_appliance_not_found(self):
        with pytest.raises(SystemExit):
            m.update_appliance(999, brand="X")

    def test_update_appliance_no_fields(self):
        res = m.add_appliance("Fridge")
        with pytest.raises(SystemExit):
            m.update_appliance(res["appliance_id"])

    def test_remove_appliance(self):
        res = m.add_appliance("Fridge")
        result = m.remove_appliance(res["appliance_id"])
        assert result["status"] == "removed"
        assert m.list_appliances() == []

    def test_remove_appliance_not_found(self):
        with pytest.raises(SystemExit):
            m.remove_appliance(999)

    # CLI
    def test_cli_add_appliance(self):
        result = _capture_main([
            "--add-appliance", "--name", "Fridge", "--brand", "Samsung",
            "--location", "Kitchen"
        ])
        assert result["status"] == "created"

    def test_cli_list_appliances(self):
        m.add_appliance("Fridge")
        result = _capture_main(["--list-appliances"])
        assert len(result) == 1

    def test_cli_update_appliance(self):
        res = m.add_appliance("Fridge")
        result = _capture_main([
            "--update-appliance", "--appliance-id", str(res["appliance_id"]),
            "--brand", "LG"
        ])
        assert result["status"] == "updated"

    def test_cli_remove_appliance(self):
        res = m.add_appliance("Fridge")
        result = _capture_main([
            "--remove-appliance", "--appliance-id", str(res["appliance_id"])
        ])
        assert result["status"] == "removed"


# ── Home Projects ──────────────────────────────────────────────────────────

class TestProjects:
    def test_add_project(self):
        result = m.add_project("Fence replacement", budget=5000)
        assert result["status"] == "created"
        assert result["project_id"] == 1

    def test_add_project_minimal(self):
        result = m.add_project("Paint bedroom")
        assert result["status"] == "created"

    def test_add_project_with_started_date(self):
        result = m.add_project("Deck", started_date="2026-03-01")
        projects = m.list_projects()
        assert projects[0]["started_date"] == "2026-03-01"

    def test_add_project_default_started_date(self):
        m.add_project("Deck")
        projects = m.list_projects()
        assert projects[0]["started_date"] == _today()

    def test_list_projects_empty(self):
        assert m.list_projects() == []

    def test_list_projects(self):
        m.add_project("Fence")
        m.add_project("Deck")
        assert len(m.list_projects()) == 2

    def test_list_projects_filter_status(self):
        m.add_project("Fence")
        m.add_project("Deck")
        p = m.list_projects()
        m.update_project(p[0]["project_id"], status="completed")
        active = m.list_projects(status="active")
        assert len(active) == 1

    def test_list_projects_filter_case_insensitive(self):
        m.add_project("Fence")
        assert len(m.list_projects(status="Active")) == 1

    def test_update_project(self):
        res = m.add_project("Fence", budget=5000)
        result = m.update_project(res["project_id"], status="completed",
                                   actual_cost=4800,
                                   completed_date="2026-04-01")
        assert result["status"] == "updated"
        projects = m.list_projects()
        assert projects[0]["status"] == "completed"
        assert projects[0]["actual_cost"] == 4800

    def test_update_project_not_found(self):
        with pytest.raises(SystemExit):
            m.update_project(999, status="completed")

    def test_update_project_invalid_status(self):
        res = m.add_project("Fence")
        with pytest.raises(SystemExit):
            m.update_project(res["project_id"], status="cancelled")

    def test_update_project_no_fields(self):
        res = m.add_project("Fence")
        with pytest.raises(SystemExit):
            m.update_project(res["project_id"])

    def test_add_project_item(self):
        res = m.add_project("Fence")
        item = m.add_project_item(res["project_id"], "Get quotes from 3 vendors")
        assert item["status"] == "created"
        assert item["item_id"] == 1

    def test_add_project_item_not_found(self):
        with pytest.raises(SystemExit):
            m.add_project_item(999, "Something")

    def test_check_project_item(self):
        res = m.add_project("Fence")
        m.add_project_item(res["project_id"], "Get quotes from 3 vendors")
        result = m.check_project_item(res["project_id"], "quotes")
        assert result["status"] == "checked"
        assert "quotes" in result["description"].lower()

    def test_check_project_item_case_insensitive(self):
        res = m.add_project("Fence")
        m.add_project_item(res["project_id"], "Get Quotes from vendors")
        result = m.check_project_item(res["project_id"], "QUOTES")
        assert result["status"] == "checked"

    def test_check_project_item_no_match(self):
        res = m.add_project("Fence")
        m.add_project_item(res["project_id"], "Get quotes")
        with pytest.raises(SystemExit):
            m.check_project_item(res["project_id"], "nonexistent")

    def test_check_project_item_already_done(self):
        res = m.add_project("Fence")
        m.add_project_item(res["project_id"], "Get quotes")
        m.check_project_item(res["project_id"], "quotes")
        # Second check should fail — item is already done
        with pytest.raises(SystemExit):
            m.check_project_item(res["project_id"], "quotes")

    def test_check_project_item_ambiguous_match(self):
        res = m.add_project("Fence")
        m.add_project_item(res["project_id"], "Get quotes from Alice")
        m.add_project_item(res["project_id"], "Get quotes from Bob")
        with pytest.raises(SystemExit):
            m.check_project_item(res["project_id"], "quotes")

    def test_check_project_item_project_not_found(self):
        with pytest.raises(SystemExit):
            m.check_project_item(999, "anything")

    def test_project_status(self):
        res = m.add_project("Fence", budget=5000, description="Replace rotting sections")
        pid = res["project_id"]
        m.add_project_item(pid, "Get quotes")
        m.add_project_item(pid, "Buy materials")
        m.check_project_item(pid, "quotes")
        status = m.project_status(pid)
        assert status["name"] == "Fence"
        assert status["budget"] == 5000
        assert len(status["items"]) == 2
        done_items = [i for i in status["items"] if i["status"] == "done"]
        open_items = [i for i in status["items"] if i["status"] == "open"]
        assert len(done_items) == 1
        assert len(open_items) == 1

    def test_project_status_not_found(self):
        with pytest.raises(SystemExit):
            m.project_status(999)

    # CLI
    def test_cli_add_project(self):
        result = _capture_main([
            "--add-project", "--name", "Fence", "--budget", "5000"
        ])
        assert result["status"] == "created"

    def test_cli_list_projects(self):
        m.add_project("Fence")
        result = _capture_main(["--list-projects"])
        assert len(result) == 1

    def test_cli_update_project(self):
        res = m.add_project("Fence")
        result = _capture_main([
            "--update-project", "--project-id", str(res["project_id"]),
            "--status", "completed", "--actual-cost", "4800"
        ])
        assert result["status"] == "updated"

    def test_cli_add_project_item(self):
        res = m.add_project("Fence")
        result = _capture_main([
            "--add-project-item", "--project-id", str(res["project_id"]),
            "--description", "Get quotes"
        ])
        assert result["status"] == "created"

    def test_cli_check_project_item(self):
        res = m.add_project("Fence")
        m.add_project_item(res["project_id"], "Get quotes")
        result = _capture_main([
            "--check-project-item", "--project-id", str(res["project_id"]),
            "--item", "quotes"
        ])
        assert result["status"] == "checked"

    def test_cli_project_status(self):
        res = m.add_project("Fence")
        result = _capture_main([
            "--project-status", "--project-id", str(res["project_id"])
        ])
        assert result["name"] == "Fence"


# ── Dashboard ──────────────────────────────────────────────────────────────

class TestDashboard:
    def test_dashboard_empty(self):
        result = m.dashboard()
        assert result["overdue_tasks"] == []
        assert result["upcoming_tasks"] == []
        assert result["active_projects"] == []
        assert result["warranty_expirations"] == []

    def test_dashboard_overdue_task(self):
        res = m.add_task("Filter", "HVAC", 90)
        # Force overdue
        with m.get_conn() as conn:
            conn.execute(
                "UPDATE maintenance_tasks SET next_due = ? WHERE task_id = ?",
                ((date.today() - timedelta(days=5)).isoformat(), res["task_id"]),
            )
            conn.commit()
        result = m.dashboard()
        assert len(result["overdue_tasks"]) == 1
        assert result["overdue_tasks"][0]["name"] == "Filter"

    def test_dashboard_upcoming_task(self):
        m.add_task("Filter", "HVAC", 3)  # due in 3 days
        result = m.dashboard()
        assert len(result["upcoming_tasks"]) == 1

    def test_dashboard_task_not_upcoming(self):
        m.add_task("Filter", "HVAC", 30)  # due in 30 days
        result = m.dashboard()
        assert len(result["upcoming_tasks"]) == 0
        assert len(result["overdue_tasks"]) == 0

    def test_dashboard_active_projects(self):
        m.add_project("Fence")
        m.add_project("Deck")
        res = m.add_project("Done Project")
        m.update_project(res["project_id"], status="completed")
        result = m.dashboard()
        assert len(result["active_projects"]) == 2

    def test_dashboard_warranty_expiring(self):
        # Warranty expiring in 30 days (within 90d window)
        m.add_appliance("Fridge", warranty_until=_days_from_now(30))
        result = m.dashboard()
        assert len(result["warranty_expirations"]) == 1

    def test_dashboard_warranty_not_expiring(self):
        # Warranty expiring in 180 days (outside 90d window)
        m.add_appliance("Fridge", warranty_until=_days_from_now(180))
        result = m.dashboard()
        assert len(result["warranty_expirations"]) == 0

    def test_dashboard_warranty_already_expired(self):
        # Already expired warranty should not show
        m.add_appliance("Fridge", warranty_until=(date.today() - timedelta(days=1)).isoformat())
        result = m.dashboard()
        assert len(result["warranty_expirations"]) == 0

    def test_dashboard_no_warranty(self):
        m.add_appliance("Fridge")  # no warranty_until
        result = m.dashboard()
        assert len(result["warranty_expirations"]) == 0

    # CLI
    def test_cli_dashboard(self):
        m.add_task("Filter", "HVAC", 3)
        m.add_project("Fence")
        result = _capture_main(["--dashboard"])
        assert "overdue_tasks" in result
        assert "upcoming_tasks" in result
        assert "active_projects" in result
        assert "warranty_expirations" in result

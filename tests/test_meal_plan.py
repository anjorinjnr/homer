"""Tests for meal_plan.py — SQLite-backed meal planning, grocery lists, and pantry."""

from datetime import datetime, timedelta, timezone

import pytest

import tools.meal_plan as mp


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point meal_plan at a temp DB for every test."""
    db_path = tmp_path / "meals.db"
    monkeypatch.setenv("HOMER_MEALS_DB", str(db_path))
    return db_path


# ── Helpers ─────────────────────────────────────────────────────────────────

def _add_sample_meal(name="Chicken Stir Fry", meal_type="dinner",
                     tags=None, rating=None):
    result = mp.add_meal(name=name, meal_type=meal_type, tags=tags)
    meal_id = result["meal_id"]
    if rating:
        mp.rate_meal(meal_id, rating)
    return meal_id


# ── Meal CRUD ───────────────────────────────────────────────────────────────

class TestMeals:
    def test_add_meal_basic(self):
        result = mp.add_meal(name="Tacos", meal_type="dinner")
        assert result["status"] == "added"
        assert result["meal_id"] is not None
        assert result["name"] == "Tacos"

    def test_add_meal_with_all_fields(self):
        result = mp.add_meal(
            name="Pasta",
            meal_type="dinner",
            recipe_url="https://example.com/pasta",
            recipe_notes="Use fresh basil",
            servings=4,
            prep_time_min=30,
            tags="quick,italian",
        )
        assert result["status"] == "added"
        meal = mp.get_meal(result["meal_id"])
        assert meal["name"] == "Pasta"
        assert meal["recipe_url"] == "https://example.com/pasta"
        assert meal["servings"] == 4
        assert meal["prep_time_min"] == 30
        assert meal["tags"] == "quick,italian"

    def test_get_meal_not_found(self):
        result = mp.get_meal(9999)
        assert "error" in result

    def test_get_meal_with_ingredients(self):
        mid = _add_sample_meal()
        mp.add_ingredient(mid, "chicken breast", "2 lbs", "meat")
        mp.add_ingredient(mid, "soy sauce", "3 tbsp", "pantry")
        meal = mp.get_meal(mid)
        assert len(meal["ingredients"]) == 2
        assert meal["ingredients"][0]["item"] == "chicken breast"

    def test_get_meal_includes_planned_dates(self):
        mid = _add_sample_meal()
        mp.schedule_meal(mid, "2026-04-07")
        mp.schedule_meal(mid, "2026-04-14")
        meal = mp.get_meal(mid)
        assert len(meal["planned_dates"]) == 2
        assert meal["planned_dates"][0]["date"] == "2026-04-07"

    def test_update_meal(self):
        mid = _add_sample_meal()
        result = mp.update_meal(mid, name="Tofu Stir Fry", tags="vegan,quick")
        assert result["status"] == "updated"
        assert "name" in result["fields"]
        assert "tags" in result["fields"]
        meal = mp.get_meal(mid)
        assert meal["name"] == "Tofu Stir Fry"
        assert meal["tags"] == "vegan,quick"

    def test_update_meal_type(self):
        """Verify meal_type can be updated to any value including dinner."""
        mid = _add_sample_meal()
        mp.update_meal(mid, meal_type="lunch")
        assert mp.get_meal(mid)["meal_type"] == "lunch"
        mp.update_meal(mid, meal_type="dinner")
        assert mp.get_meal(mid)["meal_type"] == "dinner"

    def test_update_meal_not_found(self):
        result = mp.update_meal(9999, name="Ghost")
        assert "error" in result

    def test_update_meal_nothing_to_update(self):
        mid = _add_sample_meal()
        result = mp.update_meal(mid)
        assert "error" in result
        assert "nothing" in result["error"]

    def test_remove_meal(self):
        mid = _add_sample_meal()
        result = mp.remove_meal(mid)
        assert result["status"] == "removed"
        assert "error" in mp.get_meal(mid)

    def test_remove_meal_not_found(self):
        result = mp.remove_meal(9999)
        assert "error" in result

    def test_remove_meal_cascades_ingredients(self):
        mid = _add_sample_meal()
        mp.add_ingredient(mid, "chicken", "1 lb", "meat")
        mp.add_ingredient(mid, "rice", "2 cups", "pantry")
        mp.remove_meal(mid)
        with mp.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM meal_ingredients WHERE meal_id = ?", (mid,)
            ).fetchall()
            assert len(rows) == 0

    def test_remove_meal_cascades_planned_meals(self):
        mid = _add_sample_meal()
        mp.schedule_meal(mid, "2026-04-07")
        mp.remove_meal(mid)
        with mp.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM planned_meals WHERE meal_id = ?", (mid,)
            ).fetchall()
            assert len(rows) == 0

    def test_rate_meal(self):
        mid = _add_sample_meal()
        result = mp.rate_meal(mid, 4)
        assert result["status"] == "rated"
        assert mp.get_meal(mid)["rating"] == 4

    def test_rate_meal_invalid(self):
        mid = _add_sample_meal()
        result = mp.rate_meal(mid, 0)
        assert "error" in result
        result = mp.rate_meal(mid, 6)
        assert "error" in result

    def test_rate_meal_not_found(self):
        result = mp.rate_meal(9999, 3)
        assert "error" in result

    def test_list_meals_all(self):
        _add_sample_meal("Tacos")
        _add_sample_meal("Pasta")
        meals = mp.list_meals()
        assert len(meals) == 2

    def test_list_meals_by_tags(self):
        _add_sample_meal("Quick Tacos", tags="quick,mexican")
        _add_sample_meal("Slow Roast", tags="slow,comfort")
        meals = mp.list_meals(tags="quick")
        assert len(meals) == 1
        assert meals[0]["name"] == "Quick Tacos"

    def test_list_meals_invalid_week_date(self):
        """Invalid date format should return error, not raise."""
        meals = mp.list_meals(week="next monday")
        assert len(meals) == 1
        assert "error" in meals[0]

    def test_list_meals_by_multiple_tags(self):
        _add_sample_meal("Quick Mexican", tags="quick,mexican")
        _add_sample_meal("Quick Italian", tags="quick,italian")
        _add_sample_meal("Slow Roast", tags="slow,comfort")
        meals = mp.list_meals(tags="quick,mexican")
        assert len(meals) == 1
        assert meals[0]["name"] == "Quick Mexican"


# ── Scheduling ──────────────────────────────────────────────────────────────

class TestScheduling:
    def test_schedule_meal(self):
        mid = _add_sample_meal()
        result = mp.schedule_meal(mid, "2026-04-07")
        assert result["status"] == "scheduled"
        assert result["plan_id"] is not None
        assert result["date"] == "2026-04-07"

    def test_schedule_meal_uses_meal_type(self):
        mid = _add_sample_meal(meal_type="lunch")
        result = mp.schedule_meal(mid, "2026-04-07")
        assert result["meal_type"] == "lunch"

    def test_schedule_meal_override_type(self):
        mid = _add_sample_meal(meal_type="dinner")
        result = mp.schedule_meal(mid, "2026-04-07", meal_type="lunch")
        assert result["meal_type"] == "lunch"

    def test_schedule_meal_invalid_date(self):
        mid = _add_sample_meal()
        result = mp.schedule_meal(mid, "next monday")
        assert "error" in result

    def test_schedule_meal_not_found(self):
        result = mp.schedule_meal(9999, "2026-04-07")
        assert "error" in result

    def test_unschedule_meal(self):
        mid = _add_sample_meal()
        scheduled = mp.schedule_meal(mid, "2026-04-07")
        result = mp.unschedule_meal(scheduled["plan_id"])
        assert result["status"] == "unscheduled"

    def test_unschedule_meal_not_found(self):
        result = mp.unschedule_meal(9999)
        assert "error" in result

    def test_schedule_keeps_meal_in_library(self):
        """Scheduling a meal does not remove it from the recipe library."""
        mid = _add_sample_meal("Tacos")
        mp.schedule_meal(mid, "2026-04-07")
        meals = mp.list_meals()
        assert any(m["name"] == "Tacos" for m in meals)

    def test_list_meals_by_date(self):
        m1 = _add_sample_meal("Tacos")
        m2 = _add_sample_meal("Pasta")
        mp.schedule_meal(m1, "2026-04-07")
        mp.schedule_meal(m2, "2026-04-08")
        meals = mp.list_meals(date="2026-04-07")
        assert len(meals) == 1
        assert meals[0]["name"] == "Tacos"

    def test_list_meals_by_week(self):
        m1 = _add_sample_meal("Mon")
        m2 = _add_sample_meal("Wed")
        m3 = _add_sample_meal("Next Mon")
        mp.schedule_meal(m1, "2026-04-06")
        mp.schedule_meal(m2, "2026-04-08")
        mp.schedule_meal(m3, "2026-04-13")
        meals = mp.list_meals(week="2026-04-06")
        assert len(meals) == 2
        names = {m["name"] for m in meals}
        assert names == {"Mon", "Wed"}

    def test_same_meal_multiple_dates(self):
        mid = _add_sample_meal("Tacos")
        mp.schedule_meal(mid, "2026-04-07")
        mp.schedule_meal(mid, "2026-04-14")
        meals = mp.list_meals(date="2026-04-07")
        assert len(meals) == 1
        meals = mp.list_meals(date="2026-04-14")
        assert len(meals) == 1


# ── Ingredients ─────────────────────────────────────────────────────────────

class TestIngredients:
    def test_add_ingredient(self):
        mid = _add_sample_meal()
        result = mp.add_ingredient(mid, "chicken breast", "2 lbs", "meat")
        assert result["status"] == "added"
        assert result["item"] == "chicken breast"

    def test_add_ingredient_meal_not_found(self):
        result = mp.add_ingredient(9999, "ghost pepper", "1", "produce")
        assert "error" in result

    def test_ingredients_ordered(self):
        mid = _add_sample_meal()
        mp.add_ingredient(mid, "a_first", "1", "produce")
        mp.add_ingredient(mid, "b_second", "2", "dairy")
        meal = mp.get_meal(mid)
        assert meal["ingredients"][0]["item"] == "a_first"
        assert meal["ingredients"][1]["item"] == "b_second"


# ── Suggest ─────────────────────────────────────────────────────────────────

class TestSuggest:
    def test_suggest_returns_all_library_meals(self):
        _add_sample_meal("Tacos")
        _add_sample_meal("Pasta")
        meals = mp.suggest_meals(count=5)
        assert len(meals) == 2

    def test_suggest_prefers_rated(self):
        _add_sample_meal("Low Rated", rating=1)
        _add_sample_meal("High Rated", rating=5)
        _add_sample_meal("Unrated")
        meals = mp.suggest_meals(count=1)
        assert meals[0]["name"] == "High Rated"

    def test_suggest_excludes_recently_planned(self):
        """Meals scheduled in the last N days should be excluded from suggestions."""
        mid = _add_sample_meal("Recent Dinner")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mp.schedule_meal(mid, today)
        meals = mp.suggest_meals(count=5, exclude_recent=14)
        assert len(meals) == 0  # the only meal is recently planned

    def test_suggest_includes_old_planned(self):
        """Meals planned more than N days ago should still be suggested."""
        mid = _add_sample_meal("Old Dinner")
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        mp.schedule_meal(mid, old_date)
        meals = mp.suggest_meals(count=5, exclude_recent=14)
        assert len(meals) == 1
        assert meals[0]["name"] == "Old Dinner"

    def test_suggest_with_tags(self):
        _add_sample_meal("Quick Tacos", tags="quick,mexican")
        _add_sample_meal("Slow Roast", tags="slow,comfort")
        meals = mp.suggest_meals(tags="quick")
        assert len(meals) == 1
        assert meals[0]["name"] == "Quick Tacos"

    def test_suggest_respects_count(self):
        for i in range(10):
            _add_sample_meal(f"Meal {i}")
        meals = mp.suggest_meals(count=3)
        assert len(meals) == 3

    def test_suggest_no_exclude(self):
        """With exclude_recent=0, all meals are eligible."""
        mid = _add_sample_meal("Today's Meal")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mp.schedule_meal(mid, today)
        meals = mp.suggest_meals(count=5, exclude_recent=0)
        assert len(meals) == 1


# ── Grocery Lists ───────────────────────────────────────────────────────────

class TestGroceryLists:
    def test_create_empty_list(self):
        result = mp.create_grocery_list(name="Test List")
        assert result["status"] == "created"
        assert result["list_id"] is not None
        assert result["items_added"] == 0

    def test_create_list_default_name(self):
        result = mp.create_grocery_list()
        assert result["status"] == "created"
        assert "Grocery list" in result["name"]

    def test_add_grocery_item(self):
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        result = mp.add_grocery_item(lid, "milk", "1 gallon", "dairy")
        assert result["status"] == "added"

    def test_add_grocery_item_list_not_found(self):
        result = mp.add_grocery_item(9999, "milk")
        assert "error" in result

    def test_check_grocery_item_exact_match(self):
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        mp.add_grocery_item(lid, "milk", "1 gallon", "dairy")
        result = mp.check_grocery_item(lid, "milk")
        assert result["status"] == "checked"
        items = mp.list_grocery(list_id=lid)
        assert items["items"][0]["checked"] == 1

    def test_check_grocery_item_case_insensitive(self):
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        mp.add_grocery_item(lid, "Whole Milk", "1 gallon", "dairy")
        result = mp.check_grocery_item(lid, "whole milk")
        assert result["status"] == "checked"

    def test_check_does_not_substring_match(self):
        """Checking 'apple' should not check 'pineapple'."""
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        mp.add_grocery_item(lid, "apple", "3", "produce")
        mp.add_grocery_item(lid, "pineapple", "1", "produce")
        mp.check_grocery_item(lid, "apple")
        items = mp.list_grocery(list_id=lid)
        apple = next(i for i in items["items"] if i["item"] == "apple")
        pineapple = next(i for i in items["items"] if i["item"] == "pineapple")
        assert apple["checked"] == 1
        assert pineapple["checked"] == 0

    def test_uncheck_grocery_item(self):
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        mp.add_grocery_item(lid, "milk", "1 gallon", "dairy")
        mp.check_grocery_item(lid, "milk")
        result = mp.check_grocery_item(lid, "milk", checked=False)
        assert result["status"] == "unchecked"
        items = mp.list_grocery(list_id=lid)
        assert items["items"][0]["checked"] == 0

    def test_check_grocery_item_not_found(self):
        gl = mp.create_grocery_list(name="Test")
        result = mp.check_grocery_item(gl["list_id"], "nonexistent")
        assert "error" in result

    def test_list_grocery_all_lists(self):
        mp.create_grocery_list(name="List A")
        mp.create_grocery_list(name="List B")
        result = mp.list_grocery()
        assert len(result) == 2

    def test_list_grocery_specific_list(self):
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        mp.add_grocery_item(lid, "milk", "1 gal", "dairy")
        mp.add_grocery_item(lid, "eggs", "1 dozen", "dairy")
        result = mp.list_grocery(list_id=lid)
        assert result["name"] == "Test"
        assert len(result["items"]) == 2

    def test_list_grocery_active_only(self):
        gl = mp.create_grocery_list(name="Test")
        lid = gl["list_id"]
        mp.add_grocery_item(lid, "milk")
        mp.add_grocery_item(lid, "eggs")
        mp.check_grocery_item(lid, "milk")
        result = mp.list_grocery(list_id=lid, active_only=True)
        assert len(result["items"]) == 1
        assert result["items"][0]["item"] == "eggs"

    def test_list_grocery_not_found(self):
        result = mp.list_grocery(list_id=9999)
        assert "error" in result

    def test_complete_list(self):
        gl = mp.create_grocery_list(name="Test")
        result = mp.complete_list(gl["list_id"])
        assert result["status"] == "completed"

    def test_complete_list_not_found(self):
        result = mp.complete_list(9999)
        assert "error" in result

    def test_create_list_from_planned_meals_deduplicates(self):
        """Ingredients from multiple planned meals should be deduplicated."""
        m1 = _add_sample_meal("Meal A")
        m2 = _add_sample_meal("Meal B")
        mp.add_ingredient(m1, "Chicken Breast", "1 lb", "meat")
        mp.add_ingredient(m2, "chicken breast", "2 lbs", "meat")
        mp.add_ingredient(m1, "rice", "2 cups", "pantry")
        mp.schedule_meal(m1, "2026-04-07")
        mp.schedule_meal(m2, "2026-04-08")

        gl = mp.create_grocery_list(name="Week", from_meals_range="2026-04-07,2026-04-08")
        assert gl["items_added"] == 2  # chicken breast (merged) + rice

        items = mp.list_grocery(list_id=gl["list_id"])
        chicken_items = [i for i in items["items"] if "chicken" in i["item"].lower()]
        assert len(chicken_items) == 1
        assert "+" in chicken_items[0]["quantity"]

    def test_create_list_from_planned_meals_excludes_pantry(self):
        """Pantry items should be excluded from auto-generated grocery lists."""
        mid = _add_sample_meal("Pasta")
        mp.add_ingredient(mid, "pasta", "1 lb", "pantry")
        mp.add_ingredient(mid, "olive oil", "2 tbsp", "pantry")
        mp.add_ingredient(mid, "fresh tomatoes", "4", "produce")
        mp.schedule_meal(mid, "2026-04-07")

        mp.add_pantry("olive oil", "pantry")

        gl = mp.create_grocery_list(from_meals_range="2026-04-07,2026-04-07")
        assert gl["items_added"] == 2  # pasta + fresh tomatoes, NOT olive oil

    def test_create_list_only_includes_planned_meals(self):
        """Only meals that are scheduled in the range should contribute ingredients."""
        m1 = _add_sample_meal("Planned")
        m2 = _add_sample_meal("Unplanned")
        mp.add_ingredient(m1, "chicken", "1 lb", "meat")
        mp.add_ingredient(m2, "beef", "1 lb", "meat")
        mp.schedule_meal(m1, "2026-04-07")
        # m2 is NOT scheduled

        gl = mp.create_grocery_list(from_meals_range="2026-04-07,2026-04-07")
        assert gl["items_added"] == 1  # only chicken from the planned meal

    def test_create_list_from_meals_invalid_date(self):
        result = mp.create_grocery_list(from_meals_range="next monday,2026-04-12")
        assert "error" in result

    def test_create_list_from_meals_invalid_range(self):
        result = mp.create_grocery_list(from_meals_range="bad-format")
        assert "error" in result

    def test_create_list_from_meals_no_meals(self):
        gl = mp.create_grocery_list(from_meals_range="2026-01-01,2026-01-01")
        assert gl["items_added"] == 0


# ── Pantry ──────────────────────────────────────────────────────────────────

class TestPantry:
    def test_add_pantry(self):
        result = mp.add_pantry("olive oil", "pantry", "Costco bulk")
        assert result["status"] == "added"

    def test_add_pantry_duplicate(self):
        mp.add_pantry("olive oil")
        result = mp.add_pantry("olive oil")
        assert "error" in result
        assert "already in pantry" in result["error"]

    def test_list_pantry(self):
        mp.add_pantry("olive oil", "pantry")
        mp.add_pantry("salt", "pantry")
        mp.add_pantry("milk", "dairy")
        items = mp.list_pantry()
        assert len(items) == 3

    def test_list_pantry_by_category(self):
        mp.add_pantry("olive oil", "pantry")
        mp.add_pantry("milk", "dairy")
        items = mp.list_pantry(category="dairy")
        assert len(items) == 1
        assert items[0]["item"] == "milk"

    def test_remove_pantry(self):
        mp.add_pantry("olive oil")
        result = mp.remove_pantry("olive oil")
        assert result["status"] == "removed"
        assert len(mp.list_pantry()) == 0

    def test_remove_pantry_case_insensitive(self):
        mp.add_pantry("Olive Oil")
        result = mp.remove_pantry("olive oil")
        assert result["status"] == "removed"

    def test_remove_pantry_not_found(self):
        result = mp.remove_pantry("nonexistent")
        assert "error" in result

#!/usr/bin/env python3
"""meal_plan.py — SQLite-backed meal planning, grocery lists, and pantry for Homer.

Schema: meals (recipe library), planned_meals (schedule), meal_ingredients,
        grocery_lists, grocery_items, pantry.
DB location: state/meals.db (inside nanobot workspace) or HOMER_MEALS_DB env var.

All output is JSON: {"status": "..."} on success, {"error": "..."} + exit 1 on failure.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.resolve()
_WORKSPACE_DIR = Path(
    os.environ.get(
        "HOMER_WORKSPACE",
        str(REPO_ROOT / "context" / ".nanobot_workspace"),
    )
)
DEFAULT_DB_PATH = _WORKSPACE_DIR / "state" / "meals.db"


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return the meals DB path. Override with HOMER_MEALS_DB env var."""
    env = os.environ.get("HOMER_MEALS_DB")
    return Path(env) if env else DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the meals DB and ensure tables exist."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meals (
            meal_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            meal_type       TEXT NOT NULL DEFAULT 'dinner',
            recipe_url      TEXT,
            recipe_notes    TEXT,
            servings        INTEGER,
            prep_time_min   INTEGER,
            rating          INTEGER,
            tags            TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS planned_meals (
            plan_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_id         INTEGER NOT NULL,
            date            TEXT NOT NULL,
            meal_type       TEXT NOT NULL DEFAULT 'dinner',
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (meal_id) REFERENCES meals(meal_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meal_ingredients (
            ingredient_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_id         INTEGER NOT NULL,
            item            TEXT NOT NULL,
            quantity        TEXT,
            category        TEXT,
            FOREIGN KEY (meal_id) REFERENCES meals(meal_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS grocery_lists (
            list_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS grocery_items (
            item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id         INTEGER NOT NULL,
            item            TEXT NOT NULL,
            quantity        TEXT,
            category        TEXT,
            checked         INTEGER NOT NULL DEFAULT 0,
            source          TEXT,
            FOREIGN KEY (list_id) REFERENCES grocery_lists(list_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS pantry (
            pantry_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            item            TEXT NOT NULL UNIQUE,
            category        TEXT,
            notes           TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_date(value: str) -> Optional[str]:
    """Return an error message if value is not YYYY-MM-DD, else None."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return None
    except ValueError:
        return f"invalid date format '{value}', expected YYYY-MM-DD"


def _ok(data: dict) -> None:
    print(json.dumps(data, indent=2))


def _err(msg: str) -> None:
    print(json.dumps({"error": msg}))
    sys.exit(1)


# ---------------------------------------------------------------------------
# Meal CRUD (recipe library)
# ---------------------------------------------------------------------------

def add_meal(
    name: str,
    meal_type: str = "dinner",
    recipe_url: Optional[str] = None,
    recipe_notes: Optional[str] = None,
    servings: Optional[int] = None,
    prep_time_min: Optional[int] = None,
    tags: Optional[str] = None,
) -> dict:
    """Insert a meal into the recipe library. Returns the created meal dict."""
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO meals (name, meal_type, recipe_url, recipe_notes,
                                  servings, prep_time_min, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, meal_type, recipe_url, recipe_notes, servings,
             prep_time_min, tags),
        )
        conn.commit()
        meal_id = cursor.lastrowid
    return {"status": "added", "meal_id": meal_id, "name": name}


def add_ingredient(
    meal_id: int,
    item: str,
    quantity: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Add an ingredient to a meal."""
    with get_conn() as conn:
        # verify meal exists
        row = conn.execute(
            "SELECT meal_id FROM meals WHERE meal_id = ?", (meal_id,)
        ).fetchone()
        if not row:
            return {"error": f"meal_id {meal_id} not found"}
        conn.execute(
            """INSERT INTO meal_ingredients (meal_id, item, quantity, category)
               VALUES (?, ?, ?, ?)""",
            (meal_id, item, quantity, category),
        )
        conn.commit()
    return {"status": "added", "meal_id": meal_id, "item": item}


def rate_meal(meal_id: int, rating: int) -> dict:
    """Rate a meal 1-5."""
    if rating < 1 or rating > 5:
        return {"error": "rating must be 1-5"}
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE meals SET rating = ? WHERE meal_id = ?", (rating, meal_id)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"meal_id {meal_id} not found"}
    return {"status": "rated", "meal_id": meal_id, "rating": rating}


def get_meal(meal_id: int) -> dict:
    """Get full meal detail including ingredients and planned dates."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meals WHERE meal_id = ?", (meal_id,)
        ).fetchone()
        if not row:
            return {"error": f"meal_id {meal_id} not found"}
        meal = dict(row)
        ingredients = conn.execute(
            "SELECT * FROM meal_ingredients WHERE meal_id = ? ORDER BY ingredient_id",
            (meal_id,),
        ).fetchall()
        meal["ingredients"] = [dict(i) for i in ingredients]
        planned = conn.execute(
            "SELECT plan_id, date, meal_type FROM planned_meals WHERE meal_id = ? ORDER BY date",
            (meal_id,),
        ).fetchall()
        meal["planned_dates"] = [dict(p) for p in planned]
    return meal


def update_meal(meal_id: int, **kwargs) -> dict:
    """Update meal fields. Only non-None kwargs are applied."""
    allowed = {"name", "meal_type", "recipe_url", "recipe_notes",
               "servings", "prep_time_min", "rating", "tags"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return {"error": "nothing to update"}
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [meal_id]
    with get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE meals SET {set_clause} WHERE meal_id = ?", values
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"meal_id {meal_id} not found"}
    return {"status": "updated", "meal_id": meal_id, "fields": list(updates.keys())}


def remove_meal(meal_id: int) -> dict:
    """Remove a meal and cascade to ingredients and planned entries."""
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM meals WHERE meal_id = ?", (meal_id,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"meal_id {meal_id} not found"}
    return {"status": "removed", "meal_id": meal_id}


# ---------------------------------------------------------------------------
# Meal scheduling (planned_meals)
# ---------------------------------------------------------------------------

def schedule_meal(
    meal_id: int,
    date: str,
    meal_type: Optional[str] = None,
) -> dict:
    """Schedule a meal from the library on a specific date."""
    err = _validate_date(date)
    if err:
        return {"error": err}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT meal_id, name, meal_type FROM meals WHERE meal_id = ?",
            (meal_id,),
        ).fetchone()
        if not row:
            return {"error": f"meal_id {meal_id} not found"}
        mt = meal_type or row["meal_type"]
        cursor = conn.execute(
            "INSERT INTO planned_meals (meal_id, date, meal_type) VALUES (?, ?, ?)",
            (meal_id, date, mt),
        )
        conn.commit()
        plan_id = cursor.lastrowid
    return {
        "status": "scheduled",
        "plan_id": plan_id,
        "meal_id": meal_id,
        "name": row["name"],
        "date": date,
        "meal_type": mt,
    }


def unschedule_meal(plan_id: int) -> dict:
    """Remove a planned meal entry. The meal stays in the library."""
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM planned_meals WHERE plan_id = ?", (plan_id,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"plan_id {plan_id} not found"}
    return {"status": "unscheduled", "plan_id": plan_id}


def list_meals(
    date: Optional[str] = None,
    week: Optional[str] = None,
    unscheduled: bool = False,
    tags: Optional[str] = None,
) -> list[dict]:
    """List meals with optional filters.

    --date / --week: show planned meals for that date/week (joins planned_meals).
    --unscheduled: show library meals (all meals in the recipe library).
    --tags: filter by tags (works with both scheduled and library views).
    No date/week/unscheduled: show all meals in the library.
    """
    if date:
        err = _validate_date(date)
        if err:
            return [{"error": err}]
    if week:
        err = _validate_date(week)
        if err:
            return [{"error": err}]

    with get_conn() as conn:
        if date or week:
            # Query planned_meals joined with meals
            clauses: list[str] = []
            params: list = []
            if date:
                clauses.append("p.date = ?")
                params.append(date)
            elif week:
                end = (datetime.strptime(week, "%Y-%m-%d") + timedelta(days=6)).strftime(
                    "%Y-%m-%d"
                )
                clauses.append("p.date >= ? AND p.date <= ?")
                params.extend([week, end])

            if tags:
                for tag in tags.split(","):
                    tag = tag.strip()
                    clauses.append("(',' || LOWER(m.tags) || ',' LIKE ?)")
                    params.append(f"%,{tag.lower()},%")

            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = conn.execute(
                f"""SELECT m.*, p.plan_id, p.date, p.meal_type AS planned_meal_type
                    FROM planned_meals p
                    JOIN meals m ON p.meal_id = m.meal_id
                    {where}
                    ORDER BY p.date, p.meal_type""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        else:
            # Library view: all meals in the recipe library
            clauses = []
            params = []
            if unscheduled:
                # "unscheduled" still returns all library meals (they're all in the library)
                pass
            if tags:
                for tag in tags.split(","):
                    tag = tag.strip()
                    clauses.append("(',' || LOWER(tags) || ',' LIKE ?)")
                    params.append(f"%,{tag.lower()},%")

            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM meals{where} ORDER BY name", params
            ).fetchall()
            return [dict(r) for r in rows]


def suggest_meals(
    count: int = 5,
    tags: Optional[str] = None,
    exclude_recent: int = 14,
) -> list[dict]:
    """Suggest meals from the recipe library.

    Prefers higher-rated meals. Excludes meals planned in the last N days.
    """
    clauses: list[str] = []
    params: list = []

    if exclude_recent > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=exclude_recent)).strftime(
            "%Y-%m-%d"
        )
        clauses.append(
            """meal_id NOT IN (
                SELECT meal_id FROM planned_meals WHERE date >= ?
            )"""
        )
        params.append(cutoff)

    if tags:
        for tag in tags.split(","):
            tag = tag.strip()
            clauses.append("(',' || LOWER(tags) || ',' LIKE ?)")
            params.append(f"%,{tag.lower()},%")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(count)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM meals{where}
                ORDER BY COALESCE(rating, 0) DESC, RANDOM()
                LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Grocery lists
# ---------------------------------------------------------------------------

def create_grocery_list(
    name: Optional[str] = None,
    from_meals_range: Optional[str] = None,
) -> dict:
    """Create a grocery list, optionally populated from planned meals in a date range.

    from_meals_range: "YYYY-MM-DD,YYYY-MM-DD"
    Deduplicates ingredients (case-insensitive), merges quantities, excludes pantry items.
    """
    list_name = name or f"Grocery list {_now_utc()[:10]}"

    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO grocery_lists (name) VALUES (?)", (list_name,)
        )
        conn.commit()
        list_id = cursor.lastrowid

        items_added = 0
        if from_meals_range:
            parts = from_meals_range.split(",")
            if len(parts) != 2:
                return {"error": "from-meals must be YYYY-MM-DD,YYYY-MM-DD"}
            start, end = parts[0].strip(), parts[1].strip()
            for d in (start, end):
                err = _validate_date(d)
                if err:
                    return {"error": err}

            # gather ingredients from meals planned in that range
            rows = conn.execute(
                """SELECT mi.item, mi.quantity, mi.category
                   FROM meal_ingredients mi
                   JOIN planned_meals pm ON mi.meal_id = pm.meal_id
                   WHERE pm.date >= ? AND pm.date <= ?""",
                (start, end),
            ).fetchall()

            # load pantry items for exclusion
            pantry_items = {
                r["item"].lower()
                for r in conn.execute("SELECT item FROM pantry").fetchall()
            }

            # deduplicate by lowercase item name
            merged: dict[str, dict] = {}
            for r in rows:
                key = r["item"].lower()
                if key in pantry_items:
                    continue
                if key in merged:
                    existing_qty = merged[key]["quantity"]
                    new_qty = r["quantity"]
                    if existing_qty and new_qty:
                        merged[key]["quantity"] = f"{existing_qty} + {new_qty}"
                    elif new_qty:
                        merged[key]["quantity"] = new_qty
                else:
                    merged[key] = {
                        "item": r["item"],
                        "quantity": r["quantity"],
                        "category": r["category"],
                    }

            for entry in merged.values():
                conn.execute(
                    """INSERT INTO grocery_items (list_id, item, quantity, category, source)
                       VALUES (?, ?, ?, ?, ?)""",
                    (list_id, entry["item"], entry["quantity"], entry["category"],
                     "meal-plan"),
                )
                items_added += 1
            conn.commit()

    return {
        "status": "created",
        "list_id": list_id,
        "name": list_name,
        "items_added": items_added,
    }


def add_grocery_item(
    list_id: int,
    item: str,
    quantity: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Add a manual item to a grocery list."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT list_id FROM grocery_lists WHERE list_id = ?", (list_id,)
        ).fetchone()
        if not row:
            return {"error": f"list_id {list_id} not found"}
        conn.execute(
            """INSERT INTO grocery_items (list_id, item, quantity, category, source)
               VALUES (?, ?, ?, ?, 'manual')""",
            (list_id, item, quantity, category),
        )
        conn.commit()
    return {"status": "added", "list_id": list_id, "item": item}


def check_grocery_item(list_id: int, item: str, checked: bool = True) -> dict:
    """Mark/unmark a grocery item as bought. Case-insensitive exact match on item name."""
    action = "checked" if checked else "unchecked"
    with get_conn() as conn:
        cursor = conn.execute(
            """UPDATE grocery_items SET checked = ?
               WHERE list_id = ? AND LOWER(item) = LOWER(?)""",
            (1 if checked else 0, list_id, item),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"no item matching '{item}' in list {list_id}"}
    return {"status": action, "list_id": list_id, "item": item, "matched": cursor.rowcount}


def list_grocery(
    list_id: Optional[int] = None,
    active_only: bool = False,
) -> list[dict] | dict:
    """List grocery lists or items in a specific list."""
    with get_conn() as conn:
        if list_id is None:
            rows = conn.execute(
                "SELECT * FROM grocery_lists ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        else:
            list_row = conn.execute(
                "SELECT * FROM grocery_lists WHERE list_id = ?", (list_id,)
            ).fetchone()
            if not list_row:
                return {"error": f"list_id {list_id} not found"}
            query = "SELECT * FROM grocery_items WHERE list_id = ?"
            params: list = [list_id]
            if active_only:
                query += " AND checked = 0"
            query += " ORDER BY category, item"
            items = conn.execute(query, params).fetchall()
            result = dict(list_row)
            result["items"] = [dict(i) for i in items]
            return result


def complete_list(list_id: int) -> dict:
    """Mark a grocery list as completed."""
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE grocery_lists SET status = 'completed' WHERE list_id = ?",
            (list_id,),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"list_id {list_id} not found"}
    return {"status": "completed", "list_id": list_id}


# ---------------------------------------------------------------------------
# Pantry
# ---------------------------------------------------------------------------

def add_pantry(
    item: str,
    category: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Add an item to the pantry."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO pantry (item, category, notes) VALUES (?, ?, ?)",
                (item, category, notes),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return {"error": f"'{item}' already in pantry"}
    return {"status": "added", "item": item}


def list_pantry(category: Optional[str] = None) -> list[dict]:
    """List pantry items, optionally filtered by category."""
    with get_conn() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM pantry WHERE LOWER(category) = ? ORDER BY item",
                (category.lower(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pantry ORDER BY category, item"
            ).fetchall()
    return [dict(r) for r in rows]


def remove_pantry(item: str) -> dict:
    """Remove a pantry item. Case-insensitive match."""
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM pantry WHERE LOWER(item) = ?", (item.lower(),)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"'{item}' not found in pantry"}
    return {"status": "removed", "item": item}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _JsonParser(argparse.ArgumentParser):
    """ArgumentParser that outputs errors as JSON to stdout (tool contract)."""

    def error(self, message: str) -> None:
        print(json.dumps({"error": message}))
        sys.exit(1)


def main() -> None:
    parser = _JsonParser(description="Meal planning, grocery lists, and pantry.")

    # --- Meal actions ---
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add-meal", action="store_true")
    group.add_argument("--add-ingredient", action="store_true")
    group.add_argument("--rate-meal", action="store_true")
    group.add_argument("--list-meals", action="store_true")
    group.add_argument("--get-meal", action="store_true")
    group.add_argument("--update-meal", action="store_true")
    group.add_argument("--remove-meal", action="store_true")
    group.add_argument("--suggest", action="store_true")
    group.add_argument("--schedule-meal", action="store_true")
    group.add_argument("--unschedule-meal", action="store_true")
    # --- Grocery actions ---
    group.add_argument("--create-list", action="store_true")
    group.add_argument("--add-grocery-item", action="store_true")
    group.add_argument("--check-grocery-item", action="store_true")
    group.add_argument("--uncheck-grocery-item", action="store_true")
    group.add_argument("--list-grocery", action="store_true")
    group.add_argument("--complete-list", action="store_true")
    # --- Pantry actions ---
    group.add_argument("--add-pantry", action="store_true")
    group.add_argument("--list-pantry", action="store_true")
    group.add_argument("--remove-pantry", action="store_true")

    # Shared arguments
    parser.add_argument("--meal-id", type=int)
    parser.add_argument("--plan-id", type=int)
    parser.add_argument("--list-id", type=int)
    parser.add_argument("--name")
    parser.add_argument("--item")
    parser.add_argument("--date")
    parser.add_argument("--week")
    parser.add_argument("--meal-type")
    parser.add_argument("--recipe-url")
    parser.add_argument("--recipe-notes")
    parser.add_argument("--servings", type=int)
    parser.add_argument("--prep-time", type=int)
    parser.add_argument("--rating", type=int)
    parser.add_argument("--tags")
    parser.add_argument("--quantity")
    parser.add_argument("--category")
    parser.add_argument("--notes")
    parser.add_argument("--unscheduled", action="store_true")
    parser.add_argument("--active", action="store_true")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--exclude-recent", type=int, default=14)
    parser.add_argument("--from-meals")

    args = parser.parse_args()

    # --- Dispatch ---
    if args.add_meal:
        if not args.name:
            _err("--add-meal requires --name")
        result = add_meal(
            name=args.name,
            meal_type=args.meal_type or "dinner",
            recipe_url=args.recipe_url,
            recipe_notes=args.recipe_notes,
            servings=args.servings,
            prep_time_min=args.prep_time,
            tags=args.tags,
        )
        _ok(result)

    elif args.add_ingredient:
        if not args.meal_id or not args.item:
            _err("--add-ingredient requires --meal-id and --item")
        result = add_ingredient(
            meal_id=args.meal_id,
            item=args.item,
            quantity=args.quantity,
            category=args.category,
        )
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.rate_meal:
        if not args.meal_id or args.rating is None:
            _err("--rate-meal requires --meal-id and --rating")
        result = rate_meal(args.meal_id, args.rating)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.list_meals:
        meals = list_meals(
            date=args.date,
            week=args.week,
            unscheduled=args.unscheduled,
            tags=args.tags,
        )
        _ok({"meals": meals, "count": len(meals)})

    elif args.get_meal:
        if not args.meal_id:
            _err("--get-meal requires --meal-id")
        result = get_meal(args.meal_id)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.update_meal:
        if not args.meal_id:
            _err("--update-meal requires --meal-id")
        result = update_meal(
            args.meal_id,
            name=args.name,
            meal_type=args.meal_type,
            rating=args.rating,
            tags=args.tags,
            recipe_url=args.recipe_url,
            recipe_notes=args.recipe_notes,
            servings=args.servings,
            prep_time_min=args.prep_time,
        )
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.remove_meal:
        if not args.meal_id:
            _err("--remove-meal requires --meal-id")
        result = remove_meal(args.meal_id)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.suggest:
        meals = suggest_meals(
            count=args.count,
            tags=args.tags,
            exclude_recent=args.exclude_recent,
        )
        _ok({"suggestions": meals, "count": len(meals)})

    elif args.schedule_meal:
        if not args.meal_id or not args.date:
            _err("--schedule-meal requires --meal-id and --date")
        result = schedule_meal(
            meal_id=args.meal_id,
            date=args.date,
            meal_type=args.meal_type,
        )
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.unschedule_meal:
        if not args.plan_id:
            _err("--unschedule-meal requires --plan-id")
        result = unschedule_meal(args.plan_id)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.create_list:
        result = create_grocery_list(
            name=args.name,
            from_meals_range=args.from_meals,
        )
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.add_grocery_item:
        if not args.list_id or not args.item:
            _err("--add-grocery-item requires --list-id and --item")
        result = add_grocery_item(
            list_id=args.list_id,
            item=args.item,
            quantity=args.quantity,
            category=args.category,
        )
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.check_grocery_item:
        if not args.list_id or not args.item:
            _err("--check-grocery-item requires --list-id and --item")
        result = check_grocery_item(args.list_id, args.item, checked=True)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.uncheck_grocery_item:
        if not args.list_id or not args.item:
            _err("--uncheck-grocery-item requires --list-id and --item")
        result = check_grocery_item(args.list_id, args.item, checked=False)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.list_grocery:
        result = list_grocery(list_id=args.list_id, active_only=args.active)
        if isinstance(result, dict) and "error" in result:
            _err(result["error"])
        _ok(result if isinstance(result, dict) else {"lists": result, "count": len(result)})

    elif args.complete_list:
        if not args.list_id:
            _err("--complete-list requires --list-id")
        result = complete_list(args.list_id)
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.add_pantry:
        if not args.item:
            _err("--add-pantry requires --item")
        result = add_pantry(
            item=args.item,
            category=args.category,
            notes=args.notes,
        )
        if "error" in result:
            _err(result["error"])
        _ok(result)

    elif args.list_pantry:
        items = list_pantry(category=args.category)
        _ok({"pantry": items, "count": len(items)})

    elif args.remove_pantry:
        if not args.item:
            _err("--remove-pantry requires --item")
        result = remove_pantry(args.item)
        if "error" in result:
            _err(result["error"])
        _ok(result)


if __name__ == "__main__":
    main()

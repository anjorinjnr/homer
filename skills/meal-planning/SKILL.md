---
name: meal-planning
description: Plan meals, manage recipes, generate grocery lists, and track pantry staples.
metadata: {"nanobot":{"always":false,"emoji":"🍽️"}}
---

# Meal Planning

## Rules

- All meal planning data lives in **meals.db** (SQLite). Never store meal data in context files.
- The `meals` table is the **recipe library** -- meals stay there permanently.
- To plan meals for specific dates, use `--schedule-meal` which creates entries in `planned_meals`.
- Scheduling does not remove a meal from the library; the same meal can be scheduled multiple times.
- Dietary preferences, allergies, and household size live in household context (USER.md). Read household context before suggesting meals.
- Grocery lists can be auto-generated from planned meals but also accept manual items.
- When generating a grocery list from planned meals, pantry staples are automatically excluded.
- Ratings (1-5 stars) influence suggestions: higher-rated meals are suggested more often.
- `--check-grocery-item` uses case-insensitive **exact** match on item name (not substring).

## meal_plan.py

### Meal management (recipe library)

```bash
# Add a meal to the recipe library
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --add-meal --name "Chicken Stir Fry" --meal-type dinner \
  [--recipe-url "https://..."] [--recipe-notes "Use low sodium soy sauce"] \
  [--servings 4] [--prep-time 30] [--tags "quick,kid-friendly"]

# Add ingredient to a meal
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --add-ingredient --meal-id 3 --item "chicken breast" \
  --quantity "2 lbs" [--category meat]

# Rate a meal (1-5)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --rate-meal --meal-id 3 --rating 4

# List all meals in library (optionally filter by tags)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --list-meals [--tags "quick"]

# List planned meals for a specific date or week
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --list-meals --date 2026-04-07
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --list-meals --week 2026-04-06

# Get full meal detail with ingredients and planned dates
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --get-meal --meal-id 3

# Update a meal in the library
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --update-meal --meal-id 3 [--name "..."] \
  [--meal-type lunch] [--rating 5] [--tags "quick,healthy"]

# Remove a meal (cascades to ingredients and planned entries)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --remove-meal --meal-id 3

# Suggest meals from the library (prefers higher-rated, excludes recently planned)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --suggest [--count 5] [--tags "quick"] [--exclude-recent 14]
```

### Scheduling meals

```bash
# Schedule a library meal on a date
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --schedule-meal --meal-id 3 --date 2026-04-07 \
  [--meal-type lunch]

# Remove a planned meal (meal stays in library)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --unschedule-meal --plan-id 5
```

### Grocery lists

```bash
# Create an empty grocery list
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --create-list [--name "Week of April 6"]

# Create a grocery list auto-populated from planned meals in a date range
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --create-list --name "Week of April 6" \
  --from-meals "2026-04-06,2026-04-12"

# Add a manual item
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --add-grocery-item --list-id 1 --item "milk" \
  [--quantity "1 gallon"] [--category dairy]

# Check off an item (bought) — case-insensitive exact match
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --check-grocery-item --list-id 1 --item "milk"

# Uncheck an item
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --uncheck-grocery-item --list-id 1 --item "milk"

# List all grocery lists
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --list-grocery

# List items in a specific list (--active = unchecked only)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --list-grocery --list-id 1 [--active]

# Mark list as completed
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --complete-list --list-id 1
```

### Pantry staples

```bash
# Add a pantry item (excluded from auto-generated grocery lists)
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --add-pantry --item "olive oil" [--category pantry] \
  [--notes "Costco bulk"]

# List pantry items
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --list-pantry [--category produce]

# Remove a pantry item
{HOMER_VENV} {HOMER_TOOLS}/meal_plan.py --remove-pantry --item "olive oil"
```

## Workflows

### Plan the week's meals

1. Get suggestions from the library: `--suggest --count 7`
2. User picks meals; schedule each: `--schedule-meal --meal-id N --date YYYY-MM-DD`
3. Generate the grocery list: `--create-list --name "Week of April 6" --from-meals "2026-04-06,2026-04-12"`
4. Add any extra items: `--add-grocery-item --list-id N --item "paper towels"`

### Add a new recipe

1. Add the meal: `--add-meal --name "Thai Basil Chicken" --tags "quick,spicy" --servings 4 --prep-time 25`
2. Add each ingredient: `--add-ingredient --meal-id N --item "chicken thighs" --quantity "1.5 lbs" --category meat`
3. Repeat for all ingredients

### Generate grocery list from meal plan

1. Ensure meals are scheduled for the target week (`--schedule-meal`)
2. Run `--create-list --from-meals "start,end"` -- ingredients are deduplicated and pantry staples are excluded
3. Review with `--list-grocery --list-id N`

### Rate a meal after cooking

1. `--rate-meal --meal-id N --rating 4`
2. Optionally add notes: `--update-meal --meal-id N --recipe-notes "Needed more garlic"`

### Quick dinner suggestion

1. `--suggest --count 3 --tags "quick" --exclude-recent 7`
2. Present options to the user with prep time and rating

## Calendar integration

After planning meals for the week, optionally add them to Google Calendar so the household sees what's for dinner. Use `calendar_add.py` with a title like "Dinner: Chicken Stir Fry" and set the date accordingly. This is optional and should only be done if the user requests it.

## Examples

**User:** "What should we have for dinner this week?"
1. Run `--suggest --count 7` to get rated recipe suggestions
2. Present options grouped by type (quick weeknight vs weekend project)
3. After user picks, schedule each with `--schedule-meal --meal-id N --date YYYY-MM-DD`
4. Offer to generate the grocery list

**User:** "Add my mom's lasagna recipe"
1. `--add-meal --name "Mom's Lasagna" --meal-type dinner --servings 8 --prep-time 90 --tags "comfort,family"`
2. Add ingredients one by one with `--add-ingredient`
3. Confirm: "Got it! Mom's Lasagna is saved with all 12 ingredients. Want to schedule it for this week?"

**User:** "We're going to the store, what do we need?"
1. Check for an active grocery list: `--list-grocery`
2. If one exists, show unchecked items grouped by category: `--list-grocery --list-id N --active`
3. If none exists, offer to generate one from the current meal plan

**User:** "The chicken stir fry was amazing, 5 stars"
1. Find the meal: `--list-meals --tags "stir fry"` or check recent meals
2. Rate it: `--rate-meal --meal-id N --rating 5`
3. Confirm: "Rated Chicken Stir Fry 5 stars! It'll come up more often in suggestions now."

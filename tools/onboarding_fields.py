"""Canonical onboarding field list — tiers, phrasings, and household.md mapping.

This is the single source of truth for what Homer asks new users during
onboarding. Fields are grouped by tier:

  Tier 1 — asked in cold start (baseline usefulness)
  Tier 2 — asked progressively after cold start
  Tier 3 — asked last or never

Each field describes:
  key       — stable identifier used by onboarding.py and the SQLite DB
  tier      — priority tier (1, 2, or 3)
  scalar    — True if written as "- **Key**: value" under a section;
              False if written as freeform body of a section
  section   — household.md H2 section heading
  field     — for scalar fields, the bullet key (e.g. "Name")
  phrasing  — example question Homer uses when asking this field

Edit this file to change what gets asked — no code changes needed elsewhere.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FieldDef:
    key: str
    tier: int
    scalar: bool
    section: str
    phrasing: str
    field: Optional[str] = None  # only for scalar fields


FIELDS: list[FieldDef] = [
    # ── Tier 1 — cold start baseline ──────────────────────────────────────
    FieldDef(
        key="primary_user.name",
        tier=1, scalar=True, section="Primary user", field="Name",
        phrasing="what should I call you?",
    ),
    FieldDef(
        key="primary_user.role",
        tier=1, scalar=True, section="Primary user", field="Role",
        phrasing="what do you do for work?",
    ),
    FieldDef(
        key="home.address",
        tier=1, scalar=True, section="Home", field="Address",
        phrasing="where do you live? city + state is fine, or full address if you want weather/local stuff to be accurate",
    ),
    FieldDef(
        key="partner",
        tier=1, scalar=False, section="Partner",
        phrasing="anyone else sharing the household with you? partner, roommate, etc",
    ),
    FieldDef(
        key="children",
        tier=1, scalar=False, section="Children",
        phrasing="any kids? names + ages if you're cool sharing",
    ),

    # ── Tier 2 — progressive, high value ──────────────────────────────────
    # Each group field gets its own section so writes never clobber each other.
    FieldDef(
        key="children.schools",
        tier=2, scalar=False, section="Schools",
        phrasing="what schools do the kids go to?",
    ),
    FieldDef(
        key="dietary.restrictions",
        tier=2, scalar=False, section="Dietary",
        phrasing="any dietary restrictions or strong preferences in the house?",
    ),
    FieldDef(
        key="allergies.medical",
        tier=2, scalar=False, section="Allergies",
        phrasing="any medical allergies I should know about?",
    ),
    FieldDef(
        key="pets",
        tier=2, scalar=False, section="Pets",
        phrasing="any pets?",
    ),
    FieldDef(
        key="emergency_contacts",
        tier=2, scalar=False, section="Emergency contacts",
        phrasing="who's your go-to emergency contact? just a name + how to reach them",
    ),

    # ── Tier 3 — nice to have ─────────────────────────────────────────────
    FieldDef(
        key="work_schedules",
        tier=3, scalar=False, section="Work schedules",
        phrasing="what does a typical work week look like — any fixed hours or travel?",
    ),
    FieldDef(
        key="recurring_routines",
        tier=3, scalar=False, section="Routines",
        phrasing="any weekly routines worth knowing about? (gym, church, standing dinners, that kind of thing)",
    ),
    FieldDef(
        key="preferred_names_nicknames",
        tier=3, scalar=False, section="Nicknames",
        phrasing="nicknames or preferred names for anyone in the house?",
    ),
    FieldDef(
        key="medical_basics",
        tier=3, scalar=False, section="Medical",
        phrasing="anything medical I should be generally aware of — regular meds, conditions, primary doctor?",
    ),
]


def field_by_key(key: str) -> Optional[FieldDef]:
    for f in FIELDS:
        if f.key == key:
            return f
    return None


def fields_by_tier(tier: int) -> list[FieldDef]:
    return [f for f in FIELDS if f.tier == tier]


HOUSEHOLD_TEMPLATE = """# Household

## Primary user
- **Name**: [FILL: your name]
- **Role**: [FILL: what you do]
- **Timezone**: [FILL: e.g., America/New_York]

## Partner
(none)

## Children
(none recorded)

## Home
- **Address**: [FILL: city + state, or full address]

## Pets
(none recorded)
"""

EMPTY_MARKERS = {"(none)", "(none recorded)", "(unknown)", ""}

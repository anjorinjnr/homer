---
name: health
description: Track family health records — medical visits, vaccinations, medications, and symptoms. All data stored locally, no external API calls.
metadata: {"nanobot":{"always":false,"emoji":"🏥"}}
---

# Health Records

Track family health and medical records: member profiles, doctor visits, vaccinations, medications, and symptom logs.

## Privacy & Security

All data is stored locally in a SQLite database on the host (`health.db`). No health data is ever sent to external APIs or third-party services.

**Do not store:**
- Full Social Security numbers
- Complete insurance policy documents
- Credit card or bank account numbers

**Use reference identifiers instead:** "Aetna PPO", "Group #12345", "Dr. Smith at Piedmont".

## Rules

- `health.db` is for **operational health tracking** — visits, vaccinations, medications, symptoms.
- For **permanent household-level facts** (preferred pharmacy, family doctor contact info, insurance plan names), use `context_updater.py --file health` to store in `context/user_context/health.md`.
- **Always confirm** sensitive health data with the user before writing. Never assume diagnoses or treatments.
- Member names should match household members in USER.md.
- When logging visits or symptoms, record what the user reports — do not editorialize.

## health_records.py

### Member profiles

```bash
# Add a family member
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --add-member --name "Alex" --dob 2021-07-12 --blood-type "O+" --allergies "peanuts,shellfish" --doctor "Dr. Smith" --insurance "Aetna PPO, Group #12345" --notes "..."

# List all members
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --list-members

# Get full profile (includes active meds, recent visits, upcoming vaccinations)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --get-member --name "Alex"

# Update member info
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --update-member --name "Alex" --allergies "peanuts" --doctor "Dr. Jones"

# Remove member and all associated records
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --remove-member --name "Alex"
```

### Medical visits

```bash
# Log a visit
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --log-visit --member "Alex" --date 2026-03-15 --provider "Dr. Smith" --type checkup --diagnosis "healthy" --treatment "none" --follow-up 2026-09-15 --notes "..."

# List visits (optionally filter by year or type)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --list-visits --member "Alex" --year 2026 --type checkup

# Upcoming follow-ups (next 30 days, all members)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --upcoming-visits
```

### Vaccinations

```bash
# Log a vaccine
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --log-vaccine --member "Alex" --vaccine "Flu" --date 2026-10-01 --provider "CVS" --lot "ABC123" --next-due 2027-10-01

# List all vaccines for a member
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --list-vaccines --member "Alex"

# Check due or overdue vaccines (all members)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --due-vaccines
```

### Medications

```bash
# Add a medication
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --add-medication --member "Alex" --name "Amoxicillin" --dosage "250mg" --frequency "twice daily" --prescriber "Dr. Smith" --pharmacy "CVS" --refill-date 2026-04-15

# List medications (--active for current only)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --list-medications --member "Alex" --active 1

# Update medication (change refill date, discontinue, etc.)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --update-medication --medication-id 1 --refill-date 2026-05-15
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --update-medication --medication-id 1 --active 0

# Check refills due within 7 days or past due
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --due-refills
```

### Symptom log

```bash
# Log symptoms
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --log-symptom --member "Alex" --symptoms "fever, cough" --severity 6 --temperature 101.2 --notes "started yesterday"

# List symptoms (default last 30 days)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --list-symptoms --member "Alex" --days 7
```

### Dashboard

```bash
# Full family dashboard (upcoming visits, due vaccines, due refills, recent symptoms)
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --dashboard

# Single member dashboard
{HOMER_VENV} {HOMER_TOOLS}/health_records.py --dashboard --member "Alex"
```

## Workflows

### Log a doctor visit

1. User says "Alex had a checkup today with Dr. Smith"
2. Confirm details: date, provider, type, diagnosis, treatment, follow-up
3. Run `--log-visit` with confirmed details
4. If follow-up was scheduled, mention it: "Got it — follow-up with Dr. Smith on Sep 15"

### Check vaccination status

1. User asks "Are Alex's vaccines up to date?"
2. Run `--list-vaccines --member "Alex"` to see history
3. Run `--due-vaccines` to check if anything is due or overdue
4. Report status naturally: "Alex's flu shot is due next month. Everything else is current."

### Track medication refills

1. Run `--due-refills` to check what needs refilling
2. Report upcoming refills: "Alex's Amoxicillin refill is due April 15 at CVS"
3. After refill, update: `--update-medication --medication-id N --refill-date YYYY-MM-DD`

### Child is sick (symptom logging)

1. User says "Alex has a fever, 101.2, and is coughing"
2. Run `--log-symptom --member "Alex" --symptoms "fever, cough" --severity 6 --temperature 101.2`
3. Check recent history: `--list-symptoms --member "Alex" --days 7`
4. If pattern emerging, mention it: "This is the third day of fever — might be worth calling Dr. Smith"

### Prepare for a doctor visit

1. User says "Alex has a checkup tomorrow, what should I bring up?"
2. Run `--get-member --name "Alex"` for full profile
3. Run `--list-symptoms --member "Alex" --days 30` for recent symptoms
4. Run `--list-medications --member "Alex" --active 1` for current meds
5. Summarize: active meds, recent symptoms, anything due for follow-up

## Examples

**User:** "Alex saw Dr. Smith today for her 5-year checkup. Everything looked good, next visit in 6 months."

**Homer:** Logs the visit with `--log-visit`, sets follow-up date 6 months out, confirms: "Logged Alex's checkup with Dr. Smith. Follow-up set for October 1."

---

**User:** "Add Alex to our health records — she was born July 12, 2021, blood type O+, allergic to peanuts. Dr. Williams is her pediatrician."

**Homer:** Runs `--add-member` with all details, confirms: "Added Alex to health records — DOB 7/12/2021, O+, peanut allergy, pediatrician Dr. Williams."

---

**User:** "What medications is everyone on?"

**Homer:** Runs `--list-members` then `--list-medications` for each member with `--active 1`. Summarizes in a natural list.

---

**User:** "Alex woke up with a fever of 102 and a sore throat"

**Homer:** Logs the symptom, checks recent symptom history, responds: "Logged — 102F fever and sore throat. I don't see any recent symptoms before today. Want me to check when her last visit with Dr. Williams was?"

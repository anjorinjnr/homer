---
name: event-management
description: Plan and coordinate events (trips, gatherings) with guests — RSVPs, itineraries, budget tracking, proactive follow-up.
metadata: {"nanobot":{"always":false,"emoji":"🎉"}}
---

# Event Management

Homer can plan and coordinate events with guests. Event metadata, open items, and notes live in `context/events/<event_id>/status.md`. Guest roster and RSVP data live in `state/events.db` (SQLite).

## General principles

**Always run `--status` before answering event questions.** Never answer from context or memory alone. If the owner asks about RSVPs, confirmations, guest status, dates, open items, or any event detail — run `--status` first. The file is the source of truth; your context may be stale.

**status.md is Homer's memory.** Everything worth remembering goes there: what was sent to guests, responses received, decisions made. Without this, Homer has no context when a follow-up reminder fires.

**Always ask for contact info before enrolling guests.** Homer cannot message or include a guest without their phone number. If the owner says "add Jake to the trip" without a number, ask before proceeding.

**Dates: only store a range when the event actually spans multiple days.** The `Dates:` string on an event is rendered verbatim on the RSVP page. A 2 PM–4 PM birthday on May 2 is a single day — store it as `"Saturday, May 2, 2026"`, not `"May 2-3, 2026"`. Only use a range (e.g. `"July 15-20, 2026"` or `"2026-07-15 to 2026-07-20"`) for true multi-day events like trips or weekend conferences. When in doubt, ask the owner. If an existing event has a range stored but is really single-day, offer to fix it.

**Proactive follow-up.** Whenever Homer sends guests something requiring a response:
1. Record what was sent with `--add-note`
2. Plant a follow-up reminder via `tasks_update.py` with enough context to act on later
3. When the reminder fires, read `--status` first — check Notes to see who already responded, and only follow up with those who haven't
4. When resolved, record the outcome with `--add-note`, mark the open item done with `--check-item`, and complete the reminder with `tasks_update.py --complete`

**When a guest sends any response Homer recognizes as an RSVP, confirmation, or material update:**
1. Record it immediately with `--add-note` (e.g. "Jake confirmed — available July 15-20")
2. Alert the owner proactively: message them with the guest's response without waiting to be asked
3. If the open item tracking that RSVP is now resolved, mark it done with `--check-item`
4. If all guests have responded, cancel any pending follow-up reminders with `tasks_update.py --complete`

**When the owner asks Homer to coordinate something with guests** (RSVPs, date proposals, picking a restaurant, etc.):
1. Create the event if it doesn't exist
2. Ask for guest phone numbers if not already known
3. Enroll guests with `manage_event_guest.py --add` (opens WhatsApp access + sends welcome message)
4. Add open items to track each outstanding task
5. Message guests via their JIDs with the request and any context they need
6. Record what was sent + plant follow-up reminders

## event_manage.py
```
# Create a new event (also creates a Google Sheet for budget):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --create --name "MTB Colorado" --event-id mtb_colorado

# Check event status:
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --status --event-id mtb_colorado

# Update event details (dates, location, lodging, or any confirmed detail):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --update --event-id mtb_colorado --field dates --value "2026-07-15 to 2026-07-20"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --update --event-id kemi_bday --field dates --value "Saturday, May 2, 2026"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --update --event-id mtb_colorado --field "Location" --value "Crested Butte, CO"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --update --event-id mtb_colorado --field "Lodging" --value "Airbnb booked, confirmation #ABC123"

# Manage open items:
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --add-item --event-id mtb_colorado --item "Book Airbnb" --assignee "@alex"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --check-item --event-id mtb_colorado --item "Airbnb"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --remove-item --event-id mtb_colorado --item "Airbnb"

# Set event lifecycle — use whatever label fits the situation. Only "archived" is reserved.
# Examples: planning, waitlisting, confirmed, deposits-due, active, cancelled, postponed
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --set-status --event-id mtb_colorado --lifecycle confirmed
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --set-status --event-id mtb_colorado --lifecycle "deposits due"

# Close an event — use this instead of setting lifecycle to archived.
# Revokes all guest access, runs final budget summary, then archives.
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --close --event-id mtb_colorado

# List all events:
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --list

# Budget summary (reads the event's Google Sheet):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --budget-summary --event-id mtb_colorado

# Append a timestamped note (decisions, guest responses, what was sent):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --add-note --event-id mtb_colorado --note "Sent brunch options to all guests: Cafe Luna, The Diner, Stack House"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --add-note --event-id mtb_colorado --note "Brunch vote: Jake=Cafe Luna, Mike=pending, Alex=pending"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --add-note --event-id mtb_colorado --note "Brunch vote complete: Cafe Luna (Jake, Alex) wins"

# Guest list with RSVP status (from SQLite):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --guests --event-id mtb_colorado

# Record an RSVP (status: confirmed, declined, maybe):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --rsvp --event-id mtb_colorado --guest "Jake" --rsvp-status confirmed --headcount 3 --note "bringing the kids"

# RSVP summary (counts by status):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --rsvp-summary --event-id mtb_colorado

# List guests who haven't responded (for follow-up):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --rsvp-pending --event-id mtb_colorado

# Configure RSVP webpage fields (what guests see on the RSVP form):
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --set-rsvp-fields --event-id mtb_colorado --fields '[{"id":"dietary","type":"select","label":"Dietary restrictions","required":true,"options":["None","Vegetarian","Vegan","Gluten-free"]},{"id":"gear","type":"checkbox","label":"Do you need to borrow camping gear?"}]'

# Set RSVP deadline and description shown on the webpage:
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --set-rsvp-deadline --event-id mtb_colorado --deadline "2026-07-01"
{HOMER_VENV} {HOMER_TOOLS}/event_manage.py --set-event-description --event-id mtb_colorado --description "Join us for a week of mountain biking in Crested Butte!"
```

## rsvp_invite.py
```
# Generate a shareable public RSVP link (anyone with the link can RSVP):
{HOMER_VENV} {HOMER_TOOLS}/rsvp_invite.py --event-id mtb_colorado --public

# Generate a personal RSVP link for a specific pre-enrolled guest:
{HOMER_VENV} {HOMER_TOOLS}/rsvp_invite.py --event-id mtb_colorado --guest "Jake"

# Generate personal links for all enrolled guests at once:
{HOMER_VENV} {HOMER_TOOLS}/rsvp_invite.py --event-id mtb_colorado --all
```

**Two RSVP modes:**
- **Public link** (`--public`): One shareable URL for the event. Anyone can RSVP by entering their name. Use when: the owner says "shareable link", "send to everyone", "post in the group chat", or there are no pre-enrolled guests.
- **Personal links** (`--guest` or `--all`): Unique URL per pre-enrolled guest. Guest name is pre-filled. Use when: the owner asks for links "for [specific guests]" or the event already has enrolled guests and the owner wants per-person tracking.

**Use `short_url` when sending invites.** Output JSON includes both `url` (long, household-scoped) and `short_url` (`<portal>/s/<code>`). Send `short_url` over WhatsApp/Telegram/SMS — it's easier to read and type. The long `url` stays valid as a fallback (e.g. if `short_url` is missing because the shortener was unreachable).

**Only generate a link when the owner asks for one.** Do not proactively generate links when setting up RSVP fields — wait until the owner asks.

**RSVP webpage workflow:**
1. Set up the RSVP form: use `--set-rsvp-fields` with fields appropriate for the event type. Field types: `text`, `number`, `select` (with `options`), `checkbox`, `textarea`. Each field needs `id`, `type`, and `label`; add `required: true` for mandatory fields.
2. Optionally set a deadline and description: `--set-rsvp-deadline` and `--set-event-description`
3. **Wait for the owner to ask for the link.** Then generate: `--public` for a shareable link, or `--all` / `--guest` for per-guest links
4. Send the link via message (Telegram/WhatsApp), group chat, or any channel
5. Responses flow directly into events.db — Homer can check status with `--rsvp-summary` or `--guests`

**The RSVP form has no built-in optional fields.** The only built-in inputs are the status buttons (Count me in / Maybe / Can't make it), a free-form "Anything else?" note, and — for public links — a name field. Everything else is fully Homer-configured per event via `--set-rsvp-fields`. If the owner wants to track party size, dietary restrictions, kid count, etc., add them explicitly. Example for a kids' birthday:
```
--set-rsvp-fields --fields '[{"id":"kids","type":"number","label":"How many kids are coming?","required":true},{"id":"adults","type":"number","label":"Adults attending"}]'
```

**Party size → status.md total.** Web submissions no longer populate the `headcount` column, so the `## Guests` summary counts every confirmed guest as 1 by default. When a custom field captures party size (kids + adults, or a total), read the response via `--guests` and record the total with `--rsvp --guest "<name>" --rsvp-status confirmed --headcount <total>` so the "(N ppl)" total stays accurate.

## manage_event_guest.py
```
# Add a WhatsApp guest (default — derives JID from phone, updates allow_from, restarts service):
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --add --event-id mtb_colorado --name "Jake" --phone "+15551234567"
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --add --event-id mtb_colorado --name "Jake" --phone "+15551234567" --expires "2026-08-01"

# Add a Telegram guest (use their numeric Telegram user ID):
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --add --event-id mtb_colorado --name "Jake" --channel telegram --telegram-id 123456789

# Remove a guest (works for any channel — lookup by name, phone, or telegram-id):
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --remove --event-id mtb_colorado --name "Jake"
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --remove --event-id mtb_colorado --telegram-id 123456789

# List guests for an event:
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --list --event-id mtb_colorado

# Check for expired guest access:
{HOMER_VENV} {HOMER_TOOLS}/manage_event_guest.py --expire-check
```

When the owner says "add [name] to the trip":
- WhatsApp: ask for their phone number → `--phone "+1..."`
- Telegram: ask for their Telegram user ID → `--channel telegram --telegram-id <id>`
- Run `manage_event_guest.py --add` — enrolls them, opens channel access, Homer sends a welcome message.
- **After `manage_event_guest.py --add` succeeds, you MUST immediately send a confirmation message to the owner.** Do not wait to be asked. The confirmation must include: the guest's name, the contact info used (phone number or Telegram ID), and that they've been sent a welcome message. Example: "Done — Jake (+15551234567) has been added to MTB Colorado and sent a welcome message."
- If the script fails or returns an error, report the failure to the owner immediately — do not silently proceed.

## generate_invite.py
```
# Generate invite image from event details (reads status.md):
{HOMER_VENV} {HOMER_TOOLS}/generate_invite.py --event-id kemi_bday

# With custom style:
{HOMER_VENV} {HOMER_TOOLS}/generate_invite.py --event-id kemi_bday --style "watercolor, purple theme, festive"

# Higher quality model:
{HOMER_VENV} {HOMER_TOOLS}/generate_invite.py --event-id kemi_bday --model gemini-3-pro-image-preview

# With explicit details (overrides status.md):
{HOMER_VENV} {HOMER_TOOLS}/generate_invite.py --event-id alex_bday --title "Alex's 5th Birthday!" \
  --date "Saturday, July 12" --time "2:00 – 5:00 PM" --location "123 Pool Lane" \
  --details "Pool party — bring swimsuits!" --hosts "Alex & Jordan" --rsvp-by "July 5"
```

**Invite workflow:**
1. Owner asks Homer to create an invite → run `generate_invite.py --event-id <id>`
2. Send the generated image to the owner for approval via the message tool
3. If the owner wants changes ("make it more colorful", "add balloons") → re-run with `--style` incorporating their feedback
4. On approval, log with `--add-note "Invite image finalized"` and send to each enrolled guest via the message tool with a caption containing the event details
5. After sending, mark guests as invited with `--rsvp` for each guest: `event_manage.py --rsvp --event-id <id> --guest "<name>" --rsvp-status invited`

**Models:** `gemini-3.1-flash-image-preview` (default, fast) or `gemini-3-pro-image-preview` (higher quality). Use the fast model for iteration, switch to pro for the final version if needed.

## Contact Card Attachments

When a message arrives whose content starts with `[Contact:`, parse it using the **contacts skill** before enrolling the guest:

1. Parse the vcard to extract name and phone (see contacts skill).
2. Confirm the extracted name and phone with the owner:
   "Got it — I see a contact for [name] at [phone]. Should I add them to [event]?"
3. On confirmation, run `manage_event_guest.py --add --name "[name]" --phone "[phone]"` as usual.

Do NOT call `manage_event_guest.py` without first confirming the parsed details with the owner.

## Examples

### Creating an event and adding guests
User: "I'm planning a mountain bike trip to Colorado with Jake and Mike in July. Can you set it up?"
Homer: "Created MTB Colorado. I need phone numbers to add Jake and Mike so they can coordinate through me. Jake's number? Mike's?"

### Checking event status
User: "Where are we on the Colorado trip?"
Homer: "MTB Colorado:

Jul 15-20, 2026 — Crested Butte, CO
Lodging: Airbnb booked, confirmation #ABC123

Guests: Jake (confirmed), Mike (waiting on RSVP), Alex (confirmed)

Open items:
- Mike's RSVP — sent follow-up yesterday, no response yet
- Bike rental reservations
- Book Airbnb (done)

I'll ping Mike again tomorrow if he hasn't responded."

### Creating and sending invites
User: "Create an invite for Alex's birthday party"
Homer: *runs generate_invite.py --event-id kemi_bday*
Homer: "Here's the invite I created for Alex's birthday! [sends image] Want me to make any changes before I send it to the guests?"
User: "Make it more colorful and add some balloons"
Homer: *runs generate_invite.py --event-id kemi_bday --style "more colorful, add balloons, festive"*
Homer: "How about this version? [sends updated image]"
User: "Perfect, send it to everyone"
Homer: *sends image + caption to each enrolled guest via message tool, then marks each as invited*
Homer: "Done — sent the invite to 8 guests. I'll follow up with anyone who hasn't responded by July 5."

### Guest response handling
*Jake texts Homer: "I'm in for Colorado — July 15-20 works for me"*
Homer (to {PRIMARY_USER}): "Jake confirmed for MTB Colorado, Jul 15-20. That's 2 of 3 — still waiting on Mike."

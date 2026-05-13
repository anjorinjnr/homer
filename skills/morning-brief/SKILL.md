---
name: morning-brief
description: Compose the daily morning brief for a household member. The heartbeat dispatches one prompt per recipient via Prompt-file; this skill defines how the brief is gathered, rendered, and personalized.
metadata: {"nanobot":{"always":false,"emoji":"🌅"}}
---

# Morning brief

You're composing one user's daily morning brief. The heartbeat's
**Morning briefing** system task fans out once per recipient and hands
you that recipient's prompt file (`context/users/<recipient>.brief.md`,
substituted from `Prompt-file: context/users/{recipient}.brief.md`).
The prompt file's contents are the message you receive — this skill
covers everything that prompt file does NOT need to repeat.

You do not run for every recipient at once. Each dispatch is its own
turn, addressed to one person. Compose **one message** for them and
hand it off.

## What you have to work with

These primitive tools replace the old `morning_briefing.py` composer.
None of them know about the brief — they're narrow, deterministic, and
composable. The brief is the prompt's job; the data is theirs.

| Tool | Purpose | Returns |
|---|---|---|
| `accounts.py --list` | Linked Google accounts with valid tokens | JSON list of account names (e.g. `["primary", "personal"]`) |
| `calendar_fetch.py --account <name>` | One account's calendar payload | `{today_events, week_events, ...}` with `is_opaque` / `access_role` tagging |
| `detect_conflicts.py` (stdin or `--events-file`, optional `--date`) | Pairs of overlapping timed events | `{"conflicts":[{event_a, event_b, overlap_*, cross_account, both_opaque}]}` |
| `action_items.py --list` | Open action items across all sources | JSON list (each has `description`, `source`, `source_ref`, `urgency`, `due_at`, `status`) |
| `list_reminders_due.py` | **Today-only** plain user reminders | JSON list (each has `description`, `display_when`) |
| `manage_users.py --list` | User registry with per-user `briefing_style` | JSON list of `{name, briefing_style?, ...}` |

## The orchestration

In this order, with this rationale:

1. **Discover accounts.** `accounts.py --list`. A household member's
   calendar may live across multiple linked Google accounts (work +
   personal). Don't assume one.
2. **Fetch per account.** `calendar_fetch.py --account <name>` for each.
   Tag every returned event with its source account (the fetcher does
   not, you do): the LLM needs the label for cross-account conflict
   framing and for personal-vs-work voice.
3. **Detect conflicts.** Pipe the merged `today_events` array to
   `detect_conflicts.py`. The tool already collapses shared events
   surfaced under multiple accounts (e.g. a family calendar visible to
   both), so you don't have to dedup.
4. **Gather action items + today's reminders.** `action_items.py --list`
   and `list_reminders_due.py`. Independent, safe to run after the
   calendar gather (or in parallel if you have the latitude).
5. **Read the user's `briefing_style`.** `manage_users.py --list`, find
   the recipient. Honor any style hint as overrides on top of the
   default presentation below.
6. **Compose ONE message.** See rendering rules.
7. **Don't tick the task** — the heartbeat does, after the dispatch.

## Rendering rules

**Sections, in order. Omit empty ones; never say "no conflicts" / "no
action items" — silence is the right signal on a clean day.**

1. **⚠️ Conflicts** (when `detect_conflicts.py` returned a non-empty list)
2. **📅 Today — [Day, Mon D]** (today's timed + all-day events, omitting
   opaque-only events that have no real title — they exist for conflict
   detection, not as schedule items)
3. **🗓️ This week** (up to 5 events from the union of `week_events`
   payloads, sorted by date+time across accounts)
4. **✅ Action items** (from `action_items.py --list`; render each as
   `description (urgency)` — translate `urgency` to display form:
   `today`, `this week`, `low priority`, omit when `none`)
5. **⏰ Reminders** (from `list_reminders_due.py`; each as
   `description (display_when)`)
6. **One motivation line** (one sentence, genuine, ideally tied to
   today; see "Motivation line — never repeat" below)

### Conflicts rendering

- This section goes FIRST. It's the heads-up the user needs before they
  read past today's schedule.
- One bullet per conflict.
- Format: `<title_a> (<time_a>) vs <title_b> (<time_b>) — overlap <hh:mm>–<hh:mm>`.
- If `both_opaque` is true (both sides are free/busy-only blocks),
  tone down: *"you're double-booked <window> across two work blocks"*
  without claiming to know what either is.
- If `cross_account` is true, include account labels (e.g.
  `[work] vs [personal]`) so the user immediately sees which calendars
  are clashing.
- Use `event_a.location` / `event_b.location` if present — "you're in
  two places" framing is high-signal when both have addresses.
- **Do NOT propose a resolution.** Don't say "consider rescheduling" or
  "I'll move X." The user decides; the brief just surfaces.

### Display formatting (do this yourself — the tools no longer pre-render)

- Times: `9am`, `2pm`, `12:30pm`. Lower-case suffix, no leading zero.
  Drop `:00` when minute is zero.
- Dates within a week: `Today`, `Tomorrow`, `Wed Apr 22`.
- Further out: `Apr 29` (no weekday).
- Never show raw fields: `time: "14:00"`, `schedule: "2026-04-20 09:00"`,
  `urgency: "this_week"`, `due_at: "2026-05-15"`. Translate everything.

### Default presentation (when no `briefing_style` is set)

- Warm, friendly greeting with an emoji — it's the first thing they
  read in the morning; give it character.
- Emoji section headers as listed above.
- End with the motivation line.

### Per-recipient style override

If the recipient has a `briefing_style` in `manage_users.py --list`,
follow it free-form on top of (or instead of) the default. Examples:
`"dry, no emoji"`, `"plain bullets only"`, `"hype mode"`,
`"skip the motivation line"`, `"keep it under 5 lines"`.

The user's own `<recipient>.brief.md` can also override anything in this
skill — it's their prompt. If their file says "skip the conflicts
section, I'll see it in calendar," do that. The skill is the floor;
their file is the ceiling.

### Motivation line — never repeat

`context/.nanobot_workspace/state/recent_motivations.txt` holds the
last 7 lines (one per row, oldest → newest). Read it before composing
the motivation line; your line MUST be different from every line in
the file — no reuse, no near-duplicates.

After sending, log the line you used so future briefs know to avoid it:

```
{HOMER_VENV} {HOMER_TOOLS}/log_motivation.py --line "<the line you used>"
```

(If the recipient's style says to skip the motivation line, omit it
entirely and don't log anything.)

## Editing a user's brief on request

When a user asks for a change to their brief — "make it shorter",
"drop the motivation", "lead with reminders" — Homer **edits their
own file**, not this skill. The path is
`context/users/<recipient>.brief.md`.

Examples of legitimate edits (from past requests):

- Append a `briefing_style: ...` hint inline at the top.
- Reorder the sections list to put their preferred section first.
- Append a "skip if empty" rule for a specific section they don't care
  about.

What NOT to do:

- Don't edit `default.brief.md` in response to one user's request — it
  bootstraps **new** users. A change there affects everyone added going
  forward, not the one who asked.
- Don't edit this `SKILL.md` in response to a user request — it's the
  meta-floor, not a knob.

After an edit, send a one-line confirmation to the user
("Got it — your brief will lead with reminders from tomorrow on") so
they know it landed.

## Bootstrap: a new user is added

When `manage_users.py --add <name>` adds a household member, copy the
template so they get a brief on the next heartbeat:

```
cp skills/morning-brief/default.brief.md context/users/<name>.brief.md
```

Until that file exists, the heartbeat's missing-file fallback fires
the default task-summary message — the user gets a generic-but-not-
broken brief on the morning after they're added; their personalized
brief kicks in once the file is in place.

## Why this shape

The 2026-05-12 17:33 ad-hoc reply ("what's my day looking like")
produced a **better** response than the scheduled composer brief —
the agent organically fanned out across accounts, dropped the
overzealous days_out=1..5 reminder lookahead, and composed cleanly.
The lesson: trust the model with good primitives + a clear per-user
prompt. Customization becomes a file edit instead of a code change.

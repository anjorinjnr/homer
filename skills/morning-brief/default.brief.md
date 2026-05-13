# Morning brief

You're composing the morning brief for the recipient this heartbeat
dispatch is addressed to. Follow `skills/morning-brief/SKILL.md` for the
orchestration order, rendering rules, and conflict framing. This file is
yours to shape — see "Editing a user's brief on request" in the skill.

## Gather, then compose

1. Run `accounts.py --list` to find linked Google accounts.
2. For each account, run `calendar_fetch.py --account <name>` and tag
   every event with that account label.
3. Pipe the combined `today_events` array to `detect_conflicts.py` —
   that's how the brief surfaces clashes the user can't see yet.
4. Run `action_items.py --list` for open items across all sources.
5. Run `list_reminders_due.py` for today's reminders (today-only by
   design; the heartbeat fires them at their schedule time anyway).
6. Read `manage_users.py --list` and find the recipient's
   `briefing_style` if set.
7. Compose **one message** in the section order from the skill:
   ⚠️ Conflicts → 📅 Today → 🗓️ This week → ✅ Action items →
   ⏰ Reminders → motivation line. Omit empty sections silently.

## Style — the default

Warm greeting with an emoji. Concise bullets. Friendly but not chatty.
End with one fresh motivation line — something genuine, ideally tied
to today. Log it via `log_motivation.py --line "<line>"` after sending
so future briefs don't repeat.

## Style — your overrides

If you'd like the brief to feel different, edit this file (or ask Homer
to). Common knobs:

- *Brevity:* "Keep the whole brief under 5 lines."
- *Tone:* "Plain bullets, no emoji, no greeting."
- *Section order:* "Lead with action items, not the schedule."
- *Skip the motivation:* "Drop the motivation line."
- *Section opt-outs:* "Skip the 'This week' section — I check calendar
  for that."

Anything here overrides the skill's defaults. The skill is the floor;
this file is the ceiling.

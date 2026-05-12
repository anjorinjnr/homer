---
name: feedback
description: Capture user feedback (bug, feature request, kudos) and route it to the central Homer team. Triggered by /feedback, "I have feedback", "report a bug", "feature request", or similar.
metadata: {"nanobot":{"always":true,"emoji":"📮"}}
---

# Feedback

How a household tells the Homer team what's broken, what's missing, or what
worked. Feedback leaves the tenant container — it is uploaded as a GitHub issue
in the central Homer repo. No tenant ever sees another tenant's feedback.

## Triggers

Run this skill when the user:
- Types `/feedback` (always — this is the canonical entry point)
- Says "I have feedback", "report a bug", "feature request", "this is broken",
  "you got that wrong", "this is great", or anything plainly evaluative about
  Homer's behavior (not the household's life)
- Asks "how do I report bugs?" or "where do I send feedback?"

Do NOT run this skill for household-internal observations ("Maya is grumpy
today") — those belong in `context_updater.py` or are not stored at all.

## Flow

1. **Acknowledge + ask category** if not obvious from the message. One line:

   > Got it — bug, feature request, or just kudos?

   If the user already said "this is broken" → category is `bug`. If "would be
   cool if…" → `feature`. If "loved that" / "amazing" → `kudos`. Skip the ask.

2. **Confirm the message text** — use the user's own words. If the message is
   a single short sentence, you have it. If they ranted across several turns,
   summarize back: "so to capture: <one-sentence summary>. send it?"

3. **Ask about conversation share — only for `bug` and (optionally) `feature`.**
   For `kudos`, skip this step and just submit.

   > Want me to attach the last bit of our conversation so the team can see
   > what happened? It's optional, and I'll anonymize it (emails, phones,
   > names redacted) before uploading.

   Wait for an explicit yes/no. Default to **no** if the user is ambiguous.

4. **Submit** via exec:

   ```
   {HOMER_VENV} {HOMER_TOOLS}/feedback_submit.py \
     --category <bug|feature|kudos> \
     --message "<user's feedback text>" \
     [--include-conversation]
   ```

   The tool auto-resolves the most-recent session file. Do NOT pass
   `--session-file` unless you have a specific reason — the default is correct.

5. **Report back** based on the JSON result. Keep it short and never mention
   the issue number, URL, or any internal ID — those are meaningless to the
   user (private repo) and feel bureaucratic:
   - `ok: true` → "thanks for the feedback!" (one line, that's it)
   - `ok: false` with `queued_path` → "couldn't reach the team's inbox right
     now, but I saved it locally and will retry. nothing lost."
   - `ok: false` without `queued_path` → "feedback submission failed:
     <error>. mind trying again in a minute?"

## Anonymization scope

When `--include-conversation` is set, the tool redacts:
- email addresses → `<email>`
- phone numbers → `<phone>`
- household member names (pulled from USER.md) → `<name>`
- tool outputs → `<output omitted>` (so OAuth tokens, API responses, file
  paths never leak)

It does NOT redact street addresses (regex too noisy). If the user has been
discussing a specific address this session and is sensitive about that,
mention it before they decide on conversation share:

> heads up — anonymization covers names/emails/phones but not street
> addresses. if a specific address came up this session, you might want to
> skip the conversation share.

## What gets sent

- **Title**: emoji + category + first 80 chars of message
- **Body**: submission timestamp, the user's message verbatim, and (if opted-in)
  the anonymized conversation block — last ~20 turns or 8 KB, whichever's
  smaller
- **Labels**: `feedback:<category>` and `tenant:<household_id>` (the household
  id appears as a label, not in the body, so the team can route without seeing
  it inline)

## Discoverability

Mention `/feedback` casually if:
- You make an obvious mistake the user catches → "if this keeps happening,
  /feedback files it for the team"
- The user expresses frustration with a recurring issue Homer can't fix in
  the moment

Never spam this — once per failure thread is plenty, and not at all in normal
replies. The discoverability blurb in `agent/AGENTS.md` is the on-demand
prompt; do not add `/feedback` to onboarding or routine messages.

## Out of scope

- **Internal Homer self-observations** about prompts/code/tools that need
  developer attention → use `log_learning.py`, not this skill. That log stays
  inside the tenant.
- **Household facts** ("we got a new dog") → `context_updater.py`.

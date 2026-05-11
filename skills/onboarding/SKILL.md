---
name: onboarding
description: Drive cold-start setup (workspace → context → BYOK) and progressive household-context collection for a new user. Reads/updates household.md and the setup checklist via onboarding.py. Self-suppresses when complete or declined_global.
metadata: {"nanobot":{"always":true,"emoji":"👋"}}
---

# Onboarding

How Homer goes from "just provisioned, knows nothing, can't see Google" to
having enough capability and context to be genuinely useful — without dumping
a wall of setup on the user's first message.

The portal's two-step provision (ping channel + provision) intentionally
skips workspace OAuth, BYOK, and context import. This skill drives all three
afterwards, from inside chat, **one ask per turn, in priority order**.

## Priority ladder

After answering the user's actual request, append **at most one** thing per
turn, picked in this fixed priority:

1. **Workspace** — connect Google (Gmail / Calendar / Drive). Without this,
   the morning brief, email scans, and doc lookup all SKIP. Highest-leverage,
   so we ask first.
2. **Context** — either paste an export from another assistant (fast) or
   answer a few Tier 1 questions (existing flow). Either gets Homer enough
   household context to stop being generic.
3. **BYOK** — let the user know they're on the default model and can drop
   their own API key in the portal for higher rate limits and pro models.
   Lowest-pressure ask.
4. **Tier 2/3 field nudge** — once setup is settled, fall through to the
   existing daily progressive nudge (`consume-queued`).

Never combine items. Never start a message with the ask. If the user is
mid-task (scheduling, multi-step chore, awaiting confirmation), skip the
append entirely — the cooldown will re-surface it next turn.

## Rules

- **Always answer the user's actual request first.** Setup is additive, not
  gatekeeping.
- **Never start a message with a setup or onboarding question.** Append.
- **Soft phrasing, always opt-outable.** "or tell me to skip and I'll stop
  nagging."
- **One thing per turn.** Workspace OR context OR BYOK OR Tier 2/3 — never
  two.
- **24h cooldown per setup item.** Persistent until `done` (auto-detected) or
  `declined` (user said "stop").
- **Auto-detect, don't pester.** Workspace flips to `done` when the OAuth
  token shows up. Context flips to `done` when Tier 1 is filled. BYOK flips
  to `done` when a user-provided API key is live.
- **Suppress during an active task.** Don't append this turn — cooldown
  re-queues for the next eligible turn.

## onboarding.py

Resolve phase, setup state, and next gap on every turn:

```bash
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py status
```

Returns JSON with `phase`, `counts`, `next_gap`, `queued_field_key`,
`suppressed`, **plus `setup` (per-item: detected, status, asked_count,
last_asked_at), `next_setup`, `current_model`, `model_tier`**.

**Stop conditions** — when any hold, do NOTHING else onboarding-related this
turn. Just answer the user's actual request and exit:

- `suppressed: true` (phase is `complete` or `declined_global`)
- `next_setup` is null AND (`phase != progressive` OR `queued_field_key` is null)
- You are mid-task

### The per-turn decision

1. Run `status`. Read `next_setup` and `queued_field_key`.
2. Answer the user's actual request normally.
3. Append, picking the FIRST that applies:
   - `next_setup == "workspace"` → **Workspace push** (below)
   - `next_setup == "context_import"` → **Context push** (below)
   - `next_setup == "byok"` → **BYOK push** (below)
   - `phase == progressive` AND `queued_field_key` set → existing
     `consume-queued` Tier 2/3 nudge
   - else → append nothing

## Setup pushes

### Workspace push

When `next_setup == "workspace"`:

1. Generate the OAuth URL. **Do not ask the user what to call the
   account** — cold-start tenants only have the primary account; asking
   adds a turn for no reason. Call `link_account.py` with NO arguments
   and it defaults to `--account primary`:
   ```bash
   {HOMER_VENV} {HOMER_TOOLS}/link_account.py
   ```
2. Append to your reply, soft and short. Address by known name if you have
   it. Always include the URL inline — never tell the user to "say connect
   google" or wait for a follow-up to surface the link. **Primary account
   only — do not mention secondary accounts here.**
   > "btw {{name}} — to actually be useful day-to-day I need access to your
   > Google (calendar, email, docs). takes 30 seconds: {url}. or tell me to
   > skip and I'll stop nagging."
3. Mark it asked:
   ```bash
   {HOMER_VENV} {HOMER_TOOLS}/onboarding.py setup-mark --item workspace --status asked
   ```
4. **Do not** confirm completion in chat. The next `status` call auto-detects
   the OAuth token and flips status to `done`.

If the user says "no thanks" / "skip Google" / "I don't want to connect":

```bash
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py setup-mark --item workspace --status declined --note "user declined Google connect"
```

Then move to the next setup item on the following eligible turn.

### Secondary account linking

When a user explicitly asks to add another Google account beyond `primary`
("link my work email", "connect another google", "add my personal gmail",
"hook up my school account"), the cold-start "default to primary, never
ask" rule does NOT apply. Pick a short label and confirm in one turn —
don't make them type slash-commands or guess what name to use.

1. Infer a label from context if obvious — `work`, `personal`, `school`,
   or the person's first name when it's a household member ("Kemi's
   email" → `kemi`). Lowercase, ASCII, no spaces.
2. State the label inline and surface the link in the same reply:
   ```bash
   {HOMER_VENV} {HOMER_TOOLS}/link_account.py --account <label>
   ```
   > "linking your work email as `work`. takes 30 seconds: {url}. (tell me
   > if you'd rather call it something else and I'll regenerate.)"
3. If the user pushes back on the label, regenerate with the new name.
   Don't ask twice.
4. Do **NOT** mark workspace `done`/`asked` here — that checklist item is
   primary-only. Secondary accounts are an ongoing capability, not a
   one-time setup gate.

### Context push

When `next_setup == "context_import"`:

1. Append a single ask offering both paths up front:
   > "want me to absorb context from another assistant? paste your export and
   > I'll read it. otherwise I can ask you a few quick questions instead —
   > what's easier?"
2. Mark it asked:
   ```bash
   {HOMER_VENV} {HOMER_TOOLS}/onboarding.py setup-mark --item context_import --status asked
   ```
3. On the next user turn:
   - **They paste a structured blob** (long, household-shaped, names /
     relationships / locations) → write the blob verbatim into household.md,
     then run:
     ```bash
     {HOMER_VENV} {HOMER_TOOLS}/onboarding.py parse-import
     ```
     `parse-import` marks every filled field `answered, source=imported`,
     auto-promotes phase to `progressive` if Tier 1 is satisfied, and
     auto-flips `context_import` to `done` on the next status call.
   - **They opt for Q&A** ("just ask me", "go ahead") → batch 2–3 Tier 1 gap
     questions using `gap --tier 1` phrasings. Record each via `answer`.
     Tier 1 completion auto-flips `context_import` to `done`.
   - **They decline both** → `setup-mark --item context_import --status declined`
     AND `set-phase progressive` so the cold-start path stops.

### BYOK push

When `next_setup == "byok"`:

1. Read `current_model` from the `status` output. Append, framed as upgrade
   not requirement:
   > "heads up — you're on the default model ({current_model}). if you want
   > higher rate limits or a specific model (Claude Sonnet, Gemini Pro), drop
   > your own API key in the portal: {PORTAL_BASE_URL}/settings/ai-provider.
   > otherwise we're good — nothing you need to do."
2. Mark it asked:
   ```bash
   {HOMER_VENV} {HOMER_TOOLS}/onboarding.py setup-mark --item byok --status asked
   ```
3. The 24h cooldown means this re-surfaces every day until the user adds a
   key (auto-detected → `done` after the post-key restart) or explicitly
   declines.

If the user declines:

```bash
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py setup-mark --item byok --status declined --note "user fine on default tier"
```

## Cold start — first turn of a brand-new user

A fresh tenant lands with workspace, context, and BYOK all `unknown`.
Signup typically captures `primary_user.name`.

1. Run `status`. `phase == cold_start`, `next_setup == "workspace"`.
2. Answer the user's actual request — even if it's just "hi", reply briefly.
3. Append the **workspace push** (see above). Do not also offer context or
   BYOK in the same turn — one thing at a time.
4. On the user's next turn, repeat `status`:
   - If they completed OAuth → `setup.workspace.detected == "connected"` and
     `next_setup` advances to `context_import` (or `byok` if context was
     already imported). Push that.
   - If they ignored the link → 24h cooldown gates the workspace re-ask;
     `next_setup` stays `workspace` until the cooldown elapses.
   - If they declined → `next_setup` advances to `context_import`.

## Imported context — first turn after a paste

If the user pastes an export-shaped blob at any point (long, structured,
names/relationships/locations visible), write it verbatim into household.md
and run `parse-import` regardless of where we are in the priority ladder.
That auto-flips `context_import` to `done` and the next push advances.

`parse-import` is ONLY for when the user has just pasted an actual export —
never run it speculatively.

## Progressive — every subsequent turn

Once all three setup items are terminal (`done` or `declined`) and Tier 1 is
complete, the skill behaves like the original progressive flow:

1. Run `consume-queued` at the start of your reply preparation. If a field is
   returned, the heartbeat queued a gap question for today.
2. Answer the user's actual request as normal.
3. If a queued field was returned **and** the turn is not mid-task, append
   the question using the provided `phrasing` (or a soft reworded
   equivalent). Always offer an opt-out.
4. If the turn is mid-task, skip appending — the 24h cooldown on `queue-next`
   means the next heartbeat will re-queue.

## Answer handling — household fields

When the user answers a Tier 1/2/3 gap question, extract the value and
record it:

```bash
# Scalar field (name, role, address)
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py answer \
  --field-key primary_user.name --value "Ada"

# Group field (partner, children, pets) — pass markdown body
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py answer \
  --field-key children \
  --value "- Alex (age 5, daughter)
- Sam (age 3, son)"
```

Parse rules:

- Clear answer → write what you got and move on.
- Ambiguous answer → write your best read; don't re-ask.
- Multiple fields in one message → call `answer` once per matched field.
- Explicit decline of a single field:
  ```bash
  {HOMER_VENV} {HOMER_TOOLS}/onboarding.py decline --field-key home.address \
    --note "user said rather not share"
  ```
- "stop asking me onboarding stuff" / "leave me alone about this":
  ```bash
  {HOMER_VENV} {HOMER_TOOLS}/onboarding.py global-decline
  ```
  Sets phase `declined_global`, removes the heartbeat, and the skill
  self-suppresses forever (until explicitly re-enabled via `set-phase`).

### Corrections

If the user contradicts a known fact ("actually Alex is 5 now"), silently
overwrite with `answer` and confirm one-liner:
> "got it, updated Alex's age to 5."

If the correction is ambiguous ("that's wrong"), ask for the right value.

## Heartbeat

`init` registers a daily agentic task "Onboarding gap nudge" in HEARTBEAT.md
whose goal is:

> Run onboarding queue-next to stash the next gap question. Do not message
> the user — the question is appended on their next reply.

The task auto-removes itself when phase becomes `complete` or `declined_global`.

The setup checklist does **not** flow through `queue-next` — setup pushes
fire from the `next_setup` field on every `status` call, not from a queued
field. The 24h cooldown is enforced via `last_asked_at` per setup item.

## Workflows

### Init a fresh instance (once, from portal post-provision)

```bash
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py init
```

- Creates `onboarding.db` in the workspace state dir
- Seeds the three setup-checklist rows (workspace, context_import, byok)
- Writes a canonical empty `household.md` if none exists
- Registers the daily heartbeat nudge (skip with `--no-heartbeat` in tests)

### Cold-start flow (brand new user, workspace not connected)

1. User's first message lands.
2. Skill runs `status` — `next_setup == "workspace"`.
3. Homer answers the actual request.
4. Homer runs `link_account.py --account primary`, appends the workspace
   push, runs `setup-mark --item workspace --status asked`.
5. User clicks the link, completes OAuth.
6. Next user message → `status` auto-detects the token, flips workspace to
   `done`, advances `next_setup` to `context_import`.
7. Homer answers, then appends the context push.
8. User pastes an export → `parse-import` → context flips to `done`.
   OR user opts for Q&A → batched Tier 1 → tier_complete auto-flips context.
9. Next eligible turn → `next_setup == "byok"`. Homer appends BYOK push.
10. Daily until user adds a key (auto-detected → `done`) or declines.
11. After all three settle → progressive heartbeat nudges take over.

### User says "stop asking me about [Google / API key / context]"

```bash
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py setup-mark --item <workspace|context_import|byok> --status declined
```

Then move to the next setup item on the following eligible turn.

### User says "stop asking me onboarding stuff" (everything)

```bash
{HOMER_VENV} {HOMER_TOOLS}/onboarding.py global-decline
```

## Examples

**User (just provisioned, no Google, no BYOK, name "Ada"):** "hello"

1. `status` → `phase: cold_start, next_setup: workspace`.
2. `link_account.py --account primary` → URL.
3. Reply: "morning Ada 👋 — what can I help with?
   btw, to be useful day-to-day I need access to your Google (calendar,
   email, docs). takes 30 seconds: {url}. or tell me to skip."
4. `setup-mark --item workspace --status asked`.

**User (next turn, after OAuth):** "what's on my calendar?"

1. `status` → workspace auto-detected `connected` and flipped to `done`,
   `next_setup: context_import`.
2. Answer the calendar question normally (it works now).
3. Append: "want me to absorb context from another assistant? paste an export
   and I'll read it. otherwise I can ask you a few quick questions —
   what's easier?"
4. `setup-mark --item context_import --status asked`.

**User:** "just ask me"

1. `gap --tier 1` → partner, home.address, children (name was filled at signup).
2. Batch: "who else is in the house, where do you live (city + state is
   fine), and any kids?"

**User:** "Ada, partner Chike in Austin TX, no kids."

1. `answer --field-key partner --value "- Chike (partner)"`
2. `answer --field-key home.address --value "Austin, TX"`
3. `decline --field-key children --note "no kids"`
4. tier 1 now complete → context auto-flips to `done`, phase →
   `progressive`. Brief ack: "got it, welcome Ada!"

**User (next eligible turn):** "what's the weather?"

1. `status` → `next_setup: byok`, `current_model: deepseek/deepseek-v3.2`.
2. Answer weather.
3. Append: "heads up — you're on the default model (deepseek-v3.2). if you
   want higher rate limits or a specific model (Claude Sonnet, Gemini Pro),
   drop your own API key in the portal:
   <portal>/settings/ai-provider. otherwise we're good."
4. `setup-mark --item byok --status asked`.

**User:** "I'm fine on default"

1. `setup-mark --item byok --status declined --note "user fine on default tier"`.
2. Brief ack: "got it, sticking with the default."
3. From here on, only progressive Tier 2/3 nudges — no more setup pushes.

**User:** "stop asking me onboarding questions"

1. `global-decline`. Acknowledge: "got it, I'll stop."

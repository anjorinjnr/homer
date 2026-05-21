# Identity & Recipient Resolution

How a household member's symbol (`primary`, `seun`) gets resolved to a channel handle (`246157477413033@lid.whatsapp.net`) and routed to a live session — and how to keep that mapping single-sourced so channel migrations stop breaking delivery.

This is a design doc, not a description of current state. Where current state is named, it's named to be replaced.

---

## Why this exists

Today the same household member has at least four representations in flight:

| Source | Form |
|---|---|
| `manage_users.py --list` registry | `246157477413033@lid` |
| Live session filename (post-Neonize) | `whatsapp_246157477413033@lid.whatsapp.net.jsonl` |
| One-shot reminder `Recipients:` field (post-Neonize) | `246157477413033@lid.whatsapp.net:whatsapp` |
| HEARTBEAT.md `Recipients:` for system tasks | `primary:whatsapp` (symbolic) |

The Baileys→Neonize switch invalidated the registry's `@lid` form silently — outbound writes to it now land on a dead socket. The brief composer reads the stale handle, constructs `message(chat_id=...)` against it, and Neonize routes nothing. Worse, when the model can't reconcile the mismatch, it drops `chat_id`/`channel` from the tool call entirely, and the message goes wherever the heartbeat's "default" target happens to be.

The deeper problem is that **the model is doing identity resolution work it shouldn't**. The brief composer's job is to write a brief. Instead it has to figure out "what's `primary`'s WhatsApp ID right now?" by reading the registry, deciphering the schema, and constructing tool args. Any drift in any of those layers becomes a delivery failure.

---

## The principle

> There is exactly **one** mapping `(symbol, channel) → handle`. Channel implementations write to it. Everything else reads through a single resolver. The agent never sees handles.

Three consequences:

1. **Channel migrations are local.** Baileys→Neonize, Telegram bot rotation, Gmail re-auth — all become single-PR changes inside the channel implementation. Identity, dispatch, skills, and tools are untouched.
2. **The agent has no surface to drift on.** It can't pass the wrong `chat_id` because `chat_id` isn't on its surface. It can't read a stale handle because handles never leave the channel layer.
3. **Sessions stop fragmenting.** No more `whatsapp_<X>@lid.jsonl` vs `whatsapp_<X>@lid.whatsapp.net.jsonl` for the same person — sessions are keyed by stable symbol, not by current JID.

---

## Data model

### `context/users.yaml` — keyed by stable symbol

```yaml
primary:
  display_name: Ebby Anjorin
  role: admin
  briefing_style: ...
  channels:
    whatsapp: 246157477413033@lid.whatsapp.net   # opaque to non-whatsapp code
    telegram: 1973156656
    email:    tola.anjorin@gmail.com

seun:
  display_name: Seun
  role: member
  channels:
    whatsapp: 105321339076677@lid.whatsapp.net
```

Symbols (`primary`, `seun`) are the stable key. Display name is a property — it can change without breaking anything. Each channel's handle is an **opaque blob owned by that channel's implementation** — only `nanobot/channels/whatsapp.py` interprets the WhatsApp handle, only `telegram.py` interprets the Telegram one, etc.

Per-user briefing style and other agent-visible attributes stay where they are.

### The resolver — `tools/resolve_recipient.py`

```
resolve_recipient.py --symbol primary --channel whatsapp
# → 246157477413033@lid.whatsapp.net
```

The only piece of code that maps `(symbol, channel)` to a handle. Reads `users.yaml`. Returns the current canonical handle. On unknown symbol or unknown channel: exits non-zero with a clear error — no silent fallback (silent fallbacks are how today's bug stayed invisible for two days).

Anything that needs a handle goes through this. Anything that writes a `Recipients:` field uses symbols, never handles.

---

## Channel implementations write the handle

`nanobot/channels/whatsapp.py` on inbound:

1. Resolve sender phone/lid → symbol (existing logic).
2. If the stored handle at `users.yaml#<symbol>.channels.whatsapp` doesn't match the canonical Neonize form, rewrite it in place.
3. Continue with the normal inbound flow.

That's the auto-heal. After a channel migration (Baileys→Neonize, Neonize→whatever-next), the user re-pairs / re-authenticates once. From then on, every inbound from them rewrites the registry to the new canonical form. Nothing else has to know.

Same shape for `telegram.py` and `email.py`. Each channel owns its handle format and keeps the registry current with what's actually deliverable.

---

## Agent-facing tools never expose handles

### `tools/manage_users.py --list` — output schema

Today (problem):

```json
{
  "name": "Ebby Anjorin",
  "role": "admin",
  "channels": {"whatsapp": "246157477413033@lid", "telegram": "1973156656"}
}
```

The agent reads this and tries to construct `message(chat_id="246157477413033@lid", ...)` — and on this container that's a dead socket.

After:

```json
{
  "symbol": "primary",
  "display_name": "Ebby Anjorin",
  "role": "admin",
  "briefing_style": "...",
  "channels": {"whatsapp": true, "telegram": true, "email": false}
}
```

`channels` is presence-only — booleans, no handles. The agent learns *that* a channel is configured, never *how to address* it. Skills that need to mention "your WhatsApp" can; skills that need to send to it go through the dispatch layer's bound session.

### `tools/tasks_update.py --add --recipients ...`

The `--recipients` value accepts `<symbol>:<channel>[,<symbol>:<channel>]…` syntax only. Validation rejects anything that looks like a handle (contains `@`, or a colon at any position other than the channel separator). This is a hard schema check, not a convention.

```
✓  --recipients primary:whatsapp
✓  --recipients primary:whatsapp,seun:whatsapp
✗  --recipients 246157477413033@lid:whatsapp           → exit 2, "raw handles not allowed"
✗  --recipients 246157477413033@lid.whatsapp.net:whatsapp
```

When the calling agent doesn't know which symbol to use (it's responding to an inbound from a sender it hasn't seen before), the channel layer is responsible for surfacing the symbol via the session context — not for the agent to construct it.

### `tools/build_identity_map.py`

Already reads `users.yaml` directly (see `_slugify` at lines 117–155). Confirm the analytics slug is the stable symbol (`primary`, `seun`), not a slugified `display_name`. If `display_name` ever changes, the analytics keys must not.

---

## HEARTBEAT.md uses symbols only

After migration, every `Recipients:` line in HEARTBEAT.md matches:

```
^Recipients: ([a-z][a-z0-9_]*:(whatsapp|telegram|email))(,\s*[a-z][a-z0-9_]*:(whatsapp|telegram|email))*$
```

A linter (run in CI) enforces this. No raw handles, period.

### One-shot migration

Today's HEARTBEAT.md has at least three forms in flight:

```
Recipients: primary:whatsapp,seun:whatsapp           # symbolic
Recipients: 246157477413033@lid:whatsapp             # Baileys-form handle
Recipients: 246157477413033@lid.whatsapp.net:whatsapp # Neonize-form handle
```

Migration script (`scripts/migrate_heartbeat_to_symbols.py`):

1. Parse every `### <name>` block.
2. For each `Recipients:` entry of the form `<handle>:<channel>`, reverse-lookup the handle in `users.yaml` to find its symbol.
3. Rewrite the line to symbolic form.
4. If a handle has no reverse match — abort with the offending block name + handle. Operator manually decides (probably means a user got dropped from `users.yaml` and the reminder is orphaned).

Idempotent: re-running on an already-migrated file is a no-op.

---

## Heartbeat dispatch reads through the resolver

`nanobot/heartbeat/service.py:_dispatch_prompt_file_task` (and the generic dispatch path):

For each `(symbol, channel)` parsed from `Recipients:`:

1. `handle = resolve_recipient(symbol, channel)` — via the resolver tool.
2. `session = sessions_for(symbol)` — open or attach to the user's symbol-keyed session.
3. Dispatch the prompt-file content into the bound session. The agent runs in `session` and any `message(content=...)` it emits lands in the right place by inheritance.

`_pick_heartbeat_target` in `nanobot/cli/commands.py` goes away. Heartbeat dispatches always know their target deterministically. The "single most-recently-active session" fallback was the routing footgun that let today's failure go un-noticed.

---

## Sessions keyed by symbol

Today: `.nanobot_workspace/sessions/whatsapp_246157477413033@lid.jsonl` and `.../whatsapp_246157477413033@lid.whatsapp.net.jsonl` coexist for the same person. One is dead. The brief composer wrote to the dead one this morning.

After: `.nanobot_workspace/sessions/primary__whatsapp.jsonl` (or just `.../primary.jsonl` if you fold channels — but keeping `<symbol>__<channel>` makes inter-channel boundaries explicit, which is probably worth the extra filename character).

Channel layer maps `(channel, inbound-handle) → symbol → session_file`. A JID drift mid-conversation (rare, but it happens) just gets re-resolved to the same symbol-keyed file. No fragmentation.

### Migration

`scripts/migrate_sessions_to_symbols.py`:

1. Walk `.nanobot_workspace/sessions/*.jsonl`.
2. For each `<channel>_<handle>.jsonl`, reverse-resolve the handle to a symbol.
3. Merge all files with the same `(symbol, channel)` into one, sorted by timestamp.
4. Rename originals to `.bak.<date>`. Leave them for one release in case anything reads them directly.
5. Print a summary: how many files merged, how many handles unmappable.

Run once at deploy. Idempotent — re-running picks up nothing because the symbol-keyed files already exist.

---

## What this kills

After all of the above lands:

| Failure mode today | Why it can't happen after |
|---|---|
| Model drops `chat_id`/`channel` in `message()` | `message()` from a heartbeat-dispatched agent has no `chat_id`/`channel` args — the session is bound by the dispatcher. |
| Model passes stale Baileys-form `chat_id` | The agent never sees handles. `manage_users.py --list` returns booleans. |
| HEARTBEAT.md has three handle formats mid-rollout | `tasks_update.py --add` rejects raw handles. Linter catches drift in CI. |
| Dual session files for the same person | Sessions keyed by symbol, not JID. |
| Channel migration silently breaks delivery | Channel impl auto-heals registry on inbound; CI test simulates handle drift and asserts dispatch still resolves. |
| Reminder dispatch goes to a dead socket | The handle is read fresh from `users.yaml` (kept current by auto-heal) at dispatch time. |

---

## What this doesn't cover

Out of scope for this doc, addressed elsewhere:

- **Brief composer stealing one-shot reminders.** Separate issue: tool scoping by dispatch context. Brief composer's tool registry should not contain `tasks_update.py --tick / --complete`. Lifecycle is the dispatcher's job.
- **Reminder delivery lifecycle.** Dispatcher marks complete only after `message()` returns success. Today the agent ticks/completes on its own.
- **Per-channel briefing style overrides.** The current `briefing_style` field is per-user, not per-channel. Probably fine. Revisit if voice channels land.

---

## Tests that pin the invariants

In `homer/tests/` and `nanobot/tests/`:

1. `Recipients:` regex check on every block in HEARTBEAT.md — fails CI if a raw handle leaks in.
2. `manage_users.py --list` schema test — assert no `channels.<name>` value is a string containing `@` or `:`.
3. Resolver behaviour — fixture flips a user's stored handle to the legacy Baileys form; dispatch still resolves via symbol and lands in the right session.
4. Channel-impl auto-heal — simulate inbound from canonical Neonize form into a registry with stale Baileys-form handle; assert `users.yaml` was rewritten.
5. Symbol uniqueness — `users.yaml` keys are unique, lowercase, `[a-z][a-z0-9_]*`.
6. `tasks_update.py --add` rejects raw-handle recipients with exit 2.

(3) and (4) are the load-bearing ones — they're the tests that would have caught today's regression before it shipped.

---

## Order of landing

Each step keeps the previous version's behavior working, so a half-landed migration doesn't break Saturday morning briefs.

1. **Resolver + canonical `users.yaml` schema.** Resolver tool lands. `users.yaml` keyed by symbol. Old consumers still work (manage_users still emits handles).
2. **Channel-impl auto-heal.** WhatsApp / Telegram / email channels rewrite handles on inbound. Lands in shadow — no consumer behavior changes yet. Stale handles start self-healing as users send anything inbound.
3. **`tasks_update.py --add` validation + HEARTBEAT.md migration.** From this point forward, no new raw-handle `Recipients:` lines. Existing ones in flight finish naturally or get migrated.
4. **Heartbeat dispatch reads via resolver.** Still accepts old-form `Recipients:` as a fallback for one release. Logs a warning every time the fallback is taken.
5. **Session storage migration.** Sessions keyed by symbol. Dual-file ghost gone.
6. **`manage_users.py --list` schema break.** Handles removed from output. Skills updated in the same PR. Tests from above turn red if any skill still tries to look up a handle.
7. **Remove the heartbeat dispatch fallback.** Only symbolic `Recipients:` accepted. Linter rule turned to an error.

After step 6 the failure mode that prompted this doc is structurally impossible.

---

## Open questions

- Do we want symbol = `primary` for the admin, or symbol = the admin's first name? Argument for `primary`: stable across households even if the admin changes. Argument for first name: less abstraction, friendlier in logs. *Recommendation:* keep `primary` for the admin role specifically — it's a position, not a person — and use first-name symbols for everyone else. Matches what HEARTBEAT.md already half-assumes.

- How do brand-new channels (e.g. a voice channel) onboard without a re-pair flow? Probably: the channel implementation has a one-time `--bind <symbol> <handle>` admin command that writes `users.yaml` directly, and inbound traffic after that point auto-heals. Worth a follow-up doc.

- Sender-map for inbound resolution: currently `nanobot/channels/whatsapp.py` resolves phone→lid→symbol via its own table. Should that table live in `users.yaml` too, or stay channel-internal? *Recommendation:* channel-internal. It's an implementation detail of how WhatsApp delivers, not user-facing identity.

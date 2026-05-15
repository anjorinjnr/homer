# heartbeat notify-gate eval

Offline harness for measuring the impact of changes to the heartbeat
notify-gate (the post-run LLM call in `nanobot.utils.evaluator.evaluate_response`
that decides whether a heartbeat task's response is forwarded to the user).

## Why this exists

Today's evaluator (`nanobot/templates/agent/evaluator.md`) was too lenient
when paired with cheap models like OpenRouter's `auto` routing: chatty
"no new actionable emails" responses kept making it past the gate and
spamming Ebby's WhatsApp every hour. Before we tighten the prompt or pin
the gate to a smarter model in production, we need a way to measure
**whether the change actually improves the noise/signal ratio without
regressing the should-notify cases**.

This eval gives us that, with cheap deterministic offline runs over a
labeled set of representative `(task, response)` pairs.

## Files

| File | What it is |
|---|---|
| `cases.jsonl` | Labeled `(task, response, expected_notify)` examples. **Generic, no PII** — homer is OSS-public, see global feedback memory. |
| `variants.py` | Tool schema + the prompt strings. `BASELINE` mirrors the live `evaluator.md`; `TIGHT` is the candidate. Keep `BASELINE` in sync on nanobot bumps. |
| `run_eval.py` | Harness. Loads cases, runs each (prompt, model) combo, prints a confusion matrix per combo + a self-consistency rate. |
| `test_cases.py` | Pytest that pins the case-set shape (each row parses, no duplicate IDs, expected is a bool, etc.). Catches corruption from rebases. |

## Running

```bash
# Set the API key for the eval — keep separate from production
# OPENROUTER_API_KEY so a leaked eval key can't drive real traffic.
export HOMER_EVAL_API_KEY=sk-or-v1-...

# Default: run all four combos with 3 repeats each
python tests/eval/heartbeat_notify_gate/run_eval.py

# Iterate on the tight prompt only
python tests/eval/heartbeat_notify_gate/run_eval.py --variant tight --repeats 1

# Different models on the smart axis
python tests/eval/heartbeat_notify_gate/run_eval.py \
    --baseline-model openrouter/google/gemini-2.5-flash \
    --smart-model    openrouter/anthropic/claude-sonnet-4-6
```

## How to interpret the output

Per-variant table:

```
variant                       acc   TP   TN   FP   FN   cons  fallb
baseline (prompt=baseline,…)  68%   8    7    1    6   100%   0%
tight    (prompt=tight,…)     91%   8   12    0    2    96%   0%
```

- `acc` — overall accuracy (correct decisions / total). Higher is better.
- `TP/TN/FP/FN` — confusion matrix relative to ground truth. `FP` is
  "we notified when we shouldn't have" (the spam we're trying to kill).
  `FN` is "we suppressed when we should have notified" (the regression
  we DON'T want to introduce).
- `cons` — self-consistency. Re-running each case `--repeats` times,
  what fraction returned the same answer every time? Should be near
  100% at temperature=0; if it's not, your provider has nondeterminism
  you need to know about.
- `fallb` — what fraction of decisions came from the "tool call
  failed, fall back to notify" branch. Production code mirrors this
  fallback, so it counts as a real decision — but we track it
  separately so we know when a model is failing to use the tool
  schema reliably.

## Ground-truth bias

The seed `cases.jsonl` is hand-crafted. That means it's biased toward
the noise patterns we've personally observed; a real evaluator could
do well on this set and still flunk a pattern we haven't thought to
write. Before recommending a change ship to production, add 10+ cases
pulled from the next 24h of real heartbeat-execute responses (label
them yourself) and re-run.

## What this eval does NOT cover

- The **task-execute** LLM call (the one that produces the response in
  the first place). That's a much bigger surface and the prompt lives
  in `build_system_prompt()` per channel, not in evaluator.md.
- The **decide** step (`heartbeat.service:_decide`) — only fires when
  `last_run_tracking=false`, which is rarely the case.
- The **pre-check** path (`gateway.heartbeat.preCheckRegistry`) —
  deterministic Python, no LLM, no evaluator involvement.

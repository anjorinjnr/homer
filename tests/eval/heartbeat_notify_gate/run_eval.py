#!/usr/bin/env python3
"""Offline eval harness for the heartbeat notify-gate.

Runs candidate evaluator (prompt, model) combos against a labeled
case set and reports a confusion matrix per combo. Use this BEFORE
proposing a change to `nanobot/templates/agent/evaluator.md` or to
the heartbeat-service's evaluator-model setting.

Usage:
    # Run all four combos with default models, 3 repeats per case
    HOMER_EVAL_API_KEY=$OPENROUTER_API_KEY \\
      python tests/eval/heartbeat_notify_gate/run_eval.py

    # Single variant for fast iteration
    python tests/eval/heartbeat_notify_gate/run_eval.py --variant tight --repeats 1

    # Custom models
    python tests/eval/heartbeat_notify_gate/run_eval.py \\
      --baseline-model openrouter/google/gemini-2.5-flash \\
      --smart-model openrouter/anthropic/claude-haiku-4-5-20251001

    # Machine-readable output (JSON only, no human tables)
    python tests/eval/heartbeat_notify_gate/run_eval.py --json | jq .

Cost: each repeat is ~256 output tokens. With 22 cases × 3 repeats ×
4 combos = 264 calls. On Gemini Flash that's ~$0.02. On a Sonnet-class
model maybe $0.20. Run the smart-model axis sparingly.

Speed: calls run concurrently up to --concurrency (default 8). A full
sweep typically completes in ~30s rather than ~9m sequential.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Standalone harness: deliberately does not import from homer's tools/
# or agent/ packages. The eval is self-contained on purpose so a refactor
# in homer can't quietly invalidate eval results.
from variants import BASELINE, TIGHT, EVALUATE_TOOL


# ── Case loader ──────────────────────────────────────────────────────────

@dataclass
class Case:
    id: str
    category: str           # "should_suppress" | "should_notify" | "edge_case"
    task: str
    response: str
    expected: bool          # True = notify, False = suppress
    note: str = ""

    @classmethod
    def load_all(cls, path: Path) -> list["Case"]:
        out: list["Case"] = []
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            d = json.loads(ln)
            out.append(cls(
                id=d["id"], category=d["category"], task=d["task"],
                response=d["response"], expected=bool(d["expected"]),
                note=d.get("note", ""),
            ))
        return out


# ── LLM call (async, single attempt with retries) ────────────────────────

# Tool-call decisions can fall through to "no tool call returned" in
# production — we count that path separately so it doesn't get blended
# into the model's actual decision rate. Names mirror the metric so the
# JSON output is self-documenting.
SOURCE_TOOL_CALL = "tool_call"
SOURCE_FALLBACK = "fallback_notify"


async def _call_evaluator(
    *,
    prompt: str,
    model: str,
    task: str,
    response: str,
    api_key: str,
    timeout_s: float,
    max_retries: int,
) -> tuple[bool, str, str]:
    """Send one evaluator request. Returns (decision, reason, source).

    Mirrors `nanobot.utils.evaluator.evaluate_response` shape: tool is
    declared but NOT forced via `tool_choice`, so the eval observes the
    same "did the model decide to use the tool?" signal production sees.
    On `should_notify` absent or the call failing in a recoverable way,
    we fall back to True (notify) — same as production's fail-open.

    Retries transient errors (rate limits, timeouts, 5xx) with
    exponential backoff. Persistent failures yield ``fallback_notify``
    so the case still counts in the matrix; never raises into the
    summary code.
    """
    import litellm

    user = f"## Original task\n{task}\n\n## Agent response\n{response}"
    delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            resp = await litellm.acompletion(
                model=model,
                api_key=api_key,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user},
                ],
                tools=EVALUATE_TOOL,
                # NOTE: `tool_choice` is intentionally NOT forced.
                # Production code (`evaluate_response`) leaves it as the
                # provider default; forcing it here would hide the
                # tool-call-failure mode the eval is meant to surface.
                temperature=0.0,
                max_tokens=256,
                timeout=timeout_s,
            )
            break
        except Exception as exc:
            # litellm wraps provider errors into its own exception types;
            # rather than enumerate, retry transient-looking errors by
            # message+attempt and surface persistent failures as a
            # "fallback_notify" outcome so the eval reports rather than
            # crashes.
            transient_markers = ("429", "rate", "timeout", "timed out", "503", "502", "overload")
            is_transient = any(m in str(exc).lower() for m in transient_markers)
            if not is_transient or attempt >= max_retries:
                return True, f"call failed after {attempt + 1} attempt(s): {exc!s}", SOURCE_FALLBACK
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)
    else:  # exhausted retries with no break (defensive — `break` above is the happy path)
        return True, "exhausted retries with no response", SOURCE_FALLBACK

    choices = getattr(resp, "choices", None) or []
    if not choices:
        return True, "no choices returned", SOURCE_FALLBACK
    msg = choices[0].message
    tool_calls = getattr(msg, "tool_calls", None) or []
    if not tool_calls:
        return True, "no tool call returned", SOURCE_FALLBACK

    args_raw = tool_calls[0].function.arguments
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
    except json.JSONDecodeError:
        return True, f"unparseable args: {args_raw!r}", SOURCE_FALLBACK

    return bool(args.get("should_notify", True)), str(args.get("reason", "")), SOURCE_TOOL_CALL


# ── Per-variant runner + confusion matrix ────────────────────────────────

@dataclass
class CaseResult:
    case: Case
    decisions: list[bool] = field(default_factory=list)  # per-repeat
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def majority(self) -> bool:
        """Majority vote across repeats. Requires odd `--repeats` (the
        CLI enforces this), so ties are impossible at this layer."""
        c = Counter(self.decisions)
        return c[True] > c[False]

    @property
    def is_tie(self) -> bool:
        """Currently unreachable when CLI is used (odd-repeats enforcement)
        — kept so unit tests can poke even-count inputs and so a future
        non-CLI caller doesn't get silent bias."""
        c = Counter(self.decisions)
        return c[True] == c[False] and (c[True] + c[False]) > 0

    @property
    def consistent(self) -> bool:
        """True iff every repeat agreed."""
        return len(set(self.decisions)) <= 1

    @property
    def fallback_rate(self) -> float:
        if not self.sources:
            return 0.0
        return sum(1 for s in self.sources if s == SOURCE_FALLBACK) / len(self.sources)


async def _run_variant(
    *,
    label: str,
    prompt: str,
    model: str,
    cases: list[Case],
    repeats: int,
    api_key: str,
    concurrency: int,
    timeout_s: float,
    max_retries: int,
    quiet: bool,
) -> dict:
    if not quiet:
        print(f"\n┌─ {label}", file=sys.stderr)
        print(f"│   model: {model}", file=sys.stderr)
        print(f"│   cases: {len(cases)}   repeats: {repeats}   concurrency: {concurrency}", file=sys.stderr)
    t0 = time.monotonic()
    sem = asyncio.Semaphore(concurrency)

    # Flat (case_idx, repeat_idx) job list so we can gather everything
    # at once. The semaphore caps in-flight requests; the rest queue.
    async def _one(case_idx: int, repeat_idx: int) -> tuple[int, int, bool, str, str]:
        async with sem:
            decision, reason, source = await _call_evaluator(
                prompt=prompt, model=model,
                task=cases[case_idx].task,
                response=cases[case_idx].response,
                api_key=api_key,
                timeout_s=timeout_s,
                max_retries=max_retries,
            )
        return case_idx, repeat_idx, decision, reason, source

    jobs = [_one(ci, ri) for ci in range(len(cases)) for ri in range(repeats)]
    raw_results = await asyncio.gather(*jobs)

    # Bin per-case so the order in `results` matches the order in `cases`.
    results: list[CaseResult] = [CaseResult(case=c) for c in cases]
    # Ensure repeats arrive in order for stable display, though majority/
    # consistency don't depend on it.
    for case_idx, _repeat_idx, decision, reason, source in sorted(raw_results):
        cr = results[case_idx]
        cr.decisions.append(decision)
        cr.reasons.append(reason)
        cr.sources.append(source)

    if not quiet:
        for cr in results:
            mark = "✓" if cr.majority == cr.case.expected else "✗"
            consist = "" if cr.consistent else " [inconsistent]"
            print(f"│   {mark} {cr.case.id:14}  expected={cr.case.expected!s:5}  got={cr.majority!s:5}{consist}",
                  file=sys.stderr)
        elapsed = time.monotonic() - t0
        print(f"└─ {elapsed:.1f}s", file=sys.stderr)

    return _summarize(label, results, model=model)


def _summarize(label: str, results: list[CaseResult], *, model: str) -> dict:
    """Build a confusion matrix + per-category accuracy + self-consistency."""
    tp = sum(1 for r in results if r.majority and r.case.expected)
    tn = sum(1 for r in results if not r.majority and not r.case.expected)
    fp = sum(1 for r in results if r.majority and not r.case.expected)
    fn = sum(1 for r in results if not r.majority and r.case.expected)
    total = len(results)
    correct = tp + tn

    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_cat[r.case.category].append(r)

    consistency = sum(1 for r in results if r.consistent) / total if total else 0.0
    fallback_rate = statistics.mean(r.fallback_rate for r in results) if results else 0.0
    tie_rate = sum(1 for r in results if r.is_tie) / total if total else 0.0

    return {
        "label": label,
        "model": model,
        "accuracy": correct / total if total else 0.0,
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "by_category": {
            cat: {
                "n": len(rs),
                "correct": sum(1 for r in rs if r.majority == r.case.expected),
                # Carry both the id and its (often diagnostic) note so misses
                # show the human-readable "why this case mattered" line in
                # the summary table. Cheaper than chasing it up in cases.jsonl.
                "wrong": [
                    {"id": r.case.id, "note": r.case.note, "reasons": r.reasons}
                    for r in rs if r.majority != r.case.expected
                ],
            }
            for cat, rs in by_cat.items()
        },
        "self_consistency": consistency,
        "tool_call_fallback_rate": fallback_rate,
        "tie_rate": tie_rate,
    }


def _print_summary_table(summaries: list[dict]) -> None:
    out = sys.stderr  # human output to stderr so stdout stays clean for --json
    print("\n" + "─" * 88, file=out)
    print("Summary across variants", file=out)
    print("─" * 88, file=out)
    print(f"{'variant':<28} {'acc':>6} {'TP':>4} {'TN':>4} {'FP':>4} {'FN':>4} "
          f"{'cons':>6} {'fallb':>6} {'tie':>5}", file=out)
    print("─" * 88, file=out)
    for s in summaries:
        cm = s["confusion"]
        print(
            f"{s['label']:<28} "
            f"{s['accuracy']:>6.2%} "
            f"{cm['tp']:>4} {cm['tn']:>4} {cm['fp']:>4} {cm['fn']:>4} "
            f"{s['self_consistency']:>6.2%} "
            f"{s['tool_call_fallback_rate']:>6.2%} "
            f"{s['tie_rate']:>5.2%}",
            file=out,
        )
    print("", file=out)
    for s in summaries:
        wrong = [(cat, rs["wrong"]) for cat, rs in s["by_category"].items() if rs["wrong"]]
        if wrong:
            print(f"  {s['label']} misses:", file=out)
            for cat, items in wrong:
                for item in items:
                    note = f"  — {item['note']}" if item.get("note") else ""
                    print(f"    {cat}: {item['id']}{note}", file=out)


# ── CLI ──────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline eval harness for the heartbeat notify-gate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--variant", choices=["baseline", "tight", "smart", "both", "all"], default="all",
                        help="Which combo(s) to run. 'all' = baseline + tight + smart + both. Default: all.")
    parser.add_argument("--baseline-model", default="openrouter/google/gemini-2.5-flash",
                        help="Model for baseline/tight variants — mirrors what heartbeat uses today.")
    parser.add_argument("--smart-model", default="openrouter/anthropic/claude-haiku-4-5-20251001",
                        help="Smarter model for the smart/both variants. Default avoids pro-tier so eval "
                             "stays cheap; override per investigation.")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Run each case N times to estimate self-consistency. Must be odd so the "
                             "majority vote is unambiguous. Default 3.")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Max in-flight LLM calls. Bump for faster sweeps, lower if you're hitting "
                             "provider rate limits. Default 8.")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Per-request timeout in seconds. Default 30.")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Retry attempts on transient errors (429, 5xx, timeouts) with exponential "
                             "backoff. Default 3.")
    parser.add_argument("--cases", default=str(Path(__file__).parent / "cases.jsonl"),
                        help="Path to cases.jsonl.")
    parser.add_argument("--api-key-env", default="HOMER_EVAL_API_KEY",
                        help="Env var holding the LLM API key. Defaults to HOMER_EVAL_API_KEY to keep "
                             "this separate from production OPENROUTER_API_KEY.")
    parser.add_argument("--json", action="store_true",
                        help="Emit ONLY the JSON summary to stdout. Human tables go to stderr (and are "
                             "suppressed entirely with --quiet). Designed for `... --json | jq`.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-case progress lines. Implies tables-to-stderr only.")
    args = parser.parse_args(argv)

    if args.repeats % 2 == 0 or args.repeats < 1:
        parser.error(f"--repeats must be a positive odd integer (got {args.repeats}). "
                     "Odd values guarantee an unambiguous majority vote.")
    if args.concurrency < 1:
        parser.error(f"--concurrency must be >= 1 (got {args.concurrency}).")
    return args


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        print(f"ERROR: ${args.api_key_env} is not set. The eval needs an LLM API key.", file=sys.stderr)
        return 2

    cases_path = Path(args.cases)
    cases = Case.load_all(cases_path)
    if not cases:
        print(f"ERROR: no cases loaded from {cases_path}", file=sys.stderr)
        return 2

    combos: list[tuple[str, str, str]] = []
    if args.variant in ("baseline", "all"):
        combos.append(("baseline:fast", BASELINE, args.baseline_model))
    if args.variant in ("tight", "all"):
        combos.append(("tight:fast", TIGHT, args.baseline_model))
    if args.variant in ("smart", "all"):
        combos.append(("baseline:smart", BASELINE, args.smart_model))
    if args.variant in ("both", "all"):
        combos.append(("tight:smart", TIGHT, args.smart_model))

    summaries: list[dict] = []
    for label, prompt, model in combos:
        summaries.append(await _run_variant(
            label=label, prompt=prompt, model=model,
            cases=cases, repeats=args.repeats, api_key=api_key,
            concurrency=args.concurrency, timeout_s=args.timeout,
            max_retries=args.max_retries, quiet=args.quiet,
        ))

    if not args.quiet:
        _print_summary_table(summaries)

    if args.json:
        # JSON on stdout, tables on stderr — `... --json | jq .` works.
        json.dump({"summaries": summaries}, sys.stdout, indent=2)
        print(file=sys.stdout)  # trailing newline so cat / jq are happy
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

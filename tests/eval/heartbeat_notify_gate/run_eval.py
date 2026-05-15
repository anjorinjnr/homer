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

Cost: each repeat is ~256 output tokens. With 22 cases × 3 repeats ×
4 combos = 264 calls. On Gemini Flash that's ~$0.02. On a Sonnet-class
model maybe $0.20. Run the smart-model axis sparingly.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

# Import shared variants. Living next to this file so a `git mv` of
# the whole `heartbeat_notify_gate/` directory keeps the eval intact.
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


# ── LLM call (single, sync, tool-call shape) ─────────────────────────────

def _call_evaluator(*, prompt: str, model: str, task: str, response: str, api_key: str) -> tuple[bool | None, str, str]:
    """Send one evaluator request. Returns (decision, reason, decision_source).

    decision_source:
      - "tool_call"     — model called the tool, decision is the arg value
      - "fallback_notify" — no tool call returned; production code defaults
                            to notify, eval mirrors that. Counted separately
                            so we can tell "decided notify" from "failed to
                            call tool and we faked notify".
    """
    import litellm

    user = f"## Original task\n{task}\n\n## Agent response\n{response}"
    resp = litellm.completion(
        model=model,
        api_key=api_key,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
        tools=EVALUATE_TOOL,
        tool_choice={"type": "function", "function": {"name": "evaluate_notification"}},
        temperature=0.0,
        max_tokens=256,
    )

    choices = getattr(resp, "choices", None) or []
    if not choices:
        return True, "no choices returned", "fallback_notify"
    msg = choices[0].message
    tool_calls = getattr(msg, "tool_calls", None) or []
    if not tool_calls:
        return True, "no tool call returned", "fallback_notify"

    args_raw = tool_calls[0].function.arguments
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
    except json.JSONDecodeError:
        return True, f"unparseable args: {args_raw!r}", "fallback_notify"

    return bool(args.get("should_notify", True)), str(args.get("reason", "")), "tool_call"


# ── Per-variant runner + confusion matrix ────────────────────────────────

@dataclass
class CaseResult:
    case: Case
    decisions: list[bool] = field(default_factory=list)  # per-repeat
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def majority(self) -> bool:
        """Majority vote across repeats. Ties → True (mirrors production
        fail-open behavior)."""
        c = Counter(self.decisions)
        return c[True] >= c[False]

    @property
    def consistent(self) -> bool:
        """True iff every repeat agreed."""
        return len(set(self.decisions)) <= 1

    @property
    def fallback_rate(self) -> float:
        if not self.sources:
            return 0.0
        return sum(1 for s in self.sources if s == "fallback_notify") / len(self.sources)


def run_variant(*, label: str, prompt: str, model: str, cases: list[Case], repeats: int, api_key: str) -> dict:
    print(f"\n┌─ {label}")
    print(f"│   model: {model}")
    print(f"│   cases: {len(cases)}   repeats: {repeats}")
    results: list[CaseResult] = []
    t0 = time.monotonic()
    for c in cases:
        cr = CaseResult(case=c)
        for _ in range(repeats):
            decision, reason, source = _call_evaluator(
                prompt=prompt, model=model,
                task=c.task, response=c.response, api_key=api_key,
            )
            cr.decisions.append(bool(decision))
            cr.reasons.append(reason)
            cr.sources.append(source)
        results.append(cr)
        mark = "✓" if cr.majority == c.expected else "✗"
        consist = "" if cr.consistent else " [inconsistent]"
        print(f"│   {mark} {c.id:14}  expected={c.expected!s:5}  got={cr.majority!s:5}{consist}")
    elapsed = time.monotonic() - t0
    print(f"└─ {elapsed:.1f}s")

    return _summarize(label, results)


def _summarize(label: str, results: list[CaseResult]) -> dict:
    """Build a confusion matrix + per-category accuracy + self-consistency."""
    # Overall confusion matrix.
    tp = sum(1 for r in results if r.majority and r.case.expected)
    tn = sum(1 for r in results if not r.majority and not r.case.expected)
    fp = sum(1 for r in results if r.majority and not r.case.expected)
    fn = sum(1 for r in results if not r.majority and r.case.expected)
    total = len(results)
    correct = tp + tn

    # Per-category breakdown.
    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_cat[r.case.category].append(r)

    consistency = sum(1 for r in results if r.consistent) / total if total else 0.0
    fallback_rate = statistics.mean(r.fallback_rate for r in results) if results else 0.0

    summary = {
        "label": label,
        "accuracy": correct / total if total else 0.0,
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "by_category": {
            cat: {
                "n": len(rs),
                "correct": sum(1 for r in rs if r.majority == r.case.expected),
                "wrong": [r.case.id for r in rs if r.majority != r.case.expected],
            }
            for cat, rs in by_cat.items()
        },
        "self_consistency": consistency,
        "tool_call_fallback_rate": fallback_rate,
    }
    return summary


def _print_summary_table(summaries: list[dict]) -> None:
    print("\n" + "─" * 78)
    print("Summary across variants")
    print("─" * 78)
    print(f"{'variant':<28} {'acc':>6} {'TP':>4} {'TN':>4} {'FP':>4} {'FN':>4} {'cons':>6} {'fallb':>6}")
    print("─" * 78)
    for s in summaries:
        cm = s["confusion"]
        print(
            f"{s['label']:<28} "
            f"{s['accuracy']:>6.2%} "
            f"{cm['tp']:>4} {cm['tn']:>4} {cm['fp']:>4} {cm['fn']:>4} "
            f"{s['self_consistency']:>6.2%} "
            f"{s['tool_call_fallback_rate']:>6.2%}"
        )
    print()
    for s in summaries:
        wrong = [(cat, rs["wrong"]) for cat, rs in s["by_category"].items() if rs["wrong"]]
        if wrong:
            print(f"  {s['label']} misses:")
            for cat, ids in wrong:
                print(f"    {cat}: {', '.join(ids)}")


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["baseline", "tight", "smart", "both", "all"], default="all",
                        help="Which combo(s) to run. 'all' = baseline + tight + smart + both. Default: all.")
    parser.add_argument("--baseline-model", default="openrouter/google/gemini-2.5-flash",
                        help="Model for baseline/tight variants — mirrors what heartbeat uses today.")
    parser.add_argument("--smart-model", default="openrouter/anthropic/claude-haiku-4-5-20251001",
                        help="Smarter model for the smart/both variants. Don't pin a pro-tier model "
                             "by default — eval cost scales with this choice.")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Run each case N times to estimate self-consistency. "
                             "Temperature=0 should yield 100%% but providers vary. Default 3.")
    parser.add_argument("--cases", default=str(Path(__file__).parent / "cases.jsonl"),
                        help="Path to cases.jsonl.")
    parser.add_argument("--api-key-env", default="HOMER_EVAL_API_KEY",
                        help="Env var holding the LLM API key. Defaults to HOMER_EVAL_API_KEY "
                             "to keep this separate from production OPENROUTER_API_KEY.")
    parser.add_argument("--json", action="store_true",
                        help="Emit a single JSON document to stdout (in addition to the human-readable tables). "
                             "Useful for CI / regression-tracking.")
    args = parser.parse_args()

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
        combos.append(("baseline (prompt=baseline, model=fast)", BASELINE, args.baseline_model))
    if args.variant in ("tight", "all"):
        combos.append(("tight    (prompt=tight,    model=fast)", TIGHT, args.baseline_model))
    if args.variant in ("smart", "all"):
        combos.append(("smart    (prompt=baseline, model=smart)", BASELINE, args.smart_model))
    if args.variant in ("both", "all"):
        combos.append(("both     (prompt=tight,    model=smart)", TIGHT, args.smart_model))

    summaries: list[dict] = []
    for label, prompt, model in combos:
        summaries.append(run_variant(
            label=label, prompt=prompt, model=model,
            cases=cases, repeats=args.repeats, api_key=api_key,
        ))

    _print_summary_table(summaries)

    if args.json:
        print("\n" + json.dumps({"summaries": summaries}, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Unit tests for run_eval.py — mocks litellm so they're free + deterministic.

Catches the bugs the eval would otherwise hide: confusion-matrix math,
tie/majority semantics, --variant filter, --json output format, and the
retry path for transient errors.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import run_eval  # noqa: E402


# ── helpers: fake litellm responses ──────────────────────────────────────


def _ok_response(should_notify: bool, *, reason: str = "ok") -> SimpleNamespace:
    """Build a SimpleNamespace shaped like a litellm completion that
    contains a single tool_call returning the requested decision."""
    args = json.dumps({"should_notify": should_notify, "reason": reason})
    tc = SimpleNamespace(function=SimpleNamespace(arguments=args))
    msg = SimpleNamespace(tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _no_tool_response() -> SimpleNamespace:
    """Shape that triggers the fallback_notify branch — the model didn't
    call the tool. Mirrors the failure mode the production evaluator
    fails-open on."""
    msg = SimpleNamespace(tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _case(**overrides) -> run_eval.Case:
    d = dict(id="c1", category="should_suppress", task="t", response="r",
             expected=False, note="")
    d.update(overrides)
    return run_eval.Case(**d)


# ── _call_evaluator ──────────────────────────────────────────────────────


def test_call_evaluator_decodes_tool_call():
    """Happy path: model returns a tool call → harness returns (decision,
    reason, "tool_call")."""
    async def go():
        with patch("litellm.acompletion", new=AsyncMock(return_value=_ok_response(True, reason="yes"))):
            return await run_eval._call_evaluator(
                prompt="p", model="m", task="t", response="r",
                api_key="k", timeout_s=5, max_retries=0,
            )
    decision, reason, source = asyncio.run(go())
    assert decision is True
    assert reason == "yes"
    assert source == run_eval.SOURCE_TOOL_CALL


def test_call_evaluator_falls_open_when_no_tool_call():
    """Production fail-open path: model decided not to call the tool →
    treat as notify, source=fallback. The eval must MEASURE this mode,
    not paper over it, so the (untouched) `tool_choice` parameter
    matters — we don't force it.
    """
    async def go():
        with patch("litellm.acompletion", new=AsyncMock(return_value=_no_tool_response())):
            return await run_eval._call_evaluator(
                prompt="p", model="m", task="t", response="r",
                api_key="k", timeout_s=5, max_retries=0,
            )
    decision, _, source = asyncio.run(go())
    assert decision is True
    assert source == run_eval.SOURCE_FALLBACK


def test_call_evaluator_does_not_force_tool_choice():
    """Pin the production-mirror contract: forcing `tool_choice` would
    hide the fail-open path the eval is designed to measure.
    """
    mock = AsyncMock(return_value=_ok_response(True))
    async def go():
        with patch("litellm.acompletion", new=mock):
            await run_eval._call_evaluator(
                prompt="p", model="m", task="t", response="r",
                api_key="k", timeout_s=5, max_retries=0,
            )
    asyncio.run(go())
    call_kwargs = mock.await_args.kwargs
    assert "tool_choice" not in call_kwargs, \
        "harness must not force the tool — production doesn't force it either"
    assert call_kwargs["tools"] == run_eval.EVALUATE_TOOL


def test_call_evaluator_retries_transient_errors():
    """429s / timeouts / 5xx are retried up to max_retries; final
    success returns a clean tool_call result."""
    attempts: list[int] = []

    async def _fake(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("429 rate limit hit")
        return _ok_response(False)

    async def go():
        with patch("litellm.acompletion", side_effect=_fake), \
             patch("asyncio.sleep", new=AsyncMock()):  # don't actually sleep
            return await run_eval._call_evaluator(
                prompt="p", model="m", task="t", response="r",
                api_key="k", timeout_s=5, max_retries=5,
            )
    decision, _, source = asyncio.run(go())
    assert len(attempts) == 3, "expected two transient failures then success"
    assert decision is False
    assert source == run_eval.SOURCE_TOOL_CALL


def test_call_evaluator_persistent_error_returns_fallback():
    """Persistent non-transient error → fallback_notify with the error
    on the reason field. Eval never raises into summary code."""
    async def go():
        async def _explode(*_args, **_kwargs):
            raise RuntimeError("permission denied")
        with patch("litellm.acompletion", side_effect=_explode):
            return await run_eval._call_evaluator(
                prompt="p", model="m", task="t", response="r",
                api_key="k", timeout_s=5, max_retries=2,
            )
    decision, reason, source = asyncio.run(go())
    assert decision is True
    assert source == run_eval.SOURCE_FALLBACK
    assert "permission denied" in reason


# ── CaseResult math ──────────────────────────────────────────────────────


def test_majority_strict_no_tie_break_bias():
    """Strict majority means a 1-1 split is NOT counted as notify —
    the property reports `False` (suppress) AND `is_tie` flags it.
    Even-repeats are blocked by the CLI but the dataclass must still
    behave correctly when poked directly.
    """
    cr = run_eval.CaseResult(case=_case())
    cr.decisions = [True, False]
    assert cr.majority is False
    assert cr.is_tie is True


def test_majority_normal_case():
    cr = run_eval.CaseResult(case=_case())
    cr.decisions = [True, True, False]
    assert cr.majority is True
    assert cr.is_tie is False
    assert cr.consistent is False


def test_fallback_rate():
    cr = run_eval.CaseResult(case=_case())
    cr.sources = [run_eval.SOURCE_TOOL_CALL, run_eval.SOURCE_FALLBACK,
                  run_eval.SOURCE_FALLBACK, run_eval.SOURCE_TOOL_CALL]
    assert cr.fallback_rate == 0.5


# ── _summarize ───────────────────────────────────────────────────────────


def test_summarize_confusion_matrix():
    """Pin TP/TN/FP/FN math against a hand-built result set."""
    def make(cat, expected, decision):
        cr = run_eval.CaseResult(case=_case(category=cat, expected=expected, id=f"{cat}-{decision}"))
        cr.decisions = [decision]
        cr.sources = [run_eval.SOURCE_TOOL_CALL]
        return cr

    results = [
        make("should_notify",   True,  True),   # TP
        make("should_notify",   True,  False),  # FN
        make("should_suppress", False, False),  # TN
        make("should_suppress", False, True),   # FP
    ]
    s = run_eval._summarize("label", results, model="m")
    assert s["confusion"] == {"tp": 1, "tn": 1, "fp": 1, "fn": 1}
    assert s["accuracy"] == 0.5
    # Wrong list now carries per-case note + reasons for diagnostic UI.
    by_cat = s["by_category"]
    assert {item["id"] for item in by_cat["should_notify"]["wrong"]} == {"should_notify-False"}
    assert {item["id"] for item in by_cat["should_suppress"]["wrong"]} == {"should_suppress-True"}


# ── _parse_args ──────────────────────────────────────────────────────────


def test_parse_args_rejects_even_repeats():
    """Even --repeats would let a 1-1 vote silently bias the score.
    CLI must error out at parse time."""
    with pytest.raises(SystemExit):
        run_eval._parse_args(["--repeats", "2"])


def test_parse_args_rejects_zero_repeats():
    with pytest.raises(SystemExit):
        run_eval._parse_args(["--repeats", "0"])


def test_parse_args_rejects_zero_concurrency():
    with pytest.raises(SystemExit):
        run_eval._parse_args(["--concurrency", "0"])


def test_parse_args_accepts_valid():
    ns = run_eval._parse_args(["--repeats", "5", "--concurrency", "16", "--variant", "tight"])
    assert ns.repeats == 5
    assert ns.concurrency == 16
    assert ns.variant == "tight"


# ── --json / stdout shape ────────────────────────────────────────────────


def test_json_flag_emits_clean_json_on_stdout(tmp_path, monkeypatch):
    """`run_eval.py --json` must produce parseable JSON on stdout. Human
    tables go to stderr — running this in a pipe (`... | jq .`) is the
    main use case so the contract matters.
    """
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps({
        "id": "c1", "category": "should_suppress", "task": "t",
        "response": "no new actionable mail", "expected": False,
    }) + "\n")

    monkeypatch.setenv("HOMER_EVAL_API_KEY", "test-key")
    out, err = io.StringIO(), io.StringIO()
    with patch("litellm.acompletion", new=AsyncMock(return_value=_ok_response(False))), \
         redirect_stdout(out), redirect_stderr(err):
        rc = run_eval.main([
            "--variant", "baseline", "--repeats", "1",
            "--cases", str(cases_path), "--json", "--quiet",
        ])
    assert rc == 0
    # stdout is JSON only — must parse cleanly.
    parsed = json.loads(out.getvalue())
    assert "summaries" in parsed
    assert len(parsed["summaries"]) == 1
    assert parsed["summaries"][0]["accuracy"] == 1.0


def test_variant_filter_applied():
    """`--variant tight` runs only the tight combo, not all four."""
    captured: list[str] = []

    async def _fake(*, model, **_kwargs):
        captured.append(model)
        return _ok_response(False)

    cases_path = Path(__file__).parent / "cases.jsonl"
    with patch("litellm.acompletion", side_effect=_fake), \
         patch.dict("os.environ", {"HOMER_EVAL_API_KEY": "k"}), \
         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = run_eval.main([
            "--variant", "tight", "--repeats", "1",
            "--cases", str(cases_path), "--quiet",
        ])
    assert rc == 0
    # All calls go to the single (tight, fast-model) combo.
    assert len(set(captured)) == 1, f"expected exactly one model in the sweep, got {set(captured)}"

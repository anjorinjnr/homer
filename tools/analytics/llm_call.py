"""LLM-call telemetry wrapper.

Emits a single PostHog `$ai_generation` event per provider call (Anthropic /
Gemini / OpenAI / OpenRouter / Cerebras / etc.) so we can reconstruct per-
household token usage and cost without parsing logs.

Properties match PostHog's LLM Analytics shape (`$ai_*` namespace) so the
events show up natively in their LLM dashboards and can be queried directly
via `events.properties.$ai_total_cost_usd`.

Privacy: prompt + completion content is NEVER passed to this module. Only
counts, model identifiers, latency, and cost.

Pricing table is sourced from `nanobot.analytics.pricing`. Update there to
bump prices — the same table feeds nanobot's per-call telemetry, so a
single edit ships to both producers via the next image rebuild.

Usage — direct (computed values in hand):

    from tools.analytics.llm_call import track_llm_generation, get_distinct_id

    track_llm_generation(
        distinct_id=get_distinct_id(),
        model="claude-haiku-4-5-20251001",
        provider="anthropic",
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        latency_s=elapsed,
        task_kind="tool_classifier",
    )

Usage — context manager (times the call, fires on exit even on exception):

    from tools.analytics.llm_call import llm_call

    with llm_call(model=model, provider="anthropic",
                  task_kind="tool_classifier") as rec:
        resp = client.messages.create(...)
        rec.record(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import time
from typing import Any, Iterator, Literal

from nanobot.analytics.pricing import estimate_cost_usd

from tools.analytics.identity import get_household_id
from tools.analytics.posthog_client import get_client


# Re-exported so ``from tools.analytics.llm_call import estimate_cost_usd``
# remains the public entry point for Homer-side callers.
__all__ = [
    "LlmCallRecord",
    "TaskKind",
    "Provider",
    "estimate_cost_usd",
    "get_distinct_id",
    "llm_call",
    "track_llm_generation",
]


# Stringly-typed `task_kind` and `provider` were a maintenance hazard — typos
# only showed up at runtime, and PostHog ended up with `unknown:...` events.
# `Literal` gives static checkers + IDEs a closed set to enforce; the
# `_VALID_*` frozensets below are the runtime fallback for callers that drift
# (e.g. a new provider added in a hotfix before the type catches up).
TaskKind = Literal[
    "chat",
    "heartbeat_system",
    "heartbeat_user",
    "tool_classifier",
]
Provider = Literal[
    "anthropic",
    "gemini",
    "openai",
    "openrouter",
    "cerebras",
]


def get_distinct_id() -> str:
    """Distinct id for tool-side LLM calls.

    Tool-side classifiers don't have an inbound user (the call originates
    from a heartbeat task or another tool), so we attribute to the
    household. Mirrors how `tools/analytics/events.py` attributes
    `use_case_completed`.
    """
    hid = get_household_id()
    if hid:
        return hid
    return "system"


def _model_tier() -> str:
    return os.environ.get("HOMER_MODEL_TIER", "byok")


_VALID_TASK_KINDS: frozenset[str] = frozenset(
    {"chat", "heartbeat_system", "heartbeat_user", "tool_classifier"}
)
_VALID_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "gemini", "openai", "openrouter", "cerebras"}
)


@dataclasses.dataclass(frozen=True)
class LlmCallRecord:
    """Canonical bundle of fields for one `$ai_generation` event.

    Replaces the 12-kwarg `track_llm_generation` signature. Callers that
    already have a `Provider`/`TaskKind` Literal in hand can construct one
    directly and pass it via `record=`; the kwargs path is preserved for
    back-compat.
    """

    distinct_id: str
    model: str
    provider: Provider
    input_tokens: int
    output_tokens: int
    latency_s: float
    task_kind: TaskKind
    cache_read_tokens: int = 0
    is_error: bool = False
    http_status: int | None = None
    trace_id: str | None = None
    extra: dict[str, Any] | None = None


def track_llm_generation(
    distinct_id: str | None = None,
    *,
    record: LlmCallRecord | None = None,
    model: str | None = None,
    provider: Provider | str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_s: float | None = None,
    task_kind: TaskKind | str | None = None,
    cache_read_tokens: int = 0,
    is_error: bool = False,
    http_status: int | None = None,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Fire one `$ai_generation` event. Fire-and-forget.

    Two equivalent shapes:

      1. Dataclass (new canonical form)::

             track_llm_generation(record=LlmCallRecord(...))

      2. Kwargs (legacy, kept for back-compat)::

             track_llm_generation("d-1", model=..., provider=..., ...)

    Caller is responsible for never passing prompt/completion text — this
    function does NOT enforce that, by design (we don't want to silently
    drop a field; we want callers to have already filtered).
    """
    if record is not None:
        # Dataclass path — pull every field off the record. Positional/kwarg
        # arguments are ignored (mixing the two would only confuse readers).
        distinct_id = record.distinct_id
        model = record.model
        provider = record.provider
        input_tokens = record.input_tokens
        output_tokens = record.output_tokens
        latency_s = record.latency_s
        task_kind = record.task_kind
        cache_read_tokens = record.cache_read_tokens
        is_error = record.is_error
        http_status = record.http_status
        trace_id = record.trace_id
        extra = record.extra

    # Kwargs path: every required field must be present. We assert here
    # rather than declare them mandatory in the signature because the
    # `record=` form populates them from the dataclass at runtime.
    if distinct_id is None:
        raise TypeError("distinct_id is required (positional or via record=)")
    if model is None or provider is None or task_kind is None:
        raise TypeError("model, provider, and task_kind are required")
    if input_tokens is None or output_tokens is None or latency_s is None:
        raise TypeError("input_tokens, output_tokens, and latency_s are required")

    if task_kind not in _VALID_TASK_KINDS:
        # Don't raise — observability code shouldn't crash callers — but
        # tag the event so we can find drift in PostHog. Same fallback for
        # provider drift below.
        task_kind = f"unknown:{task_kind}"
    if provider not in _VALID_PROVIDERS:
        provider = f"unknown:{provider}"

    cost_usd = estimate_cost_usd(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
    )

    props: dict[str, Any] = {
        "$ai_model": model,
        "$ai_provider": provider,
        "$ai_input_tokens": int(input_tokens),
        "$ai_output_tokens": int(output_tokens),
        "$ai_cache_read_input_tokens": int(cache_read_tokens),
        "$ai_total_cost_usd": round(cost_usd, 8),
        "$ai_latency": round(float(latency_s), 4),
        "$ai_is_error": bool(is_error),
        "task_kind": task_kind,
        "tier": _model_tier(),
    }

    if http_status is not None:
        props["$ai_http_status"] = int(http_status)
    if trace_id:
        props["$ai_trace_id"] = trace_id

    hid = get_household_id()
    if hid:
        props["household_id"] = hid

    if extra:
        # Allow callers to add task-kind-specific fields (e.g. count of
        # emails classified) without polluting the core schema. Keys
        # starting with `$` are reserved for PostHog and dropped.
        for k, v in extra.items():
            if not k.startswith("$") and k not in props:
                props[k] = v

    client = get_client()
    client.capture(distinct_id, "$ai_generation", props)
    if hid:
        client.group_identify("household", hid, {})


class _Recorder:
    """Stash for token counts. Populated by caller via .record()."""

    __slots__ = ("input_tokens", "output_tokens", "cache_read_tokens", "http_status")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.http_status: int | None = None

    def record(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        http_status: int | None = None,
    ) -> None:
        self.input_tokens = int(input_tokens)
        self.output_tokens = int(output_tokens)
        self.cache_read_tokens = int(cache_read_tokens)
        self.http_status = http_status


@contextlib.contextmanager
def llm_call(
    *,
    model: str,
    provider: Provider | str,
    task_kind: TaskKind | str,
    distinct_id: str | None = None,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[_Recorder]:
    """Time + emit. Fires `$ai_generation` on exit (even on exception).

    Caller MUST call `rec.record(input_tokens=..., output_tokens=...)`
    inside the block. If they don't (e.g. SDK threw before usage was
    available), the event still fires with 0/0 token counts and
    `$ai_is_error=True` if there was an exception.
    """
    rec = _Recorder()
    start = time.monotonic()
    raised = False
    try:
        yield rec
    except BaseException:
        raised = True
        raise
    finally:
        latency_s = time.monotonic() - start
        track_llm_generation(
            distinct_id or get_distinct_id(),
            model=model,
            provider=provider,
            input_tokens=rec.input_tokens,
            output_tokens=rec.output_tokens,
            cache_read_tokens=rec.cache_read_tokens,
            latency_s=latency_s,
            task_kind=task_kind,
            is_error=raised,
            http_status=rec.http_status,
            trace_id=trace_id,
            extra=extra,
        )

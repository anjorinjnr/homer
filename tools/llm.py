"""Shared LLM-completion dispatcher for Homer tool sidecars.

Tool scripts (gmail_fetch, history_extract, …) used to each hold their own
`if provider == "anthropic" / elif "gemini" / else error` block. After the
OpenRouter consolidation that pattern broke on every tenant: the agent loop
reads provider="openrouter" out of nanobot config, but no tool script
recognised it.

This module routes through litellm so adding a new provider is a config
change, not a code change. Every call still emits a PostHog `$ai_generation`
event in the same shape as the agent-loop side, via
`tools.analytics.llm_call.llm_call`.
"""

from __future__ import annotations

import os
from typing import Any

from tools.analytics.llm_call import Provider, TaskKind, llm_call


_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
}


def _resolve_model(model: str, provider: Provider | str) -> str:
    """Translate (model, provider) into a litellm-routable model id.

    Nanobot config stores bare slugs like `google/gemini-2.5-pro` and
    `claude-haiku-4-5-20251001`; litellm wants the provider-prefixed form
    for non-default routes.
    """
    if provider == "openrouter" and not model.startswith("openrouter/"):
        return f"openrouter/{model}"
    if provider == "gemini" and not model.startswith("gemini/"):
        return f"gemini/{model}"
    if provider == "cerebras" and not model.startswith("cerebras/"):
        return f"cerebras/{model}"
    return model


def _api_key_for(provider: Provider | str) -> str | None:
    env = _API_KEY_ENV.get(str(provider))
    if env == "GEMINI_API_KEY":
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return os.environ.get(env) if env else None


def complete(
    *,
    prompt: str,
    model: str,
    provider: Provider | str,
    task_kind: TaskKind,
    system: str | None = None,
    max_tokens: int | None = 2048,
    temperature: float | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Single-shot text completion. Returns the assistant message content.

    `model` + `provider` are passed verbatim through to the analytics
    wrapper (so PostHog still distinguishes anthropic-direct from
    openrouter-routed-claude) while litellm sees the prefixed form.
    """
    import litellm

    api_key = _api_key_for(provider)
    if not api_key:
        env = _API_KEY_ENV.get(str(provider), "<unknown>")
        raise RuntimeError(f"{env} is not set (required for provider={provider!r})")

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict[str, Any] = {
        "model": _resolve_model(model, provider),
        "messages": messages,
        "api_key": api_key,
    }
    # max_tokens=None means "no cap" — defer to the model's native default.
    # Extraction callers (history_extract) need this; without it, long
    # structured-JSON outputs silently truncate at 2048 and parsing fails.
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature

    with llm_call(
        model=model, provider=provider, task_kind=task_kind, extra=extra
    ) as rec:
        response = litellm.completion(**kwargs)
        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cache_tok = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        rec.record(input_tokens=in_tok, output_tokens=out_tok, cache_read_tokens=cache_tok)

    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(choices[0].message, "content", "") or ""
    return content.strip()

"""Use-case classifier — Gemini Flash via OpenRouter, with LRU cache.

DEPRECATED IN PLACE — this module duplicates ``nanobot/analytics/classify.py``
(same prompt, same model, same cache shape). It survives because the one
caller (``tools.tasks_update.complete_task``) is sync, and the nanobot
classifier is async-only. Removing this file requires either
async-ifying ``complete_task`` or dropping the use-case analytics tag at
completion time. Until that lands, keep this in lockstep with the
nanobot copy — the route precedence + prompt + tag validator must
match exactly so per-tenant cost attribution and dashboards stay
consistent across the two paths.

Produces a snake_case tag per message. The LLM picks from a preferred set
when a message fits, and otherwise generates its own descriptive tag.
"other" is not a valid output — if classification fails for a technical
reason (no API key, network error, malformed response) we return
"unclassified" so dashboard filters can distinguish "model couldn't decide"
from "pipeline broke".

Routing follows the same three-step precedence as the nanobot classifier:

  1. ``LLM_SYSTEM_API_KEY`` → OpenRouter (post-consolidation default).
  2. ``HOMER_ANALYTICS_GEMINI_API_KEY`` → direct Gemini (legacy Homer-
     owned analytics key, kept for pre-consolidation deployments).
  3. ``GEMINI_API_KEY`` → direct Gemini (dev/local single-key fallback).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import OrderedDict

logger = logging.getLogger(__name__)

# Tags the LLM is nudged toward. Not a whitelist — the LLM may return a
# different snake_case tag when none of these fit. Add here when a newly
# generated tag becomes common enough to stabilize.
PREFERRED_TAGS: tuple[str, ...] = (
    "calendar",
    "events",
    "meal_planning",
    "maintenance",
    "health",
    "finance",
    "email",
    "tasks_reminders",
    "research",
    "travel",
    "people",
    "admin",
    "chitchat",
)

_PROMPT_TEMPLATE = (
    "Classify this message to a household AI assistant. Return EXACTLY ONE "
    "lowercase snake_case tag. No punctuation, no quotes, no explanation.\n\n"
    "Prefer these tags when they fit:\n"
    "{preferred}\n\n"
    "If none fit, generate your own descriptive snake_case tag "
    "(1-3 words joined by _). Do NOT return 'other' — pick something "
    "specific instead.\n\n"
    'Message: "{text}"'
)

# Accept any snake_case token starting with a letter. Min length 3 rejects
# truncated outputs ("ch" from "chitchat" when max_tokens cuts mid-token).
# Upper bound keeps pathological LLM output (prompt injections, run-on
# sentences) out of the analytics stream.
_TAG_RE = re.compile(r"^[a-z][a-z0-9_]{2,29}$")
_FALLBACK = "unclassified"

_LRU_MAX = 500


class _LRUCache:
    """Simple ordered-dict LRU cache."""

    def __init__(self, maxsize: int = _LRU_MAX):
        self._data: OrderedDict[str, str] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str) -> str | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, value: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        else:
            if len(self._data) >= self._maxsize:
                self._data.popitem(last=False)
        self._data[key] = value

    def __len__(self) -> int:
        return len(self._data)


_cache = _LRUCache()


def _message_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _validate(raw: str) -> str:
    """Return a valid snake_case tag, or _FALLBACK if the input is malformed
    or is literally "other" (which we actively reject)."""
    tag = raw.strip().strip('"').strip("'").strip().lower()
    if tag == "other":
        return _FALLBACK
    if _TAG_RE.fullmatch(tag):
        return tag
    return _FALLBACK


def classify_message(text: str) -> str:
    """Classify *text* into a snake_case use-case tag.

    Returns a preferred tag, a model-generated snake_case tag, or
    "unclassified" on technical failure. Results are cached by SHA-256 of
    the input text.
    """
    key = _message_hash(text)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        tag = _call_gemini(text)
    except Exception:
        logger.debug("Classification call failed", exc_info=True)
        tag = _FALLBACK
    _cache.put(key, tag)
    return tag


# Resolution order matches nanobot/analytics/classify.py (same env-var
# precedence) so both classifiers route to the same provider on a given
# tenant. After the OpenRouter consolidation,
# ``LLM_SYSTEM_API_KEY`` is the canonical path (platform-funded sub-key
# under the OpenRouter master). The Gemini-direct paths remain as
# fallbacks for legacy tenants and dev/local environments that haven't
# switched yet.
_CLASSIFIER_ROUTES: tuple[tuple[str, str, str], ...] = (
    (
        "LLM_SYSTEM_API_KEY",
        "https://openrouter.ai/api/v1/chat/completions",
        "google/gemini-2.5-flash",
    ),
    (
        "HOMER_ANALYTICS_GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-2.5-flash",
    ),
    (
        "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-2.5-flash",
    ),
)


def _resolve_route() -> tuple[str, str, str] | None:
    for env_var, url, model_id in _CLASSIFIER_ROUTES:
        key = (os.environ.get(env_var) or "").strip()
        if key:
            return key, url, model_id
    return None


def _resolve_api_key() -> str:
    """Compatibility shim — returns just the api_key. New code should
    prefer :func:`_resolve_route` to also get the route URL + model id."""
    chosen = _resolve_route()
    return chosen[0] if chosen else ""


def _call_gemini(text: str) -> str:
    """Call the active classifier route and return a validated tag.

    Routes through OpenRouter when ``LLM_SYSTEM_API_KEY`` is set
    (post-consolidation default); falls back to direct Gemini for legacy
    deployments. The kept-for-back-compat function name stays as
    ``_call_gemini`` so existing test patches don't break.
    """
    chosen = _resolve_route()
    if chosen is None:
        return _FALLBACK
    api_key, route_url, model_id = chosen
    try:
        import httpx

        prompt = _PROMPT_TEMPLATE.format(
            preferred=", ".join(PREFERRED_TAGS),
            text=text[:500],
        )
        # Gemini 2.5 Flash uses extended thinking by default. In OpenAI-compat
        # mode the thinking tokens count against `max_tokens`, so a low cap
        # eats the actual answer. `reasoning_effort: "none"` disables thinking
        # for this call (we don't need it for a one-tag classification), and
        # max_tokens is bumped to a comfortable margin for the longest tag.
        # OpenRouter forwards `reasoning_effort` to the upstream Gemini
        # endpoint verbatim, so the same payload works on both routes.
        resp = httpx.post(
            route_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100,
                "temperature": 0,
                "reasoning_effort": "none",
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        return _validate(raw)
    except Exception:
        logger.debug("Classifier request failed", exc_info=True)
        return _FALLBACK


# Expose cache internals for testing
_get_cache = lambda: _cache

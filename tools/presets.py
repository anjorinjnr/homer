"""Canonical model-preset slate for Homer.

Single source of truth for the (label → upstream model id, provider) map.
Three downstream consumers used to keep their own copies and drifted:

- ``tools/switch_model.py`` (ad-hoc `switch_model.py --model <preset>` flow)
- ``nanobot.heartbeat.service.MODEL_PRESETS`` (heartbeat task `Model:` field)
- ``agent/AGENTS.md`` (the table the agent reads at runtime)

This module owns the dict. ``switch_model.py`` and ``history_extract.py``
import from here directly. Nanobot picks up the slate via the heartbeat
config's ``modelPresets`` field in ``guest_config.json``. The AGENTS.md
table and the config templates are guarded against drift by tests in
``tests/test_presets_drift.py``.

Every entry routes through ``provider="openrouter"`` — the consolidation
moved all tenant LLM traffic to OpenRouter sub-keys, and OpenRouter
dispatches upstream based on the ``<vendor>/<model>`` prefix.
"""

from __future__ import annotations

from typing import TypedDict


class Preset(TypedDict):
    model: str
    provider: str


PRESETS: dict[str, Preset] = {
    "auto":             {"model": "openrouter/auto",                "provider": "openrouter"},
    "cheap":            {"model": "deepseek/deepseek-v3.2",         "provider": "openrouter"},

    "gemini-fast":      {"model": "google/gemini-3-flash-preview",  "provider": "openrouter"},
    "gemini-balanced":  {"model": "google/gemini-2.5-pro",          "provider": "openrouter"},
    "gemini-smart":     {"model": "google/gemini-3.1-pro-preview",  "provider": "openrouter"},

    "gpt-fast":         {"model": "openai/gpt-5-mini",              "provider": "openrouter"},
    "gpt-balanced":     {"model": "openai/gpt-5",                   "provider": "openrouter"},
    "gpt-smart":        {"model": "openai/gpt-5.5",                 "provider": "openrouter"},

    "claude-fast":      {"model": "anthropic/claude-haiku-4.5",     "provider": "openrouter"},
    "claude-balanced":  {"model": "anthropic/claude-sonnet-4.6",    "provider": "openrouter"},
    "claude-smart":     {"model": "anthropic/claude-opus-4.7",      "provider": "openrouter"},

    # Internal alias retained for the default-tier heartbeat path (used by
    # render_household_env when HOMER_HEARTBEAT_MODEL is unset). Same SKU
    # as `cheap`; named for clarity at the call site.
    "default-cheap":    {"model": "deepseek/deepseek-v3.2",         "provider": "openrouter"},
}


def model_presets_map() -> dict[str, str]:
    """Flat label → model id dict, in the shape nanobot's heartbeat config
    expects (`gateway.heartbeat.modelPresets`)."""
    return {label: spec["model"] for label, spec in PRESETS.items()}


def resolve(label: str) -> Preset:
    """Resolve a preset label to its (model, provider). Raises KeyError on
    an unknown label so callers must opt into pass-through fallback."""
    return PRESETS[label]

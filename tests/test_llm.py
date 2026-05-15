"""Tests for tools.llm — the shared litellm-backed completion dispatcher."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import llm


# ── _resolve_model ─────────────────────────────────────────────────────────


class TestResolveModel:
    def test_openrouter_gets_prefix(self):
        assert llm._resolve_model("google/gemini-2.5-pro", "openrouter") == \
            "openrouter/google/gemini-2.5-pro"

    def test_openrouter_idempotent(self):
        assert llm._resolve_model("openrouter/foo/bar", "openrouter") == "openrouter/foo/bar"

    def test_gemini_gets_prefix(self):
        assert llm._resolve_model("gemini-2.5-flash", "gemini") == "gemini/gemini-2.5-flash"

    def test_anthropic_passthrough(self):
        # Anthropic model ids are bare slugs; litellm auto-detects them.
        assert llm._resolve_model("claude-haiku-4-5-20251001", "anthropic") == \
            "claude-haiku-4-5-20251001"


# ── _api_key_for ───────────────────────────────────────────────────────────


class TestApiKeyFor:
    def test_openrouter_reads_openrouter_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        assert llm._api_key_for("openrouter") == "or-key"

    def test_gemini_falls_back_to_google_api_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "gk")
        assert llm._api_key_for("gemini") == "gk"

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert llm._api_key_for("anthropic") is None


# ── complete ───────────────────────────────────────────────────────────────


def _fake_response(text: str, *, prompt=10, completion=5, cached=2, model="m"):
    details = SimpleNamespace(cached_tokens=cached)
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=details,
    )
    message = SimpleNamespace(content=text)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)], usage=usage, model=model,
    )


class TestComplete:
    def test_openrouter_routing(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = _fake_response("  routed  ")
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            out = llm.complete(
                prompt="hi", model="google/gemini-2.5-pro",
                provider="openrouter", task_kind="tool_classifier",
            )
        assert out == "routed"
        kwargs = mock_litellm.completion.call_args.kwargs
        assert kwargs["model"] == "openrouter/google/gemini-2.5-pro"
        assert kwargs["api_key"] == "or-key"
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["max_tokens"] == 2048

    def test_system_prompt_prepended(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = _fake_response("ok")
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            llm.complete(
                prompt="user msg", system="you are X",
                model="claude-haiku-4-5", provider="anthropic",
                task_kind="tool_classifier", temperature=0.1,
            )
        kwargs = mock_litellm.completion.call_args.kwargs
        assert kwargs["messages"] == [
            {"role": "system", "content": "you are X"},
            {"role": "user", "content": "user msg"},
        ]
        assert kwargs["temperature"] == 0.1

    def test_raises_when_no_credential(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            llm.complete(
                prompt="x", model="m", provider="openrouter",
                task_kind="tool_classifier",
            )

    def test_max_tokens_none_omits_kwarg(self, monkeypatch):
        """max_tokens=None means 'don't cap' — kwarg must NOT be forwarded to
        litellm so the provider uses its native default. Extraction callers
        (history_extract) depend on this; otherwise long structured JSON
        truncates at 2048 and parsing fails.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = _fake_response("ok")
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            llm.complete(
                prompt="x", model="claude-haiku-4-5",
                provider="anthropic", task_kind="tool_classifier",
                max_tokens=None,
            )
        kwargs = mock_litellm.completion.call_args.kwargs
        assert "max_tokens" not in kwargs

    def test_empty_choices_returns_empty_string(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = SimpleNamespace(
            choices=[], usage=None, model="m",
        )
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            out = llm.complete(
                prompt="x", model="claude-haiku-4-5",
                provider="anthropic", task_kind="tool_classifier",
            )
        assert out == ""

    def test_captures_routed_model_when_or_substitutes(self, monkeypatch):
        """If OR's auto-router substitutes a different model (e.g. asking
        for `openrouter/auto` and getting `openai/gpt-5.4-pro` served),
        the served-model id must end up on the analytics event as
        `$ai_model_served`. This is the only way to answer "which call
        actually used GPT?" after the fact.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = _fake_response(
            "ok", model="openai/gpt-5.4-pro",
        )
        captured: dict = {}

        def _capture(_distinct_id, _event, props):
            captured.update(props)

        mock_client = MagicMock()
        mock_client.capture.side_effect = _capture
        with patch.dict(sys.modules, {"litellm": mock_litellm}), \
             patch("tools.analytics.llm_call.get_client", return_value=mock_client):
            llm.complete(
                prompt="x", model="auto", provider="openrouter",
                task_kind="tool_classifier",
            )
        assert captured["$ai_model"] == "auto"
        assert captured["$ai_model_served"] == "openai/gpt-5.4-pro"

    def test_omits_model_served_when_same_as_request(self, monkeypatch):
        """No point storing two copies of the same string. Keep the event
        payload tight: emit `$ai_model_served` only when the served model
        actually differs from what we asked for (direct-Anthropic and
        direct-Gemini routes always return the same model id they were
        called with).
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = _fake_response(
            "ok", model="claude-haiku-4-5-20251001",
        )
        captured: dict = {}

        def _capture(_distinct_id, _event, props):
            captured.update(props)

        mock_client = MagicMock()
        mock_client.capture.side_effect = _capture
        with patch.dict(sys.modules, {"litellm": mock_litellm}), \
             patch("tools.analytics.llm_call.get_client", return_value=mock_client):
            llm.complete(
                prompt="x", model="claude-haiku-4-5-20251001",
                provider="anthropic", task_kind="tool_classifier",
            )
        assert captured["$ai_model"] == "claude-haiku-4-5-20251001"
        assert "$ai_model_served" not in captured

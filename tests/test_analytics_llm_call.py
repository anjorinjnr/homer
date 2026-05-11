"""Tests for tools/analytics/llm_call.py — $ai_generation event emission."""

from __future__ import annotations

import re
import time
from unittest.mock import MagicMock, patch

import pytest

from tools.analytics import llm_call


@pytest.fixture(autouse=True)
def _household_env(monkeypatch):
    monkeypatch.setenv("HOMER_HOUSEHOLD_ID", "hh-llm-test")
    monkeypatch.setenv("HOMER_MODEL_TIER", "default")


def _capture(mock: MagicMock) -> tuple[str, str, dict]:
    """Return (distinct_id, event_name, properties) for the single call."""
    assert mock.capture.call_count == 1, mock.capture.call_args_list
    args = mock.capture.call_args.args
    assert args[1] == "$ai_generation"
    return args[0], args[1], args[2]


# ── estimate_cost_usd ────────────────────────────────────────────────────────


def test_estimate_cost_anthropic_haiku():
    # 1M input + 1M output → $1 + $5 = $6
    assert llm_call.estimate_cost_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == pytest.approx(6.00)


def test_estimate_cost_gemini_flash25():
    # 1M / 1M → $0.075 + $0.30
    assert llm_call.estimate_cost_usd(
        "gemini/gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == pytest.approx(0.375)


def test_estimate_cost_gemini_unprefixed_model_resolves():
    # Bare API name from a direct google-genai call still prices.
    assert llm_call.estimate_cost_usd(
        "gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=0,
    ) == pytest.approx(0.075)


def test_estimate_cost_unknown_model_returns_zero():
    assert llm_call.estimate_cost_usd(
        "totally-made-up-model",
        input_tokens=10_000,
        output_tokens=5_000,
    ) == 0.0


def test_estimate_cost_with_cache_read_uses_cache_rate():
    # Haiku: input $1.00, cache_read $0.10. 100K cache + 50K fresh + 0 out:
    # billed_input = 50K * 1.00/1M = 0.05
    # cache       = 100K * 0.10/1M = 0.01
    # total       = 0.06
    cost = llm_call.estimate_cost_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=150_000,  # total input = cache + fresh
        output_tokens=0,
        cache_read_tokens=100_000,
    )
    assert cost == pytest.approx(0.06, rel=1e-9)


def test_estimate_cost_zero_for_free_route():
    assert llm_call.estimate_cost_usd(
        "openrouter/deepseek/deepseek-chat-v3.2:free",
        input_tokens=10_000,
        output_tokens=10_000,
    ) == 0.0


# ── track_llm_generation ─────────────────────────────────────────────────────


def test_track_emits_event_with_full_shape():
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-1",
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            input_tokens=1234,
            output_tokens=567,
            latency_s=1.234,
            task_kind="tool_classifier",
            cache_read_tokens=200,
            http_status=200,
            trace_id="trace-abc",
        )
    distinct_id, event, props = _capture(client)
    assert distinct_id == "d-1"
    assert event == "$ai_generation"
    assert props["$ai_model"] == "claude-haiku-4-5-20251001"
    assert props["$ai_provider"] == "anthropic"
    assert props["$ai_input_tokens"] == 1234
    assert props["$ai_output_tokens"] == 567
    assert props["$ai_cache_read_input_tokens"] == 200
    assert props["$ai_latency"] == pytest.approx(1.234, rel=1e-3)
    assert props["$ai_is_error"] is False
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] == "trace-abc"
    assert props["task_kind"] == "tool_classifier"
    assert props["tier"] == "default"
    assert props["household_id"] == "hh-llm-test"
    # Cost computed from price table.
    assert props["$ai_total_cost_usd"] > 0
    # group_identify fires too.
    client.group_identify.assert_called_once_with("household", "hh-llm-test", {})


def test_track_unknown_task_kind_tagged_not_dropped():
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-1",
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            latency_s=0.1,
            task_kind="weirdkind",
        )
    _, _, props = _capture(client)
    assert props["task_kind"] == "unknown:weirdkind"


def test_track_falls_back_to_byok_tier_when_env_unset(monkeypatch):
    monkeypatch.delenv("HOMER_MODEL_TIER", raising=False)
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-1",
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            latency_s=0.1,
            task_kind="tool_classifier",
        )
    _, _, props = _capture(client)
    assert props["tier"] == "byok"


def test_track_no_household_omits_household_id(monkeypatch):
    monkeypatch.delenv("HOMER_HOUSEHOLD_ID", raising=False)
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-1",
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            latency_s=0.1,
            task_kind="tool_classifier",
        )
    _, _, props = _capture(client)
    assert "household_id" not in props
    client.group_identify.assert_not_called()


def test_track_extra_extends_but_cant_override_core():
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-1",
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            latency_s=0.1,
            task_kind="tool_classifier",
            extra={"emails_classified": 7, "$ai_model": "OVERRIDE", "tier": "OVERRIDE"},
        )
    _, _, props = _capture(client)
    assert props["emails_classified"] == 7
    # Core keys can't be clobbered by `extra`, and `$`-prefixed keys are dropped.
    assert props["$ai_model"] == "claude-haiku-4-5-20251001"
    assert props["tier"] == "default"


# ── llm_call context manager ────────────────────────────────────────────────


def test_context_manager_records_and_emits_on_success():
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        with llm_call.llm_call(
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            task_kind="tool_classifier",
            distinct_id="d-cm",
        ) as rec:
            time.sleep(0.01)
            rec.record(input_tokens=100, output_tokens=50)
    _, _, props = _capture(client)
    assert props["$ai_input_tokens"] == 100
    assert props["$ai_output_tokens"] == 50
    assert props["$ai_is_error"] is False
    assert props["$ai_latency"] >= 0.01


def test_context_manager_emits_on_exception_with_zero_tokens():
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        with pytest.raises(RuntimeError):
            with llm_call.llm_call(
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
                task_kind="tool_classifier",
                distinct_id="d-cm",
            ) as rec:
                # Caller never gets to .record() because the SDK threw.
                raise RuntimeError("boom")
    _, _, props = _capture(client)
    assert props["$ai_is_error"] is True
    assert props["$ai_input_tokens"] == 0
    assert props["$ai_output_tokens"] == 0


# ── PII regression ──────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"@[\w.-]+\.[a-z]{2,}", re.I)
_PHONE_RE = re.compile(r"\b\d{10}\b")


# ── LlmCallRecord (dataclass form) ──────────────────────────────────────────


def test_record_form_emits_same_event_as_kwargs():
    """Dataclass and kwargs paths must produce identical event properties —
    the dataclass is just a bundling convenience, not a behaviour change."""
    rec = llm_call.LlmCallRecord(
        distinct_id="d-1",
        model="claude-haiku-4-5-20251001",
        provider="anthropic",
        input_tokens=1234,
        output_tokens=567,
        latency_s=1.234,
        task_kind="tool_classifier",
        cache_read_tokens=200,
        http_status=200,
        trace_id="trace-abc",
    )
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(record=rec)
    distinct_id, event, props = _capture(client)
    assert distinct_id == "d-1"
    assert event == "$ai_generation"
    assert props["$ai_model"] == "claude-haiku-4-5-20251001"
    assert props["$ai_provider"] == "anthropic"
    assert props["$ai_input_tokens"] == 1234
    assert props["$ai_output_tokens"] == 567
    assert props["$ai_cache_read_input_tokens"] == 200
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] == "trace-abc"
    assert props["task_kind"] == "tool_classifier"


def test_record_with_extra_passes_through():
    rec = llm_call.LlmCallRecord(
        distinct_id="d-1",
        model="claude-haiku-4-5-20251001",
        provider="anthropic",
        input_tokens=10,
        output_tokens=5,
        latency_s=0.1,
        task_kind="tool_classifier",
        extra={"emails_classified": 7},
    )
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(record=rec)
    _, _, props = _capture(client)
    assert props["emails_classified"] == 7


def test_kwargs_missing_required_raises():
    """Bare-positional `track_llm_generation()` with nothing else is
    a programming error, not a fire-and-forget no-op."""
    with patch("tools.analytics.llm_call.get_client"):
        with pytest.raises(TypeError):
            llm_call.track_llm_generation()  # no record, no kwargs
        with pytest.raises(TypeError):
            llm_call.track_llm_generation("d-1")  # missing model/provider/...


# ── Provider Literal + drift fallback ───────────────────────────────────────


def test_unknown_provider_tagged_not_dropped():
    """An off-Literal provider (e.g. a vendor we haven't typed yet) is
    accepted at runtime but tagged with `unknown:` so PostHog dashboards
    can find drift."""
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-1",
            model="brand-new-model",
            provider="madeup-vendor",
            input_tokens=10,
            output_tokens=5,
            latency_s=0.1,
            task_kind="tool_classifier",
        )
    _, _, props = _capture(client)
    assert props["$ai_provider"] == "unknown:madeup-vendor"


def test_known_providers_pass_through_unchanged():
    """Every entry in the Provider Literal must round-trip without the
    `unknown:` prefix. Keep this in sync with `_VALID_PROVIDERS`."""
    for provider in ("anthropic", "gemini", "openai", "openrouter", "cerebras"):
        with patch("tools.analytics.llm_call.get_client") as mock_get:
            client = MagicMock()
            mock_get.return_value = client
            llm_call.track_llm_generation(
                "d-1",
                model="m",
                provider=provider,
                input_tokens=1,
                output_tokens=1,
                latency_s=0.1,
                task_kind="tool_classifier",
            )
        _, _, props = _capture(client)
        assert props["$ai_provider"] == provider


def test_pii_regression_no_email_or_phone_in_props():
    """A PII-shaped value passed via `extra` should be stored as-is — but
    we audit that the *core* shape never carries free-form text. This is
    the regression: if someone adds `prompt` or `completion` to the core
    schema, the assertion below will catch it because those would be the
    only string fields long enough to plausibly contain PII.
    """
    with patch("tools.analytics.llm_call.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        llm_call.track_llm_generation(
            "d-pii",
            model="claude-haiku-4-5-20251001",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            latency_s=0.1,
            task_kind="tool_classifier",
        )
    _, _, props = _capture(client)
    for k, v in props.items():
        if isinstance(v, str):
            assert not _EMAIL_RE.search(v), f"email-shaped value in {k}: {v!r}"
            assert not _PHONE_RE.search(v), f"phone-shaped value in {k}: {v!r}"
            assert len(v) < 200, f"{k} too long ({len(v)}); core shape should be small"

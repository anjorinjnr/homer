"""Tests for tools/analytics/classify.py — LRU cache and tag validation."""

from unittest.mock import patch

from tools.analytics.classify import (
    _FALLBACK,
    _LRUCache,
    PREFERRED_TAGS,
    _resolve_api_key,
    _validate,
    classify_message,
    _get_cache,
)


class TestLRUCache:
    def test_basic_put_get(self):
        cache = _LRUCache(maxsize=3)
        cache.put("a", "meal_planning")
        assert cache.get("a") == "meal_planning"

    def test_miss_returns_none(self):
        cache = _LRUCache(maxsize=3)
        assert cache.get("missing") is None

    def test_evicts_oldest(self):
        cache = _LRUCache(maxsize=2)
        cache.put("a", "v1")
        cache.put("b", "v2")
        cache.put("c", "v3")  # evicts "a"
        assert cache.get("a") is None
        assert cache.get("b") == "v2"
        assert cache.get("c") == "v3"

    def test_access_refreshes_order(self):
        cache = _LRUCache(maxsize=2)
        cache.put("a", "v1")
        cache.put("b", "v2")
        cache.get("a")  # refresh "a"
        cache.put("c", "v3")  # evicts "b" (least recently used)
        assert cache.get("a") == "v1"
        assert cache.get("b") is None
        assert cache.get("c") == "v3"

    def test_overwrite_existing(self):
        cache = _LRUCache(maxsize=2)
        cache.put("a", "v1")
        cache.put("a", "v2")
        assert cache.get("a") == "v2"
        assert len(cache) == 1

    def test_len(self):
        cache = _LRUCache(maxsize=5)
        cache.put("a", "1")
        cache.put("b", "2")
        assert len(cache) == 2

    def test_maxsize_500(self):
        cache = _LRUCache(maxsize=500)
        for i in range(600):
            cache.put(str(i), f"val_{i}")
        assert len(cache) == 500
        # First 100 should be evicted
        assert cache.get("0") is None
        assert cache.get("599") == "val_599"


class TestClassifyMessage:
    def test_cache_hit_avoids_api_call(self):
        """Second call for same text should hit cache, not call Gemini."""
        with patch("tools.analytics.classify._call_gemini", return_value="calendar") as mock:
            # Clear cache for test isolation
            cache = _get_cache()
            cache._data.clear()

            result1 = classify_message("schedule a meeting for tomorrow")
            result2 = classify_message("schedule a meeting for tomorrow")

            assert result1 == "calendar"
            assert result2 == "calendar"
            assert mock.call_count == 1  # only one API call

    def test_different_text_calls_api_twice(self):
        with patch("tools.analytics.classify._call_gemini", return_value="meal_planning") as mock:
            cache = _get_cache()
            cache._data.clear()

            classify_message("what should we have for dinner")
            classify_message("fix the leaky faucet")

            assert mock.call_count == 2

    def test_falls_back_to_unclassified_on_error(self):
        with patch("tools.analytics.classify._call_gemini", side_effect=Exception("boom")):
            cache = _get_cache()
            cache._data.clear()

            result = classify_message("anything")
            assert result == _FALLBACK
            assert _FALLBACK == "unclassified"

    def test_preferred_tags_cover_core_use_cases(self):
        """Guard against accidental regressions to the preferred list.

        The exact members are a product decision — this test asserts the
        shape (tuple of lowercase snake_case strings) and a few load-bearing
        members, not an exact equality, so future additions don't break CI.
        """
        assert isinstance(PREFERRED_TAGS, tuple)
        for tag in PREFERRED_TAGS:
            assert isinstance(tag, str)
            assert tag == tag.lower()
            assert "_" not in tag or tag.replace("_", "").isalpha()
        for must_exist in ("calendar", "events", "finance", "health", "email", "chitchat"):
            assert must_exist in PREFERRED_TAGS
        # "other" is deliberately removed — it provided no signal.
        assert "other" not in PREFERRED_TAGS

    def test_accepts_llm_generated_tag_outside_preferred(self):
        """The LLM is allowed to invent a tag when nothing preferred fits."""
        with patch("tools.analytics.classify._call_gemini", return_value="birthday_planning"):
            cache = _get_cache()
            cache._data.clear()
            result = classify_message("plan Kemi's 40th")
            assert result == "birthday_planning"


class TestValidate:
    def test_accepts_preferred_tag(self):
        assert _validate("calendar") == "calendar"

    def test_accepts_generated_snake_case(self):
        assert _validate("birthday_planning") == "birthday_planning"

    def test_strips_quotes_and_whitespace(self):
        assert _validate('  "meal_planning"  ') == "meal_planning"

    def test_lowercases(self):
        assert _validate("Calendar") == "calendar"

    def test_rejects_literal_other(self):
        assert _validate("other") == _FALLBACK

    def test_rejects_multiword_with_space(self):
        assert _validate("meal planning") == _FALLBACK

    def test_rejects_hyphen(self):
        assert _validate("meal-planning") == _FALLBACK

    def test_rejects_sentence(self):
        assert _validate("I think this is meal_planning") == _FALLBACK

    def test_rejects_empty(self):
        assert _validate("") == _FALLBACK

    def test_rejects_starts_with_digit(self):
        assert _validate("1st_task") == _FALLBACK

    def test_rejects_over_30_chars(self):
        assert _validate("a" * 31) == _FALLBACK

    def test_rejects_two_char_truncation(self):
        # "ch" appeared in production as a truncated "chitchat" — min length 3
        # blocks these mid-token clips from polluting the analytics stream.
        assert _validate("ch") == _FALLBACK
        assert _validate("my") == _FALLBACK

    def test_accepts_three_char_tag(self):
        assert _validate("car") == "car"


class TestResolveApiKey:
    """Resolution order after the OpenRouter consolidation:

      1. LLM_SYSTEM_API_KEY              → OpenRouter (system bucket)
      2. HOMER_ANALYTICS_GEMINI_API_KEY  → direct Gemini (legacy)
      3. GEMINI_API_KEY                  → direct Gemini (dev/local)
    """

    @staticmethod
    def _clear(monkeypatch):
        for v in (
            "LLM_SYSTEM_API_KEY",
            "HOMER_ANALYTICS_GEMINI_API_KEY",
            "GEMINI_API_KEY",
        ):
            monkeypatch.delenv(v, raising=False)

    def test_llm_system_key_wins(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LLM_SYSTEM_API_KEY", "sk-or-v1-system")
        monkeypatch.setenv("HOMER_ANALYTICS_GEMINI_API_KEY", "homer_owned")
        monkeypatch.setenv("GEMINI_API_KEY", "tenant_owned")
        assert _resolve_api_key() == "sk-or-v1-system"

    def test_prefers_analytics_key_when_llm_system_unset(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("HOMER_ANALYTICS_GEMINI_API_KEY", "homer_owned")
        monkeypatch.setenv("GEMINI_API_KEY", "tenant_owned")
        assert _resolve_api_key() == "homer_owned"

    def test_falls_back_to_tenant_key_when_others_unset(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("GEMINI_API_KEY", "tenant_owned")
        assert _resolve_api_key() == "tenant_owned"

    def test_returns_empty_when_nothing_set(self, monkeypatch):
        self._clear(monkeypatch)
        assert _resolve_api_key() == ""

    def test_strips_whitespace(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LLM_SYSTEM_API_KEY", "  sk-or-v1-padded  ")
        assert _resolve_api_key() == "sk-or-v1-padded"

    def test_blank_higher_priority_key_falls_through(self, monkeypatch):
        # An accidentally-empty higher-priority key (e.g. set to "" in a
        # deploy script) must not blackhole the classifier.
        self._clear(monkeypatch)
        monkeypatch.setenv("LLM_SYSTEM_API_KEY", "   ")
        monkeypatch.setenv("HOMER_ANALYTICS_GEMINI_API_KEY", "homer_owned")
        assert _resolve_api_key() == "homer_owned"

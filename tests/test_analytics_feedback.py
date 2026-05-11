"""Tests for tools/analytics/feedback.py."""

from tools.analytics.feedback import FeedbackSignal, detect_feedback


class TestDetectFeedback:
    # -- Explicit command -------------------------------------------------------

    def test_feedback_command_neutral(self):
        result = detect_feedback("/feedback some thoughts")
        assert result == FeedbackSignal("neutral", "explicit_command")

    def test_feedback_command_positive(self):
        result = detect_feedback("/feedback that was helpful")
        assert result == FeedbackSignal("positive", "explicit_command")

    def test_feedback_command_negative(self):
        result = detect_feedback("/feedback that's wrong")
        assert result == FeedbackSignal("negative", "explicit_command")

    def test_feedback_command_case_insensitive(self):
        result = detect_feedback("/Feedback ok")
        assert result is not None
        assert result.trigger == "explicit_command"

    # -- Emoji reactions -------------------------------------------------------

    def test_thumbs_up(self):
        result = detect_feedback("\U0001f44d")
        assert result == FeedbackSignal("positive", "emoji_reaction")

    def test_thumbs_down(self):
        result = detect_feedback("\U0001f44e")
        assert result == FeedbackSignal("negative", "emoji_reaction")

    def test_heart_emoji(self):
        result = detect_feedback("\u2764\ufe0f great")
        assert result == FeedbackSignal("positive", "emoji_reaction")

    # -- Keywords --------------------------------------------------------------

    def test_positive_keyword_thanks(self):
        result = detect_feedback("thanks for helping with that")
        assert result == FeedbackSignal("positive", "keyword")

    def test_positive_keyword_perfect(self):
        result = detect_feedback("perfect, exactly what I needed")
        assert result == FeedbackSignal("positive", "keyword")

    def test_negative_keyword_wrong(self):
        result = detect_feedback("that's wrong, the appointment is on Tuesday")
        assert result == FeedbackSignal("negative", "keyword")

    def test_negative_keyword_not_helpful(self):
        result = detect_feedback("not helpful at all")
        assert result == FeedbackSignal("negative", "keyword")

    def test_negative_keyword_useless(self):
        result = detect_feedback("this is useless")
        assert result == FeedbackSignal("negative", "keyword")

    # -- No feedback -----------------------------------------------------------

    def test_normal_message(self):
        assert detect_feedback("what's the weather today?") is None

    def test_empty_string(self):
        assert detect_feedback("") is None

    def test_whitespace_only(self):
        assert detect_feedback("   ") is None

    def test_question_with_thanks_substring(self):
        """'thanksgiving' should not trigger 'thanks' match."""
        assert detect_feedback("when is thanksgiving this year?") is None

    # -- Priority ordering ----------------------------------------------------

    def test_command_takes_priority_over_emoji(self):
        result = detect_feedback("/feedback \U0001f44d")
        assert result.trigger == "explicit_command"

    def test_emoji_takes_priority_over_keyword(self):
        result = detect_feedback("\U0001f44e that's wrong")
        assert result.trigger == "emoji_reaction"

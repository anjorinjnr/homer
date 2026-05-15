"""
Tests for gmail_fetch.py — auto-skip rules, category matching, response parsing,
model config loading.

All tests use only pure logic functions — no real API calls, no credentials needed.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import gmail_fetch as gf


# ── should_auto_skip() ────────────────────────────────────────────────────────

class TestGmailAutoSkip:
    def _email(self, labels):
        return {"labels": set(labels), "sender": "", "subject": "", "body": ""}

    def test_skips_promotions(self):
        assert gf.should_auto_skip(self._email(["CATEGORY_PROMOTIONS"])) is True

    def test_skips_social(self):
        assert gf.should_auto_skip(self._email(["CATEGORY_SOCIAL"])) is True

    def test_skips_forums(self):
        assert gf.should_auto_skip(self._email(["CATEGORY_FORUMS"])) is True

    def test_keeps_inbox(self):
        assert gf.should_auto_skip(self._email(["INBOX"])) is False

    def test_keeps_unread(self):
        assert gf.should_auto_skip(self._email(["INBOX", "UNREAD"])) is False

    def test_skips_if_any_noise_label_present(self):
        assert gf.should_auto_skip(self._email(["INBOX", "CATEGORY_PROMOTIONS"])) is True


# ── matches_category_rule() ───────────────────────────────────────────────────

class TestGmailCategoryRules:
    def _email(self, sender="", subject=""):
        return {"labels": set(), "sender": sender, "subject": subject, "body": ""}

    def test_amazon_sender_skipped(self):
        matched, cat = gf.matches_category_rule(self._email(sender="orders@amazon.com"))
        assert matched is True
        assert cat == "amazon_orders"

    def test_fedex_sender_skipped(self):
        matched, cat = gf.matches_category_rule(self._email(sender="tracking@fedex.com"))
        assert matched is True
        assert cat == "package_deliveries"

    def test_delivery_subject_skipped(self):
        matched, cat = gf.matches_category_rule(self._email(subject="Your package is out for delivery"))
        assert matched is True
        assert cat == "package_deliveries"

    def test_security_alert_skipped(self):
        matched, cat = gf.matches_category_rule(self._email(subject="Security alert: new sign-in"))
        assert matched is True
        assert cat == "security_alerts"

    def test_new_device_skipped(self):
        matched, cat = gf.matches_category_rule(self._email(subject="New device signed in to your account"))
        assert matched is True
        assert cat == "security_alerts"

    def test_normal_email_not_matched(self):
        matched, _ = gf.matches_category_rule(self._email(sender="school@counterpane.org", subject="Field trip notice"))
        assert matched is False

    def test_case_insensitive_sender(self):
        matched, cat = gf.matches_category_rule(self._email(sender="auto-confirm@Amazon.COM"))
        assert matched is True
        assert cat == "amazon_orders"


# ── Claude response parsing ───────────────────────────────────────────────────

class TestGmailResponseParsing:
    """Test the Claude response JSON extraction logic (strip markdown fences)."""

    def _parse(self, raw: str):
        """Replicate the parsing logic from classify_emails."""
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        if not raw:
            return None
        return json.loads(raw)

    def test_plain_json_array(self):
        raw = '[{"email_index": 1, "action": "reply", "urgency": "today"}]'
        result = self._parse(raw)
        assert result[0]["email_index"] == 1

    def test_strips_json_code_fence(self):
        raw = '```json\n[{"email_index": 1}]\n```'
        result = self._parse(raw)
        assert result[0]["email_index"] == 1

    def test_strips_plain_code_fence(self):
        raw = '```\n[{"email_index": 2}]\n```'
        result = self._parse(raw)
        assert result[0]["email_index"] == 2

    def test_empty_array(self):
        raw = "[]"
        result = self._parse(raw)
        assert result == []

    def test_empty_array_in_fence(self):
        raw = "```json\n[]\n```"
        result = self._parse(raw)
        assert result == []


# ── _get_model_config() ─────────────────────────────────────────────────────

class TestGetModelConfig:
    """Test model/provider config loading from nanobot config."""

    def test_reads_valid_config(self, tmp_path):
        config = {"agents": {"defaults": {"model": "gemini/gemini-3-pro", "provider": "gemini"}}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))
        with patch.object(gf, "NANOBOT_CONFIG_PATH", config_file):
            model, provider = gf._get_model_config()
        assert model == "gemini/gemini-3-pro"
        assert provider == "gemini"

    def test_falls_back_when_config_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with patch.object(gf, "NANOBOT_CONFIG_PATH", missing):
            model, provider = gf._get_model_config()
        assert model == "claude-haiku-4-5-20251001"
        assert provider == "anthropic"

    def test_falls_back_on_invalid_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("not json{{{")
        with patch.object(gf, "NANOBOT_CONFIG_PATH", config_file):
            model, provider = gf._get_model_config()
        assert model == "claude-haiku-4-5-20251001"
        assert provider == "anthropic"

    def test_falls_back_when_config_is_array(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("[1, 2, 3]")
        with patch.object(gf, "NANOBOT_CONFIG_PATH", config_file):
            model, provider = gf._get_model_config()
        assert model == "claude-haiku-4-5-20251001"
        assert provider == "anthropic"

    def test_falls_back_when_model_empty(self, tmp_path):
        config = {"agents": {"defaults": {"model": "", "provider": "anthropic"}}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))
        with patch.object(gf, "NANOBOT_CONFIG_PATH", config_file):
            model, provider = gf._get_model_config()
        assert model == "claude-haiku-4-5-20251001"
        assert provider == "anthropic"

    def test_falls_back_when_defaults_missing(self, tmp_path):
        config = {"agents": {}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))
        with patch.object(gf, "NANOBOT_CONFIG_PATH", config_file):
            model, provider = gf._get_model_config()
        assert model == "claude-haiku-4-5-20251001"
        assert provider == "anthropic"


# ── _call_llm() ─────────────────────────────────────────────────────────────


class TestCallLlm:
    """gmail_fetch._call_llm now delegates to tools.llm.complete.

    Per-provider dispatch logic + usage extraction moved into tools/llm.py
    (litellm-backed), so this surface only needs to confirm the script
    passes its arguments through and turns dispatcher errors into the
    JSON-error+exit-1 contract the agent loop expects.
    """

    def test_dispatches_to_tools_llm_complete(self):
        with patch("tools.llm.complete") as mock_complete:
            mock_complete.return_value = "actionable"
            result = gf._call_llm("prompt body", "google/gemini-2.5-pro", "openrouter")
        assert result == "actionable"
        mock_complete.assert_called_once_with(
            prompt="prompt body",
            model="google/gemini-2.5-pro",
            provider="openrouter",
            task_kind="tool_classifier",
            extra={"tool": "gmail_fetch"},
        )

    def test_dispatcher_failure_exits_1_with_json_error(self, capsys):
        with patch("tools.llm.complete", side_effect=RuntimeError("upstream 503")):
            with pytest.raises(SystemExit) as exc_info:
                gf._call_llm("x", "claude-haiku", "anthropic")
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "anthropic" in payload["error"]
        assert "503" in payload["error"]


# ── fetch_emails() — gogcli wrapper ─────────────────────────────────────────

from datetime import datetime, timezone


class TestFetchEmails:
    """fetch_emails normalizes gogcli output into the existing email dict shape."""

    def test_normalizes_single_message(self):
        gog_response = {
            "messages": [
                {
                    "id": "abc123",
                    "threadId": "thr1",
                    "date": "2026-05-03 13:10",
                    "from": "school@example.org",
                    "subject": "Field trip permission",
                    "labels": ["INBOX", "UNREAD"],
                    "body": "Please return the permission slip.",
                }
            ]
        }
        with patch.object(gf.gogcli, "run", return_value=gog_response):
            emails = gf.fetch_emails("tok", datetime(2026, 5, 1, tzinfo=timezone.utc))
        assert len(emails) == 1
        e = emails[0]
        assert e["id"] == "abc123"
        assert e["subject"] == "Field trip permission"
        assert e["sender"] == "school@example.org"
        assert e["date"] == "2026-05-03 13:10"
        assert e["labels"] == {"INBOX", "UNREAD"}
        assert "permission slip" in e["body"]

    def test_caps_body_length(self):
        long_body = "x" * (gf.BODY_MAX_CHARS + 100)
        gog_response = {"messages": [{"id": "id1", "body": long_body}]}
        with patch.object(gf.gogcli, "run", return_value=gog_response):
            emails = gf.fetch_emails("tok", datetime(2026, 5, 1, tzinfo=timezone.utc))
        assert len(emails[0]["body"]) == gf.BODY_MAX_CHARS

    def test_missing_subject_falls_back(self):
        gog_response = {"messages": [{"id": "id1"}]}
        with patch.object(gf.gogcli, "run", return_value=gog_response):
            emails = gf.fetch_emails("tok", datetime(2026, 5, 1, tzinfo=timezone.utc))
        assert emails[0]["subject"] == "(no subject)"
        assert emails[0]["labels"] == set()
        assert emails[0]["body"] == ""

    def test_empty_response_returns_empty_list(self):
        with patch.object(gf.gogcli, "run", return_value={}):
            emails = gf.fetch_emails("tok", datetime(2026, 5, 1, tzinfo=timezone.utc))
        assert emails == []

    def test_argv_uses_after_query_and_full_body(self):
        captured = {}

        def fake_run(token, *args):
            captured["token"] = token
            captured["args"] = list(args)
            return {"messages": []}

        since = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(gf.gogcli, "run", side_effect=fake_run):
            gf.fetch_emails("fresh-token", since)

        assert captured["token"] == "fresh-token"
        argv = captured["args"]
        assert argv[:3] == ["gmail", "messages", "search"]
        # Query string is `after:<epoch>` — Gmail search syntax.
        epoch = int(since.timestamp())
        assert f"after:{epoch}" in argv
        assert "--max=50" in argv
        assert "--include-body" in argv
        assert "--full" in argv
        assert "--body-format=text" in argv

    def test_labels_become_set_for_skip_check(self):
        """Auto-skip relies on `email['labels'] & AUTO_SKIP_LABELS` — must be a set."""
        gog_response = {
            "messages": [{"id": "id1", "labels": ["CATEGORY_PROMOTIONS", "INBOX"]}]
        }
        with patch.object(gf.gogcli, "run", return_value=gog_response):
            emails = gf.fetch_emails("tok", datetime(2026, 5, 1, tzinfo=timezone.utc))
        assert isinstance(emails[0]["labels"], set)
        assert gf.should_auto_skip(emails[0]) is True


class TestGetAccessToken:
    def test_returns_token(self):
        mock_creds = MagicMock()
        mock_creds.scopes = [gf.GMAIL_READONLY_SCOPE]
        mock_creds.token = "live-token"
        with patch.object(gf, "load_google_credentials", return_value=mock_creds):
            assert gf.get_access_token("primary") == "live-token"

    def test_raises_permission_error_on_missing_scope(self):
        mock_creds = MagicMock()
        mock_creds.scopes = ["https://www.googleapis.com/auth/calendar"]
        mock_creds.token = "tok"
        with patch.object(gf, "load_google_credentials", return_value=mock_creds):
            with pytest.raises(PermissionError):
                gf.get_access_token("primary")


class TestMainSkipGate:
    """The new SKIP-on-no-token gate at the top of main() — same shape as
    morning_briefing / plaid_balance_check. The heartbeat handler treats
    `SKIP:` output as 'no message this turn'."""

    def test_skips_when_google_not_connected(self, monkeypatch, capsys):
        monkeypatch.setattr(gf, "has_google_token", lambda *a, **kw: False)

        def _explode(*a, **kw):
            raise AssertionError("must not be called when Google is not connected")

        monkeypatch.setattr(gf, "get_access_token", _explode)
        monkeypatch.setattr(gf, "fetch_emails", _explode)
        monkeypatch.setattr(sys, "argv", ["gmail_fetch.py"])

        gf.main()
        out = capsys.readouterr().out
        assert out.startswith("SKIP:")
        assert "Google not connected" in out

    def test_skip_check_honors_account_arg(self, monkeypatch, capsys):
        """`--account homer` should consult has_google_token('homer'), not
        the default — otherwise a tenant with primary linked but homer
        unlinked would silently fall through and hit a token error."""
        seen = []
        monkeypatch.setattr(gf, "has_google_token",
                            lambda account="primary": seen.append(account) or False)
        monkeypatch.setattr(sys, "argv", ["gmail_fetch.py", "--account", "homer"])

        gf.main()
        assert seen == ["homer"]
        assert capsys.readouterr().out.startswith("SKIP:")


# ── html_to_text() ───────────────────────────────────────────────────────────

class TestHtmlToText:
    """Regression coverage for homer-portal#183: forwarded HTML-only emails
    used to blow the BODY_MAX_CHARS budget on markup before any actionable
    content was visible to the classifier."""

    def test_plain_text_passes_through(self):
        # Non-HTML bodies (the common case when source has a text/plain part)
        # must round-trip unchanged so we don't perturb existing behaviour.
        text = "Hi,\n\nYour package has shipped.\n\nThanks!"
        assert gf.html_to_text(text) == text

    def test_empty_input_passes_through(self):
        assert gf.html_to_text("") == ""

    def test_strips_basic_html(self):
        out = gf.html_to_text("<html><body><p>Hello <b>world</b></p></body></html>")
        assert "Hello" in out
        assert "world" in out
        assert "<" not in out

    def test_drops_style_and_script_contents(self):
        # The MSO/CSS bloat in iOS-forwarded emails was the original budget killer.
        body = (
            "<html><head><style>body { color: red; } @media print { p {} }</style>"
            "<script>var x = 1;</script></head>"
            "<body><p>Visible text</p></body></html>"
        )
        out = gf.html_to_text(body)
        assert "Visible text" in out
        assert "color: red" not in out
        assert "var x" not in out

    def test_void_meta_link_does_not_swallow_body(self):
        # Regression: void elements (no closing tag) previously left the
        # parser in skip-mode forever, dropping the entire body.
        body = (
            "<html><head><meta charset=\"utf-8\"><link rel=\"x\" href=\"y\">"
            "<title>Ignore me</title></head>"
            "<body><meta name=\"weird\"><p>Keep me</p></body></html>"
        )
        out = gf.html_to_text(body)
        assert "Keep me" in out
        assert "Ignore me" not in out

    def test_ios_forwarded_blockquote_preserves_inner_content(self):
        # Mimics the failure mode from homer-portal#183: iPhone Mail forwards
        # as HTML-only, wraps the original in <blockquote>, and prepends a
        # large <style> block with MSO compat hacks.
        body = (
            "<html><head><meta http-equiv=\"content-type\" content=\"text/html\"></head>"
            "<body dir=\"auto\"><div>Sent from my iPhone</div>"
            "<blockquote type=\"cite\"><div>"
            "<title></title><meta http-equiv=\"X\" content=\"y\">"
            "<style>" + ("a { color: red; } " * 50) + "</style>"
            "<p>Almira, your appointment is coming up</p>"
            "<p>May 15, 2026 at 4:00 PM EDT</p>"
            "</div></blockquote></body></html>"
        )
        out = gf.html_to_text(body)
        # The original raw body is well over 1KB; stripping must shrink it
        # AND surface the appointment details before any cap can clip them.
        assert len(out) < 500, f"strip should drop the bulk of the markup: {len(out)} chars"
        assert "Almira" in out
        assert "May 15, 2026 at 4:00 PM EDT" in out
        assert "color: red" not in out
        assert "Almira" in out[:300], "appointment data must appear early in stripped body"

    def test_html_entities_decoded(self):
        out = gf.html_to_text("<p>R&amp;D &lt;at&gt; ACME &nbsp; Inc</p>")
        assert "&amp;" not in out
        assert "&lt;" not in out
        assert "R&D" in out
        assert "<at>" in out

    def test_collapses_whitespace_but_keeps_paragraph_breaks(self):
        out = gf.html_to_text("<p>One   word</p><p>Two   words</p>")
        assert "One word" in out
        assert "Two words" in out
        assert "\n" in out


# ── --has-unread pre-check ─────────────────────────────────────────────────────

class TestHasUnreadPreCheck:
    """Tests for the `gmail_fetch.py --has-unread` heartbeat pre-check.

    Contract (matches nanobot.heartbeat.service:_run_pre_check_command):
    - Non-empty stdout → heartbeat runs the LLM task.
    - Empty stdout → heartbeat skips the task.
    - Exit code 0 on success regardless; non-zero only on hard failure.

    These tests pin the contract end-to-end: account enumeration, the
    auto-skip filter (so an all-promotions inbox skips), short-circuit
    on first actionable hit, and the fail-open behaviour on errors.
    """

    def _ns(self, account=None):
        """Build an argparse.Namespace for `_cmd_has_unread` calls.

        `account` defaults to `gf.DEFAULT_ACCOUNT` to mirror argparse's
        production default — the impl always fans out across linked
        accounts regardless of --account in pre-check mode.
        """
        import argparse
        return argparse.Namespace(
            account=account if account is not None else gf.DEFAULT_ACCOUNT,
            has_unread=True, dry_run=False, hours=None, min_interval=60,
        )

    def test_no_accounts_skips_silently(self, capsys):
        with patch.object(gf, "has_google_token", return_value=False), \
             patch("accounts.list_valid_accounts", return_value=[]):
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert out == "", "no linked accounts → skip with empty stdout"

    def test_no_new_mail_skips(self, capsys):
        """gogcli returns zero messages — pre-check prints nothing
        (heartbeat skips). No LLM call, no chat noise, no cost.
        """
        with patch.object(gf, "has_google_token", return_value=True), \
             patch.object(gf, "get_access_token", return_value="tok"), \
             patch("accounts.list_valid_accounts", return_value=["primary"]), \
             patch.object(gf, "gogcli") as mock_gogcli:
            mock_gogcli.run.return_value = {"messages": []}
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert out == ""

    def test_all_auto_skip_labels_filtered_out_skips(self, capsys):
        """Every new message is PROMOTIONS / SOCIAL / FORUMS — the
        main path would auto-skip all of them. Pre-check matches that
        and skips, so the LLM never runs on a noise-only batch.
        """
        with patch.object(gf, "has_google_token", return_value=True), \
             patch.object(gf, "get_access_token", return_value="tok"), \
             patch("accounts.list_valid_accounts", return_value=["primary"]), \
             patch.object(gf, "gogcli") as mock_gogcli:
            mock_gogcli.run.return_value = {"messages": [
                {"id": "1", "labels": ["CATEGORY_PROMOTIONS"]},
                {"id": "2", "labels": ["CATEGORY_SOCIAL", "UNREAD"]},
                {"id": "3", "labels": ["CATEGORY_FORUMS"]},
            ]}
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert out == "", "all auto-skip → empty stdout"

    def test_one_actionable_among_promotions_triggers_run(self, capsys):
        """At least one non-auto-skip message exists — pre-check prints
        OK so the heartbeat runs the full classification pipeline.
        """
        with patch.object(gf, "has_google_token", return_value=True), \
             patch.object(gf, "get_access_token", return_value="tok"), \
             patch("accounts.list_valid_accounts", return_value=["primary"]), \
             patch.object(gf, "gogcli") as mock_gogcli:
            mock_gogcli.run.return_value = {"messages": [
                {"id": "1", "labels": ["CATEGORY_PROMOTIONS"]},
                {"id": "2", "labels": ["INBOX", "UNREAD"]},  # actionable
                {"id": "3", "labels": ["CATEGORY_SOCIAL"]},
            ]}
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("OK:"), f"actionable mail → OK signal, got: {out!r}"
        assert "primary" in out

    def test_fans_out_across_multiple_accounts(self, capsys):
        """Pre-check fans out across every linked account. First account
        with actionable mail wins — no need to scan the rest.
        """
        calls = []

        def _fake_run(token, *args):
            account = token  # we pass account name as "token" in this mock
            calls.append(account)
            if account == "personal":
                return {"messages": [{"id": "1", "labels": ["INBOX"]}]}
            return {"messages": []}  # primary is empty

        with patch.object(gf, "has_google_token", return_value=True), \
             patch.object(gf, "get_access_token", side_effect=lambda a: a), \
             patch("accounts.list_valid_accounts", return_value=["primary", "personal"]), \
             patch.object(gf, "gogcli") as mock_gogcli:
            mock_gogcli.run.side_effect = _fake_run
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert "personal" in out
        # Both accounts should have been checked (primary first, then personal).
        assert calls == ["primary", "personal"]

    def test_broken_token_for_one_account_does_not_break_check(self, capsys):
        """A stale/broken token on one account must not silence the
        check across the others. Don't fail-closed on a single bad
        account — fan-out continues.
        """
        def _token(account):
            if account == "primary":
                raise RuntimeError("token expired")
            return "tok"

        with patch.object(gf, "has_google_token", return_value=True), \
             patch.object(gf, "get_access_token", side_effect=_token), \
             patch("accounts.list_valid_accounts", return_value=["primary", "personal"]), \
             patch.object(gf, "gogcli") as mock_gogcli:
            mock_gogcli.run.return_value = {"messages": [{"id": "1", "labels": ["INBOX"]}]}
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert "personal" in out, "stale primary should not silence personal's signal"

    def test_api_error_fails_open(self, capsys):
        """Network blip / gogcli error → pre-check fails open (prints
        OK), so the agent-side scan runs and reports the real error
        with full context. Better than silently dropping the task on
        every transient failure.
        """
        with patch.object(gf, "has_google_token", return_value=True), \
             patch.object(gf, "get_access_token", return_value="tok"), \
             patch("accounts.list_valid_accounts", return_value=["primary"]), \
             patch.object(gf, "gogcli") as mock_gogcli:
            mock_gogcli.run.side_effect = RuntimeError("api down")
            code = gf._cmd_has_unread(self._ns())
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("OK:"), "transient error → fail open with OK"

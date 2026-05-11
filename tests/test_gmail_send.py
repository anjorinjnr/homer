"""
Tests for gmail_send.py — gogcli wrapper, policy guards, draft approval flow.
Also covers google_auth.py credential helpers (get_token_path, load_gmail_credentials).

All tests use mocks — no real API calls, no credentials needed.
"""

import json
import pickle
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import gmail_send
import google_auth


# ── Token helpers (google_auth.py) ───────────────────────────────────────────


class TestGetTokenPath:
    def test_default_account(self):
        path = google_auth.get_token_path()
        assert path == google_auth.TOKENS_DIR / "primary.pickle"

    def test_custom_account(self):
        path = google_auth.get_token_path("homer")
        assert path == google_auth.TOKENS_DIR / "homer.pickle"

    def test_another_account(self):
        path = google_auth.get_token_path("work")
        assert path == google_auth.TOKENS_DIR / "work.pickle"


class TestLoadCredentialsLegacyMigration:
    def test_migrates_legacy_token(self, tmp_path):
        """When primary token doesn't exist at new path but legacy exists, copies it."""
        tokens_dir = tmp_path / "tokens"
        legacy_path = tmp_path / "google_token.pickle"

        fake_creds = MagicMock()
        fake_creds.expired = False
        fake_creds.valid = True
        with open(legacy_path, "wb") as f:
            pickle.dump("sentinel", f)

        with patch.object(google_auth, "TOKENS_DIR", tokens_dir), \
             patch.object(google_auth, "LEGACY_TOKEN", legacy_path), \
             patch("google_auth.get_token_path", return_value=tokens_dir / "primary.pickle"), \
             patch("pickle.load", return_value=fake_creds):
            creds = google_auth.load_gmail_credentials("primary")

        assert (tokens_dir / "primary.pickle").exists()
        assert creds.expired is False

    def test_no_migration_for_other_accounts(self, tmp_path):
        tokens_dir = tmp_path / "tokens"
        legacy_path = tmp_path / "google_token.pickle"
        with open(legacy_path, "wb") as f:
            pickle.dump("sentinel", f)

        with patch.object(google_auth, "TOKENS_DIR", tokens_dir), \
             patch.object(google_auth, "LEGACY_TOKEN", legacy_path), \
             patch("google_auth.get_token_path", return_value=tokens_dir / "homer.pickle"):
            with pytest.raises(FileNotFoundError, match="homer"):
                google_auth.load_gmail_credentials("homer")


class TestLoadCredentialsMissing:
    def test_raises_file_not_found(self, tmp_path):
        tokens_dir = tmp_path / "tokens"
        token_path = tokens_dir / "missing.pickle"
        with patch.object(google_auth, "TOKENS_DIR", tokens_dir), \
             patch.object(google_auth, "LEGACY_TOKEN", tmp_path / "nope.pickle"), \
             patch("google_auth.get_token_path", return_value=token_path):
            with pytest.raises(FileNotFoundError, match="Token not found"):
                google_auth.load_gmail_credentials("missing")


class TestLoadCredentialsAutoRefresh:
    def test_refreshes_expired_token(self, tmp_path):
        tokens_dir = tmp_path / "tokens"
        tokens_dir.mkdir()
        token_path = tokens_dir / "test.pickle"

        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "rt_123"
        with open(token_path, "wb") as f:
            pickle.dump("sentinel", f)

        mock_request = MagicMock()
        saved_objects = []

        def track_dump(obj, f):
            saved_objects.append(obj)

        with patch.object(google_auth, "TOKENS_DIR", tokens_dir), \
             patch("google_auth.get_token_path", return_value=token_path), \
             patch("google.auth.transport.requests.Request", return_value=mock_request), \
             patch("pickle.load", return_value=fake_creds), \
             patch("pickle.dump", side_effect=track_dump):
            creds = google_auth.load_gmail_credentials("test")

        creds.refresh.assert_called_once_with(mock_request)
        assert len(saved_objects) == 1
        assert saved_objects[0] is creds


# ── from_address_for ─────────────────────────────────────────────────────────


class TestFromAddressFor:
    def test_homer_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert gmail_send.from_address_for("homer") == "homer@example.com"

    def test_homer_env_override(self):
        with patch.dict("os.environ", {"HOMER_EMAIL_ADDRESS": "bot@example.com"}, clear=True):
            assert gmail_send.from_address_for("homer") == "bot@example.com"

    def test_primary_returns_none(self):
        assert gmail_send.from_address_for("primary") is None

    def test_other_account_returns_none(self):
        assert gmail_send.from_address_for("work") is None


# ── gogcli wiring helpers ────────────────────────────────────────────────────


def _patch_token_and_gogcli(creds_token: str = "live-token", run_return=None, **patch_creds):
    """Build a context that stubs auth + gogcli.run.

    Returns (creds_patch, gogcli_patch). Caller composes them with `with` /
    `monkeypatch` as needed.
    """
    mock_creds = MagicMock()
    mock_creds.token = creds_token
    mock_creds.scopes = [gmail_send.GMAIL_SEND_SCOPE, gmail_send.GMAIL_COMPOSE_SCOPE]
    return mock_creds


def _run_main(argv: list[str]):
    """Run gmail_send.main with sys.argv patched to argv."""
    with patch("sys.argv", argv):
        gmail_send.main()


def _stubbed_run(side_effect=None, returns=None):
    """Return a mock for gogcli.run."""
    if side_effect:
        return patch.object(gmail_send.gogcli, "run", side_effect=side_effect)
    return patch.object(gmail_send.gogcli, "run", return_value=returns or {})


def _stubbed_token(token: str = "live-token", scopes=None):
    mock_creds = MagicMock()
    mock_creds.token = token
    mock_creds.scopes = scopes or [gmail_send.GMAIL_SEND_SCOPE, gmail_send.GMAIL_COMPOSE_SCOPE]
    return patch.object(gmail_send, "load_google_credentials", return_value=mock_creds)


# ── send subcommand ──────────────────────────────────────────────────────────


class TestSendEmail:
    def test_send_invokes_gogcli_send(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "alice@example.com")
        captured = {}

        def fake_run(token, *args):
            captured["token"] = token
            captured["args"] = list(args)
            return {"messageId": "msg123", "threadId": "thr1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--subject", "Hello", "--body", "Hi"])

        assert captured["token"] == "live-token"
        argv = captured["args"]
        assert argv[:2] == ["gmail", "send"]
        assert "--to" in argv and "alice@example.com" in argv
        assert "--subject" in argv and "Hello" in argv
        assert "--body" in argv and "Hi" in argv
        # account=homer (default) → --from homer@example.com (or HOMER_EMAIL_ADDRESS override)
        assert "--from" in argv

        output = json.loads(capsys.readouterr().out.strip())
        assert output == {"status": "sent", "message_id": "msg123",
                          "to": "alice@example.com", "subject": "Hello"}

    def test_send_primary_account_omits_from(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "alice@example.com")
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "--account", "primary", "send",
                       "--to", "alice@example.com",
                       "--subject", "Hi", "--body", "Body"])

        assert "--from" not in captured["args"]

    def test_send_with_cc_bcc(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--cc", "carl@example.com", "--bcc", "blind@example.com",
                       "--subject", "S", "--body", "B"])

        argv = captured["args"]
        assert "--cc" in argv and "carl@example.com" in argv
        assert "--bcc" in argv and "blind@example.com" in argv


# ── draft create / update / send / delete ────────────────────────────────────


class TestDraftCreate:
    def test_draft_invokes_gogcli_drafts_create(self, capsys):
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"draftId": "draft456", "threadId": "thr1"}

        approval = {"approval_id": "appr-1", "status": "pending", "draft_id": "draft456"}
        with _stubbed_token(), _stubbed_run(side_effect=fake_run), \
             patch.object(gmail_send, "create_approval", return_value=approval):
            _run_main(["gmail_send.py", "draft", "--to", "alice@example.com",
                       "--subject", "Hello", "--body", "Hi"])

        assert captured["args"][:3] == ["gmail", "drafts", "create"]
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "drafted"
        assert out["draft_id"] == "draft456"
        assert out["approval_id"] == "appr-1"
        assert "approval_url" in out


class TestDraftUpdate:
    def test_draft_update_resets_approval(self, capsys):
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"draftId": "draft456"}

        approval = {"approval_id": "appr-2", "status": "pending", "draft_id": "draft456"}
        with _stubbed_token(), _stubbed_run(side_effect=fake_run), \
             patch.object(gmail_send, "create_approval", return_value=approval) as mock_create:
            _run_main(["gmail_send.py", "draft-update", "--draft-id", "draft456",
                       "--to", "alice@example.com", "--subject", "Updated",
                       "--body", "New body"])

        assert captured["args"][:4] == ["gmail", "drafts", "update", "draft456"]
        mock_create.assert_called_once()
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "updated"
        assert out["approval_id"] == "appr-2"


class TestDraftSend:
    def test_with_approval_invokes_gogcli(self, capsys):
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"messageId": "msgX", "threadId": "thrX"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run), \
             patch.object(gmail_send, "check_approval",
                          return_value={"status": "approved", "approval_id": "a1"}), \
             patch.object(gmail_send, "mark_sent") as mock_mark:
            _run_main(["gmail_send.py", "draft-send", "--draft-id", "draft789"])

        assert captured["args"] == ["gmail", "drafts", "send", "draft789"]
        mock_mark.assert_called_once()
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "draft_sent"
        assert out["message_id"] == "msgX"

    def test_rejects_unapproved(self, capsys):
        with _stubbed_token(), _stubbed_run(returns={}), \
             patch.object(gmail_send, "check_approval",
                          return_value={"status": "pending", "approval_id": "a1"}):
            with pytest.raises(SystemExit) as exc_info:
                _run_main(["gmail_send.py", "draft-send", "--draft-id", "draft789"])
            assert exc_info.value.code == 1
        out = json.loads(capsys.readouterr().out.strip())
        assert "not approved" in out["error"].lower()

    def test_rejects_unknown_draft(self, capsys):
        with _stubbed_token(), _stubbed_run(returns={}), \
             patch.object(gmail_send, "check_approval", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                _run_main(["gmail_send.py", "draft-send", "--draft-id", "unknown"])
            assert exc_info.value.code == 1
        out = json.loads(capsys.readouterr().out.strip())
        assert "not approved" in out["error"].lower()


class TestDraftDelete:
    def test_delete_calls_gogcli(self, capsys):
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"deleted": True, "draftId": "draft999"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run), \
             patch.object(gmail_send, "check_approval", return_value=None):
            _run_main(["gmail_send.py", "draft-delete", "--draft-id", "draft999"])

        assert captured["args"] == ["gmail", "drafts", "delete", "-y", "draft999"]
        out = json.loads(capsys.readouterr().out.strip())
        assert out == {"status": "deleted", "draft_id": "draft999"}

    def test_delete_rejects_pending_approval(self, capsys):
        from email_approval_store import reject as reject_approval
        with _stubbed_token(), _stubbed_run(returns={}), \
             patch.object(gmail_send, "check_approval",
                          return_value={"status": "pending", "approval_id": "appr-3"}), \
             patch("email_approval_store.reject") as mock_reject:
            _run_main(["gmail_send.py", "draft-delete", "--draft-id", "draft999"])
        mock_reject.assert_called_once_with("appr-3", "draft-delete")


# ── reply threading ──────────────────────────────────────────────────────────


class TestReplyToThreading:
    def test_reply_passes_message_id_to_gogcli(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        captured = {"calls": []}

        def fake_run(token, *args):
            captured["calls"].append(list(args))
            argv = list(args)
            # gmail get → return a metadata payload with Subject header
            if argv[:2] == ["gmail", "get"]:
                return {"message": {"payload": {"headers": [
                    {"name": "Subject", "value": "Original Subject"},
                ]}}}
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--body", "reply text", "--reply-to", "orig_msg_id"])

        # Should have made: gmail get (subject lookup) + gmail send
        send_argv = next(c for c in captured["calls"] if c[:2] == ["gmail", "send"])
        assert "--reply-to-message-id" in send_argv
        assert "orig_msg_id" in send_argv
        # Subject auto-derived from original with Re: prefix.
        idx = send_argv.index("--subject")
        assert send_argv[idx + 1] == "Re: Original Subject"

    def test_reply_preserves_re_prefix(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        captured = {"calls": []}

        def fake_run(token, *args):
            captured["calls"].append(list(args))
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--subject", "Re: already prefixed", "--body", "reply",
                       "--reply-to", "orig_msg_id"])

        send_argv = next(c for c in captured["calls"] if c[:2] == ["gmail", "send"])
        idx = send_argv.index("--subject")
        assert send_argv[idx + 1] == "Re: already prefixed"

    def test_reply_skips_subject_lookup_when_subject_provided(self, capsys, monkeypatch):
        """When user supplies a subject, no `gmail get` call is needed."""
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        captured = {"calls": []}

        def fake_run(token, *args):
            captured["calls"].append(list(args))
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--subject", "Custom subject", "--body", "reply",
                       "--reply-to", "orig_msg_id"])

        # No gmail get call — only gmail send.
        get_calls = [c for c in captured["calls"] if c[:2] == ["gmail", "get"]]
        assert get_calls == []


# ── Error handling ───────────────────────────────────────────────────────────


class TestErrorHandling:
    def test_missing_to_for_send(self):
        with patch("sys.argv", ["gmail_send.py", "send", "--subject", "Hello", "--body", "Hi"]):
            with pytest.raises(SystemExit) as exc_info:
                gmail_send.main()
            assert exc_info.value.code == 2

    def test_missing_subcommand(self):
        with patch("sys.argv", ["gmail_send.py"]):
            with pytest.raises(SystemExit) as exc_info:
                gmail_send.main()
            assert exc_info.value.code == 2

    def test_gogcli_failure_emits_error_json(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        with _stubbed_token(), \
             _stubbed_run(side_effect=RuntimeError("API quota exceeded")):
            with pytest.raises(SystemExit) as exc_info:
                _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                           "--subject", "Hello", "--body", "Hi"])
            assert exc_info.value.code == 1
        out = json.loads(capsys.readouterr().out.strip())
        assert "API quota exceeded" in out["error"]


# ── External guard ───────────────────────────────────────────────────────────


class TestExternalGuard:
    def _expect_blocked(self, argv: list[str], capsys):
        with _stubbed_token(), _stubbed_run(returns={}) as mock_run:
            with pytest.raises(SystemExit) as exc_info:
                _run_main(argv)
            assert exc_info.value.code == 1
            mock_run.assert_not_called()
        return json.loads(capsys.readouterr().out.strip())

    def test_send_blocks_external_recipient(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "alice@internal.com,@household.org")
        out = self._expect_blocked(
            ["gmail_send.py", "send", "--to", "vendor@external.com",
             "--subject", "Hello", "--body", "Hi"],
            capsys,
        )
        assert "vendor@external.com" in out["error"]

    def test_send_allows_internal_by_address(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "alice@internal.com")
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@internal.com",
                       "--subject", "Hello", "--body", "Hi"])
        assert captured["args"][:2] == ["gmail", "send"]

    def test_send_allows_internal_by_domain(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@household.org")
        with _stubbed_token(), _stubbed_run(returns={"messageId": "m"}):
            _run_main(["gmail_send.py", "send", "--to", "anyone@household.org",
                       "--subject", "Hello", "--body", "Hi"])
        # No SystemExit raised — internal address allowed.

    def test_send_blocks_when_env_unset(self, capsys, monkeypatch):
        monkeypatch.delenv("HOMER_INTERNAL_EMAILS", raising=False)
        out = self._expect_blocked(
            ["gmail_send.py", "send", "--to", "anyone@example.com",
             "--subject", "Hello", "--body", "Hi"],
            capsys,
        )
        assert "HOMER_INTERNAL_EMAILS" in out["error"]

    def test_send_blocks_external_cc(self, capsys, monkeypatch):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "alice@internal.com")
        out = self._expect_blocked(
            ["gmail_send.py", "send", "--to", "alice@internal.com",
             "--cc", "vendor@external.com",
             "--subject", "Hello", "--body", "Hi"],
            capsys,
        )
        assert "vendor@external.com" in out["error"]

    def test_draft_allows_external(self, capsys, monkeypatch):
        monkeypatch.delenv("HOMER_INTERNAL_EMAILS", raising=False)
        approval = {"approval_id": "a1", "status": "pending", "draft_id": "d1"}
        with _stubbed_token(), _stubbed_run(returns={"draftId": "d1"}), \
             patch.object(gmail_send, "create_approval", return_value=approval):
            _run_main(["gmail_send.py", "draft", "--to", "vendor@external.com",
                       "--subject", "Hello", "--body", "Hi"])
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "drafted"


# ── Body file ────────────────────────────────────────────────────────────────


class TestBodyFile:
    def test_body_file_passes_path_through(self, capsys, monkeypatch, tmp_path):
        """--body-file path is forwarded to gogcli's --body-file (not read inline)."""
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
        body_file = tmp_path / "email_body.txt"
        body_file.write_text("Hello from a file!\nSecond paragraph.", encoding="utf-8")

        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--subject", "Test", "--body-file", str(body_file)])

        argv = captured["args"]
        # gogcli receives --body-file <path>, not --body <content>: the body
        # never travels through subprocess argv.
        assert "--body" not in argv
        assert "--body-file" in argv
        idx = argv.index("--body-file")
        assert argv[idx + 1] == str(body_file.resolve())

    def test_inline_body_uses_body_flag(self, capsys, monkeypatch):
        """--body (inline) still uses gogcli's --body flag, not --body-file."""
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        captured = {}

        def fake_run(token, *args):
            captured["args"] = list(args)
            return {"messageId": "m1"}

        with _stubbed_token(), _stubbed_run(side_effect=fake_run):
            _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                       "--subject", "Test", "--body", "Hello inline"])

        argv = captured["args"]
        assert "--body-file" not in argv
        idx = argv.index("--body")
        assert argv[idx + 1] == "Hello inline"

    def test_body_file_used_for_approval_preview(self, capsys, monkeypatch, tmp_path):
        """Draft uses the file's full content for the approval-store body field."""
        monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
        body_file = tmp_path / "draft.txt"
        body_file.write_text("Sensitive content for preview", encoding="utf-8")

        captured_preview = {}

        def fake_create(**kwargs):
            captured_preview["body_preview"] = kwargs["body_preview"]
            return {"approval_id": "a1", "status": "pending", "draft_id": "d1"}

        with _stubbed_token(), _stubbed_run(returns={"draftId": "d1"}), \
             patch.object(gmail_send, "create_approval", side_effect=fake_create):
            _run_main(["gmail_send.py", "draft", "--to", "alice@example.com",
                       "--subject", "Test", "--body-file", str(body_file)])

        assert captured_preview["body_preview"] == "Sensitive content for preview"

    def test_long_body_passed_in_full_to_approval(self, capsys, monkeypatch, tmp_path):
        """HIL gets the full body, not a truncated preview — long content survives."""
        monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
        body_file = tmp_path / "long.txt"
        long_text = "Paragraph 1\n" + ("X" * 1000) + "\n--mid--\n" + ("Y" * 1000)
        body_file.write_text(long_text, encoding="utf-8")

        captured = {}

        def fake_create(**kwargs):
            captured["body_preview"] = kwargs["body_preview"]
            return {"approval_id": "a1", "status": "pending", "draft_id": "d1"}

        with _stubbed_token(), _stubbed_run(returns={"draftId": "d1"}), \
             patch.object(gmail_send, "create_approval", side_effect=fake_create):
            _run_main(["gmail_send.py", "draft", "--to", "alice@example.com",
                       "--subject", "Long", "--body-file", str(body_file)])

        assert captured["body_preview"] == long_text
        assert "--mid--" in captured["body_preview"]
        assert len(captured["body_preview"]) > 500  # explicitly past the old cap

    def test_body_file_rejects_outside_workspace(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setenv("HOMER_INTERNAL_EMAILS", "@example.com")
        monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path / "workspace"))
        (tmp_path / "workspace").mkdir()
        secrets_file = tmp_path / "secrets" / ".env"
        secrets_file.parent.mkdir()
        secrets_file.write_text("SECRET=leaked", encoding="utf-8")

        with _stubbed_token(), _stubbed_run(returns={}) as mock_run:
            with pytest.raises(SystemExit) as exc_info:
                _run_main(["gmail_send.py", "send", "--to", "alice@example.com",
                           "--subject", "Test", "--body-file", str(secrets_file)])
            assert exc_info.value.code == 1
            mock_run.assert_not_called()

        out = json.loads(capsys.readouterr().out.strip())
        assert "must be inside the workspace" in out["error"]

    def test_body_and_body_file_mutually_exclusive(self, tmp_path):
        body_file = tmp_path / "body.txt"
        body_file.write_text("content", encoding="utf-8")
        with patch("sys.argv", ["gmail_send.py", "send", "--to", "a@b.com",
                                "--body", "inline", "--body-file", str(body_file)]):
            with pytest.raises(SystemExit) as exc_info:
                gmail_send.main()
            assert exc_info.value.code == 2


# ── fetch_subject / derive_reply_subject ────────────────────────────────────


class TestFetchSubject:
    def test_extracts_subject(self):
        with patch.object(gmail_send.gogcli, "run", return_value={
            "message": {"payload": {"headers": [
                {"name": "Subject", "value": "Original Subject"},
                {"name": "From", "value": "x@y.z"},
            ]}}
        }):
            assert gmail_send.fetch_subject("tok", "msg1") == "Original Subject"

    def test_missing_subject_returns_empty(self):
        with patch.object(gmail_send.gogcli, "run", return_value={
            "message": {"payload": {"headers": []}}
        }):
            assert gmail_send.fetch_subject("tok", "msg1") == ""

    def test_case_insensitive_header_name(self):
        """gogcli may return lowercase / normalized header names."""
        with patch.object(gmail_send.gogcli, "run", return_value={
            "message": {"payload": {"headers": [
                {"name": "subject", "value": "Hi"},
            ]}}
        }):
            assert gmail_send.fetch_subject("tok", "msg1") == "Hi"


class TestDeriveReplySubject:
    def test_provided_subject_with_re_unchanged(self):
        with patch.object(gmail_send, "fetch_subject", side_effect=AssertionError("must not fetch")):
            assert gmail_send.derive_reply_subject("tok", "Re: Hello", "id1") == "Re: Hello"

    def test_provided_subject_gets_re_prefix(self):
        with patch.object(gmail_send, "fetch_subject", side_effect=AssertionError("must not fetch")):
            assert gmail_send.derive_reply_subject("tok", "Hello", "id1") == "Re: Hello"

    def test_blank_subject_pulls_from_original(self):
        with patch.object(gmail_send, "fetch_subject", return_value="Original"):
            assert gmail_send.derive_reply_subject("tok", "", "id1") == "Re: Original"

    def test_blank_subject_with_re_original_unchanged(self):
        with patch.object(gmail_send, "fetch_subject", return_value="Re: Already"):
            assert gmail_send.derive_reply_subject("tok", "", "id1") == "Re: Already"

    def test_blank_subject_with_blank_original_falls_back(self):
        """Original message has no Subject header — don't send empty subject."""
        with patch.object(gmail_send, "fetch_subject", return_value=""):
            assert gmail_send.derive_reply_subject("tok", "", "id1") == "(no subject)"

    def test_lookup_failure_falls_back(self):
        """If fetch_subject raises (deleted message, network blip), recover."""
        with patch.object(gmail_send, "fetch_subject",
                          side_effect=RuntimeError("gogcli failed: 404 not found")):
            # Threading still works via --reply-to-message-id; subject just defaults.
            assert gmail_send.derive_reply_subject("tok", "", "id1") == "(no subject)"

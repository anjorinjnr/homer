"""
Tests for tools/feedback_submit.py — covers anonymization, session excerpting,
issue assembly, and the dry-run CLI path. Network is never touched.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import feedback_submit as fs


def make_workspace(tmp, user_md_text=None, session_records=None, session_name="session.jsonl"):
    ws = Path(tmp) / "ws"
    (ws / "sessions").mkdir(parents=True)
    if user_md_text is not None:
        (ws / "USER.md").write_text(user_md_text, encoding="utf-8")
    session_path = None
    if session_records is not None:
        session_path = ws / "sessions" / session_name
        session_path.write_text(
            "\n".join(json.dumps(r) for r in session_records) + "\n",
            encoding="utf-8",
        )
    return ws, session_path


def run_cli(args, env=None):
    """Invoke fs.main(args) with optional env overrides; return (rc, parsed_json)."""
    buf = io.StringIO()
    ctx = patch.dict("os.environ", env or {}, clear=False)
    with patch("sys.stdout", buf), ctx:
        rc = fs.main(args)
    return rc, json.loads(buf.getvalue())


# ── Anonymization ────────────────────────────────────────────────────────────

class TestAnonymize(unittest.TestCase):

    def test_redacts_email(self):
        out = fs.anonymize("ping me at jane.doe+homer@example.co.uk later")
        self.assertNotIn("jane.doe", out)
        self.assertIn("<email>", out)

    def test_redacts_phone_variants(self):
        cases = [
            "call 412-555-1212 today",
            "call (412) 555-1212",
            "call +1 412 555 1212",
            "call +14125551212",
        ]
        for c in cases:
            with self.subTest(case=c):
                out = fs.anonymize(c)
                self.assertNotIn("555", out, msg=f"phone digits leaked: {out!r}")
                self.assertIn("<phone>", out)

    def test_phone_re_does_not_match_long_digit_runs(self):
        # Regression: order numbers / event IDs / timestamps should not be
        # redacted as phones. PHONE_RE requires a separator or `+` prefix.
        for s in [
            "order 4125551212 shipped",       # 10 bare digits
            "event_id 17234567890 created",   # 11 bare digits
            "epoch 1714600000 utc",           # 10 digits, ts
        ]:
            with self.subTest(case=s):
                self.assertEqual(fs.anonymize(s), s)

    def test_redacts_household_names(self):
        names = ["Mira", "Jamie Smith"]
        pat = fs.name_pattern_for(names)
        out = fs.anonymize("Mira asked Jamie Smith about dinner", name_pattern=pat)
        self.assertNotIn("Mira", out)
        self.assertNotIn("Jamie", out)
        self.assertEqual(out.count("<name>"), 2)

    def test_short_names_not_substituted(self):
        # 1-char names would clobber English words; helper drops them.
        pat = fs.name_pattern_for(["A", "Bo"])
        # Bo is 2 chars, kept; "A" dropped.
        self.assertIsNotNone(pat)
        # "A" should not match in random text:
        out = fs.anonymize("A short Bo note", name_pattern=pat)
        self.assertIn("A short", out)
        self.assertIn("<name>", out)

    def test_handles_empty_input(self):
        self.assertEqual(fs.anonymize(""), "")
        self.assertIsNone(fs.anonymize(None))

    def test_no_pattern_when_no_names(self):
        self.assertIsNone(fs.name_pattern_for([]))
        self.assertIsNone(fs.name_pattern_for(["", "  "]))

    def test_long_name_redacts_substring_within_word(self):
        # Regression for homer-portal#183: USER.md listed "Mira" as the
        # nickname, but the agent surfaced the full legal name "Almira"
        # from a calendar/email lookup and it leaked through. Single-word
        # 4+ char names redact any word containing them.
        pat = fs.name_pattern_for(["Mira"])
        out = fs.anonymize("Carla forwarded email about Almira's appointment", name_pattern=pat)
        self.assertNotIn("Almira", out)
        self.assertNotIn("Mira", out)
        self.assertIn("<name>", out)

    def test_short_name_does_not_substring_match(self):
        # 2-3 char names stay strict so "Bo" doesn't eat "About", "Bob",
        # "Boy", etc. Substring matching is only safe for 4+ char names.
        pat = fs.name_pattern_for(["Bo"])
        out = fs.anonymize("About Bo bouncing", name_pattern=pat)
        self.assertIn("About", out)
        self.assertIn("bouncing", out)
        self.assertIn("<name>", out)

    def test_multi_word_name_uses_word_boundary(self):
        # Multi-word names don't get substring expansion (the inner space
        # would never match `\w*` anyway). Whole phrase still redacts.
        pat = fs.name_pattern_for(["Mira Smith"])
        out = fs.anonymize("Mira Smith came by", name_pattern=pat)
        self.assertIn("<name>", out)
        self.assertNotIn("Mira Smith", out)


# ── USER.md name extraction ──────────────────────────────────────────────────

class TestLoadHouseholdNames(unittest.TestCase):

    def test_pulls_bullet_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, _ = make_workspace(tmp, user_md_text=(
                "# Household\n"
                "- Jamie (primary): software engineer\n"
                "- Mira, partner — pediatrician\n"
                "- some non-name line\n"
                "Other text without bullet.\n"
            ))
            names = fs.load_household_names(ws)
            self.assertIn("Jamie", names)
            self.assertIn("Mira", names)

    def test_missing_user_md_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "empty"
            ws.mkdir()
            self.assertEqual(fs.load_household_names(ws), [])

    def test_skips_hyphenated_phrases_and_acronyms(self):
        # Regression: "Pre-emergent herbicide", "Monitor HVAC — peak load" used
        # to be picked up as names. Plain `-` after the leader is not a person
        # terminator; all-caps tokens like HVAC are dropped.
        with tempfile.TemporaryDirectory() as tmp:
            ws, _ = make_workspace(tmp, user_md_text=(
                "- Pre-emergent herbicide before soil hits 55°F\n"
                "- Monitor HVAC — peak load season\n"
                "- Mira Smith (born April 29, 2021)\n"
            ))
            names = fs.load_household_names(ws)
            self.assertNotIn("Pre", names)
            self.assertNotIn("Monitor HVAC", names)
            self.assertIn("Mira Smith", names)
            self.assertIn("Mira", names)  # first-token form also added

    def test_handles_literal_backslash_n_in_bullet(self):
        # USER.md sometimes contains a literal "\n" (as-typed two chars) instead
        # of a newline — both leaders should still be picked up.
        with tempfile.TemporaryDirectory() as tmp:
            ws, _ = make_workspace(tmp, user_md_text=(
                "- Mira Smith (born 2021)\\n- Theo Smith (born 2024)\n"
            ))
            names = fs.load_household_names(ws)
            self.assertIn("Mira Smith", names)
            self.assertIn("Theo Smith", names)

    def test_pulls_bold_field_bullets(self):
        # `- **Field**: Name` is the format used for "## Primary user" rows.
        # Regression for homer-portal#183: this was missed entirely.
        with tempfile.TemporaryDirectory() as tmp:
            ws, _ = make_workspace(tmp, user_md_text=(
                "## Primary user\n"
                "- **Name**: Jamie\n"
                "- **Primary user**: Mira Smith\n"
            ))
            names = fs.load_household_names(ws)
            self.assertIn("Jamie", names)
            self.assertIn("Mira Smith", names)
            self.assertIn("Mira", names)

    def test_pulls_bare_bullets_under_person_heading(self):
        # `- Name` with no terminator is only treated as a person row when
        # it follows a person-listing heading (`## Children`, `## Adults`,
        # etc.). Outside those sections we'd over-match generic bullets.
        with tempfile.TemporaryDirectory() as tmp:
            ws, _ = make_workspace(tmp, user_md_text=(
                "## Children\n"
                "- Mira\n"
                "- Theo Smith\n"
                "## Pool\n"
                "- Saltwater\n"   # not a person — must NOT be redacted
            ))
            names = fs.load_household_names(ws)
            self.assertIn("Mira", names)
            self.assertIn("Theo Smith", names)
            self.assertIn("Theo", names)
            self.assertNotIn("Saltwater", names)


class TestLoadUsersYamlNames(unittest.TestCase):

    def test_pulls_names_from_users_yaml(self):
        # Regression for homer-portal#183: `Carla` was listed in
        # context/users.yaml only — not in USER.md — and leaked through.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Path(tmp) / "context"
            ctx.mkdir()
            (ctx / "users.yaml").write_text(
                "users:\n"
                "  - name: Jamie\n"
                "    role: admin\n"
                "  - name: Carla\n"
                "    role: member\n",
                encoding="utf-8",
            )
            names = fs.load_users_yaml_names(ctx)
            self.assertIn("Jamie", names)
            self.assertIn("Carla", names)

    def test_missing_users_yaml_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fs.load_users_yaml_names(Path(tmp)), [])

    def test_malformed_users_yaml_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Path(tmp)
            (ctx / "users.yaml").write_text("not: valid: yaml: ::\n", encoding="utf-8")
            # Either yaml parse error or non-dict result — both should not raise.
            self.assertIsInstance(fs.load_users_yaml_names(ctx), list)

    def test_collect_unions_both_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "ws"
            ws.mkdir()
            (ws / "USER.md").write_text(
                "## Children\n- Mira Smith (born 2021)\n", encoding="utf-8"
            )
            ctx = tmp / "context"
            ctx.mkdir()
            (ctx / "users.yaml").write_text(
                "users:\n  - name: Carla\n    role: member\n", encoding="utf-8"
            )
            names = fs.collect_household_names(ws, ctx)
            self.assertIn("Mira Smith", names)
            self.assertIn("Mira", names)
            self.assertIn("Carla", names)


# ── Session excerpting ───────────────────────────────────────────────────────

class TestExcerptSession(unittest.TestCase):

    def test_excerpt_renders_user_and_assistant(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=[
                {"role": "user", "content": "what's the weather?", "timestamp": "T1"},
                {"role": "assistant", "content": "sunny and 70", "timestamp": "T2"},
            ])
            out = fs.excerpt_session(sp)
            self.assertIn("user: what's the weather?", out)
            self.assertIn("assistant: sunny and 70", out)

    def test_excerpt_anonymizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=[
                {"role": "user", "content": "email me at foo@bar.com",
                 "timestamp": "T1"},
            ])
            out = fs.excerpt_session(sp)
            self.assertNotIn("foo@bar.com", out)
            self.assertIn("<email>", out)

    def test_excerpt_uses_household_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=[
                {"role": "user", "content": "Mira will be late", "timestamp": "T1"},
            ])
            pat = fs.name_pattern_for(["Mira"])
            out = fs.excerpt_session(sp, name_pattern=pat)
            self.assertNotIn("Mira", out)
            self.assertIn("<name>", out)

    def test_turn_limit_respected(self):
        records = [{"role": "user", "content": f"msg {i}", "timestamp": f"T{i}"} for i in range(50)]
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=records)
            out = fs.excerpt_session(sp, turn_limit=5)
            self.assertIn("msg 49", out)
            self.assertNotIn("msg 0", out)
            self.assertNotIn("msg 30", out)

    def test_byte_limit_truncates_from_front(self):
        big = "x" * 5000
        records = [{"role": "user", "content": big, "timestamp": "T0"},
                   {"role": "user", "content": "TAIL_MARKER", "timestamp": "T1"}]
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=records)
            out = fs.excerpt_session(sp, byte_limit=200)
            self.assertIn("TAIL_MARKER", out)
            self.assertIn("(truncated)", out)
            self.assertLess(len(out.encode("utf-8")), 400)  # truncated + prefix

    def test_tool_records_omitted_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=[
                {"role": "tool", "name": "gmail_fetch",
                 "content": "SECRET_OAUTH_TOKEN_xyz", "timestamp": "T1"},
            ])
            out = fs.excerpt_session(sp)
            self.assertIn("gmail_fetch", out)
            self.assertNotIn("SECRET_OAUTH_TOKEN", out)

    def test_assistant_tool_calls_summarized(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=[
                {"role": "assistant", "content": "looking it up",
                 "tool_calls": [{"function": {"name": "calendar_fetch", "arguments": "{}"}}],
                 "timestamp": "T1"},
            ])
            out = fs.excerpt_session(sp)
            self.assertIn("[tools: calendar_fetch]", out)

    def test_missing_session_file_returns_placeholder(self):
        out = fs.excerpt_session(None)
        self.assertIn("no session file", out.lower())

    def test_invalid_jsonl_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "sessions").mkdir(parents=True)
            sp = ws / "sessions" / "s.jsonl"
            sp.write_text(
                "garbage line\n"
                + json.dumps({"role": "user", "content": "ok", "timestamp": "T1"}) + "\n",
                encoding="utf-8",
            )
            out = fs.excerpt_session(sp)
            self.assertIn("user: ok", out)


# ── Session resolution ──────────────────────────────────────────────────────

class TestResolveSessionFile(unittest.TestCase):

    def test_returns_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp, session_records=[{"role": "user", "content": "hi"}])
            self.assertEqual(fs.resolve_session_file(str(sp), ws), sp)

    def test_picks_most_recent_when_no_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "sessions").mkdir(parents=True)
            old = ws / "sessions" / "old.jsonl"
            new = ws / "sessions" / "new.jsonl"
            old.write_text("{}\n")
            new.write_text("{}\n")
            os.utime(old, (1000, 1000))
            os.utime(new, (2000, 2000))
            picked = fs.resolve_session_file(None, ws)
            self.assertEqual(picked, new)

    def test_returns_none_when_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "noexist"
            self.assertIsNone(fs.resolve_session_file(None, ws))


# ── Issue assembly ───────────────────────────────────────────────────────────

class TestAssembleIssue(unittest.TestCase):

    def test_includes_category_label(self):
        p = fs.assemble_issue("bug", "thing broke", "hh-123")
        self.assertIn("feedback:bug", p["labels"])
        self.assertIn("tenant:hh-123", p["labels"])
        self.assertTrue(p["title"].startswith("🐛 [bug]"))

    def test_skips_tenant_label_when_no_household(self):
        p = fs.assemble_issue("kudos", "thanks", "")
        self.assertEqual(p["labels"], ["feedback:kudos"])

    def test_includes_conversation_block_when_provided(self):
        p = fs.assemble_issue("bug", "x", "hh", conversation_block="[T1] user: hi")
        self.assertIn("Conversation excerpt", p["body"])
        self.assertIn("[T1] user: hi", p["body"])

    def test_omits_conversation_block_when_absent(self):
        p = fs.assemble_issue("bug", "x", "hh")
        self.assertNotIn("Conversation excerpt", p["body"])

    def test_title_truncated_to_80(self):
        long = "x" * 200
        p = fs.assemble_issue("feature", long, "hh")
        self.assertLessEqual(len(p["title"]), 100)


# ── Dry-run CLI ──────────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):

    def test_dry_run_emits_payload_no_network(self):
        rc, out = run_cli([
            "--dry-run", "--category", "feature", "--message", "add recurring chores",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertIn("feedback:feature", out["payload"]["labels"])

    def test_dry_run_with_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, sp = make_workspace(tmp,
                user_md_text="- Jamie (primary): notes\n",
                session_records=[
                    {"role": "user", "content": "Jamie is asking weather", "timestamp": "T1"},
                ])
            rc, out = run_cli([
                "--dry-run", "--category", "bug", "--message", "weather flow broken",
                "--include-conversation", "--session-file", str(sp),
                "--workspace", str(ws),
            ])
            self.assertEqual(rc, 0)
            body = out["payload"]["body"]
            self.assertIn("Conversation excerpt", body)
            self.assertNotIn("Jamie", body)
            self.assertIn("<name>", body)

    def test_empty_message_rejected(self):
        # `--message ""` would crash assemble_issue's title slicing.
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                fs.main(["--category", "bug", "--message", "   "])

    def test_http_4xx_does_not_retry_or_queue(self):
        # 422 (unprocessable, e.g. missing label), 401 (bad token), 403 (SSO
        # required) are deterministic — retrying just hides the error from the
        # operator. Should fail fast with rc=3 and no queue file.
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            calls = []

            def four_oh_one(*a, **kw):
                calls.append(1)
                raise urllib.error.HTTPError(
                    url="https://api.github.com/", code=401,
                    msg="Bad credentials", hdrs=None, fp=None,
                )

            with patch.object(fs, "post_issue", side_effect=four_oh_one), \
                 patch.object(fs.time, "sleep", return_value=None):
                rc, out = run_cli(
                    ["--category", "bug", "--message", "x"],
                    env={"HOMER_FEEDBACK_TOKEN": "fake",
                         "HOMER_FEEDBACK_REPO": "example/example",
                         "HOMER_HOUSEHOLD_ID": "hh",
                         "HOMER_WORKSPACE": str(ws)},
                )
            self.assertEqual(len(calls), 1, "must NOT retry on 4xx")
            self.assertEqual(rc, 3)
            self.assertFalse(out["ok"])
            self.assertIn("401", out["error"])
            self.assertNotIn("queued_path", out)
            self.assertFalse((ws / "feedback_queue").exists())

    def test_upload_failure_retries_once_then_queues(self):
        # Network errors should trigger one retry; if both attempts fail the
        # payload must land in the local queue instead of being dropped.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            calls = []

            def boom(*args, **kwargs):
                calls.append(1)
                raise TimeoutError("simulated network blip")

            with patch.object(fs, "post_issue", side_effect=boom), \
                 patch.object(fs.time, "sleep", return_value=None):
                rc, out = run_cli(
                    ["--category", "bug", "--message", "x"],
                    env={"HOMER_FEEDBACK_TOKEN": "fake",
                         "HOMER_FEEDBACK_REPO": "example/example",
                         "HOMER_HOUSEHOLD_ID": "hh",
                         "HOMER_WORKSPACE": str(ws)},
                )
            self.assertEqual(len(calls), 2, "expected 1 retry after first failure")
            self.assertEqual(rc, 2)
            self.assertFalse(out["ok"])
            self.assertIn("simulated network blip", out["error"])
            self.assertTrue(Path(out["queued_path"]).exists())

    def test_missing_repo_queues_locally(self):
        # HOMER_FEEDBACK_REPO is now mandatory (no default). When unset,
        # the upload should fail closed (queue locally) rather than try
        # to POST to an unknown repo.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            rc, out = run_cli(
                ["--category", "kudos", "--message", "thanks"],
                env={"HOMER_FEEDBACK_TOKEN": "fake",
                     # Deliberately unset HOMER_FEEDBACK_REPO.
                     "HOMER_HOUSEHOLD_ID": "hh-1",
                     "HOMER_WORKSPACE": str(ws)},
            )
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            self.assertIn("HOMER_FEEDBACK_REPO", out["error"])
            self.assertTrue(Path(out["queued_path"]).exists())

    def test_missing_token_queues_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            rc, out = run_cli(
                ["--category", "kudos", "--message", "thanks"],
                env={"HOMER_FEEDBACK_TOKEN": "",
                     "HOMER_FEEDBACK_REPO": "example/example",
                     "HOMER_HOUSEHOLD_ID": "hh-1",
                     "HOMER_WORKSPACE": str(ws)},
            )
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            queued = Path(out["queued_path"])
            self.assertTrue(queued.exists())
            record = json.loads(queued.read_text())
            self.assertIn("kudos", record["payload"]["title"].lower())


if __name__ == "__main__":
    unittest.main()

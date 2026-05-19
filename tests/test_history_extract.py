"""Tests for history_extract.py — Gemini extraction pipeline.

Validation logic now lives in tools/extract_core.py (shared with the portal).
The thin Gemini wrapper + persistence stays in tools/history_extract.py.
"""

import json
import uuid
import subprocess
from unittest.mock import patch

import pytest

import tools.extract_core as ec
import tools.history_extract as he


def _uuid() -> str:
    return str(uuid.uuid4())


# ── extract_core.validate_fragment ───────────────────────────────────────────


class TestValidateFragment:
    def test_valid_person_fragment(self):
        frag = {
            "kind": "person",
            "payload": {"name": "Helen"},
            "confidence": 0.9,
            "attribution": "verbatim",
        }
        assert ec.validate_fragment(frag)

    def test_invalid_kind(self):
        frag = {
            "kind": "unknown_kind",
            "payload": {},
            "confidence": 0.8,
            "attribution": "paraphrased",
        }
        assert not ec.validate_fragment(frag)

    def test_invalid_attribution(self):
        frag = {
            "kind": "place",
            "payload": {"name": "Lagos"},
            "confidence": 0.7,
            "attribution": "guessed",
        }
        assert not ec.validate_fragment(frag)

    def test_missing_payload(self):
        frag = {"kind": "date", "confidence": 0.5, "attribution": "paraphrased"}
        assert not ec.validate_fragment(frag)

    def test_confidence_not_numeric(self):
        frag = {
            "kind": "event",
            "payload": {"description": "wedding"},
            "confidence": "high",
            "attribution": "paraphrased",
        }
        assert not ec.validate_fragment(frag)

    def test_all_valid_kinds(self):
        for kind in ec.FRAGMENT_KINDS:
            frag = {
                "kind": kind,
                "payload": {"data": True},
                "confidence": 0.8,
                "attribution": "paraphrased",
            }
            assert ec.validate_fragment(frag), f"Expected {kind} to be valid"


# ── extract_core.extract_fragments (clamping, fence-stripping, validation) ───


class TestExtractFragmentsCore:
    @pytest.mark.asyncio
    async def test_strips_markdown_fences_and_clamps_confidence(self):
        async def fake_llm(system: str, user: str) -> str:
            return (
                "```json\n"
                '[{"kind": "event", "payload": {"description": "war"}, '
                '"confidence": 1.7, "attribution": "inferred"}]\n'
                "```"
            )

        out = await ec.extract_fragments(body="text", kind="text", llm_call=fake_llm)
        assert len(out) == 1
        assert out[0]["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_filters_invalid_fragments(self):
        async def fake_llm(system: str, user: str) -> str:
            return json.dumps([
                {"kind": "person", "payload": {"name": "Jake"}, "confidence": 0.7,
                 "attribution": "paraphrased"},
                {"kind": "INVALID", "payload": {}, "confidence": 0.5,
                 "attribution": "paraphrased"},
            ])

        out = await ec.extract_fragments(body="text", kind="text", llm_call=fake_llm)
        assert len(out) == 1
        assert out[0]["kind"] == "person"

    @pytest.mark.asyncio
    async def test_empty_body_returns_empty(self):
        async def fake_llm(system: str, user: str) -> str:
            raise AssertionError("llm should not be called for empty body")

        out = await ec.extract_fragments(body="", kind="text", llm_call=fake_llm)
        assert out == []


# ── do_extract ────────────────────────────────────────────────────────────────


class TestDoExtract:
    def test_no_body_returns_zero_fragments(self, monkeypatch, capsys):
        aid = _uuid()
        artifact = {"id": aid, "household_id": _uuid(), "kind": "text", "body": ""}
        monkeypatch.setattr(he.hs, "get_artifact", lambda artifact_id: artifact)

        he.do_extract(aid)
        out = json.loads(capsys.readouterr().out)
        assert out["fragments_written"] == 0
        assert out["artifact_id"] == aid

    def test_none_body_returns_zero_fragments(self, monkeypatch, capsys):
        aid = _uuid()
        artifact = {"id": aid, "household_id": _uuid(), "kind": "text", "body": None}
        monkeypatch.setattr(he.hs, "get_artifact", lambda artifact_id: artifact)

        he.do_extract(aid)
        out = json.loads(capsys.readouterr().out)
        assert out["fragments_written"] == 0

    def test_missing_artifact_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(he.hs, "get_artifact", lambda artifact_id: None)
        with pytest.raises(SystemExit) as exc:
            he.do_extract(_uuid())
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_gemini_error_exits_1(self, monkeypatch, capsys):
        aid = _uuid()
        artifact = {
            "id": aid, "household_id": _uuid(), "kind": "text",
            "body": "My grandmother was amazing.",
        }
        monkeypatch.setattr(he.hs, "get_artifact", lambda artifact_id: artifact)

        def boom():
            raise RuntimeError("Gemini unavailable")

        monkeypatch.setattr(he, "_build_llm_call", boom)

        with pytest.raises(SystemExit) as exc:
            he.do_extract(aid)
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_valid_fragments_written(self, monkeypatch, capsys):
        aid = _uuid()
        hid = _uuid()
        artifact = {
            "id": aid, "household_id": hid, "kind": "text",
            "body": "My grandmother Helen, born 1932 in Lagos, used to make jollof rice.",
        }
        monkeypatch.setattr(he.hs, "get_artifact", lambda artifact_id: artifact)

        # Stub extract_core.extract_fragments — we already test its internals
        # in TestExtractFragmentsCore. Here we exercise the persistence path.
        async def fake_extract(*, body, kind, llm_call):
            return [
                {"kind": "person",
                 "payload": {"name": "Helen", "birth_year": 1932},
                 "confidence": 0.9, "attribution": "paraphrased"},
                {"kind": "place",
                 "payload": {"name": "Lagos", "country": "Nigeria"},
                 "confidence": 0.85, "attribution": "paraphrased"},
            ]

        monkeypatch.setattr(he.extract_core, "extract_fragments", fake_extract)
        monkeypatch.setattr(he, "_build_llm_call", lambda: None)
        monkeypatch.setattr(he.hs, "insert_fragment",
                            lambda **kw: {"id": str(uuid.uuid4()), **kw})
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

        he.do_extract(aid, contributor_id=_uuid())
        out = json.loads(capsys.readouterr().out)
        assert out["fragments_written"] == 2
        assert len(out["fragment_ids"]) == 2


# ── _build_llm_call ───────────────────────────────────────────────────


class TestBuildLlmCall:
    def test_raises_without_any_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            he._build_llm_call()

    def test_prefers_openrouter_route(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setenv("GEMINI_API_KEY", "gm-test")  # both present
        assert he._pick_extraction_route() == ("google/gemini-3-flash-preview", "openrouter")

    def test_falls_back_to_gemini_when_openrouter_absent(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
        assert he._pick_extraction_route() == ("gemini-2.5-flash", "gemini")

    @pytest.mark.asyncio
    async def test_dispatches_with_max_tokens_none(self, monkeypatch):
        """Extraction must not pass a max_tokens cap to the dispatcher —
        the pre-litellm direct-Gemini call had no cap, and structured-JSON
        outputs can exceed any default. Regression guard for the fix to the
        bug introduced by the original litellm migration (PR #23).
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        captured: dict = {}

        def fake_complete(**kwargs):
            captured.update(kwargs)
            return "[]"

        monkeypatch.setattr("tools.llm.complete", fake_complete)

        call = he._build_llm_call()
        await call("sys", "user")
        assert captured["max_tokens"] is None
        assert captured["temperature"] == 0.1
        assert captured["extra"] == {"tool": "history_extract"}

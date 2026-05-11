#!/usr/bin/env python3
"""history_extract.py — Extract structured fragments from a raw artifact.

Given an artifact_id:
  1. Fetch the artifact from Supabase
  2. Run Gemini for structured fact extraction (via shared extract_core)
  3. Write fragments to Supabase
  4. Trigger era_coverage recompute (async — best effort)

Output: {"artifact_id": ..., "fragments_written": N, "fragment_ids": [...]}

Usage (via Homer exec tool):
    python tools/history_extract.py --artifact-id <uuid>
    python tools/history_extract.py --artifact-id <uuid> --contributor-id <uuid>
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
TOOLS_DIR = str(REPO_ROOT / "tools")
HOMER_VENV = str(REPO_ROOT / ".venv" / "bin" / "python")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import extract_core
import history_store as hs


def _build_gemini_llm_call():
    """Return an async llm_call(system, user) -> str backed by Gemini.

    Imports happen here so failures show up as a clear extraction error
    rather than a module-load crash for callers that never run extraction.
    """
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    sys.path.insert(0, str(REPO_ROOT))
    from tools.analytics.llm_call import llm_call as _llm_call_recorder

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=api_key)

    async def call(system: str, user: str) -> str:
        # google-genai ships a sync client; run it in a thread so the
        # extract_core async contract is honoured without blocking the loop.
        def _sync() -> str:
            with _llm_call_recorder(
                model="gemini/gemini-2.5-flash",
                provider="gemini",
                task_kind="tool_classifier",
                extra={"tool": "history_extract"},
            ) as rec:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=0.1,
                    ),
                )
                usage = getattr(response, "usage_metadata", None)
                rec.record(
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    cache_read_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
                )
            return response.text or ""

        return await asyncio.to_thread(_sync)

    return call


def do_extract(artifact_id: str, contributor_id: str | None = None) -> None:
    artifact = hs.get_artifact(artifact_id)
    if not artifact:
        print(json.dumps({"error": f"Artifact '{artifact_id}' not found"}))
        sys.exit(1)

    body = artifact.get("body")
    if not body or not body.strip():
        print(json.dumps({
            "artifact_id": artifact_id,
            "fragments_written": 0,
            "fragment_ids": [],
            "note": "Artifact has no body text to extract from",
        }))
        return

    kind = artifact.get("kind", "text")
    household_id = artifact["household_id"]
    artifact_contributor_id = artifact.get("contributor_id")

    try:
        llm_call = _build_gemini_llm_call()
        fragments = asyncio.run(
            extract_core.extract_fragments(body=body, kind=kind, llm_call=llm_call)
        )
    except Exception as exc:
        print(json.dumps({"error": f"Gemini extraction failed: {exc}"}))
        sys.exit(1)

    written_ids: list[str] = []
    for frag in fragments:
        row = hs.insert_fragment(
            household_id=household_id,
            artifact_id=artifact_id,
            kind=frag["kind"],
            payload=frag["payload"],
            confidence=frag["confidence"],
            attribution=frag["attribution"],
            contributor_id=artifact_contributor_id,
        )
        written_ids.append(row["id"])

    # Trigger era recompute best-effort (non-blocking)
    if contributor_id and written_ids:
        subprocess.Popen(
            [HOMER_VENV, f"{TOOLS_DIR}/history_era_recompute.py",
             "--contributor-id", contributor_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    print(json.dumps({
        "artifact_id": artifact_id,
        "fragments_written": len(written_ids),
        "fragment_ids": written_ids,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract structured fragments from an artifact.")
    parser.add_argument("--artifact-id", required=True, help="Artifact UUID to process")
    parser.add_argument("--contributor-id", help="Contributor UUID (used to trigger era recompute)")
    args = parser.parse_args()
    do_extract(args.artifact_id, args.contributor_id)


if __name__ == "__main__":
    main()

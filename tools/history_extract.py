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


def _pick_extraction_route() -> tuple[str, str]:
    """Choose (model, provider) for the extraction call.

    Extraction wants a flash-class model regardless of what the agent's
    chat default is. Routing preference: OpenRouter if the tenant has a
    sub-key (post-consolidation default), otherwise direct Gemini BYOK.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return "google/gemini-3-flash-preview", "openrouter"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini-2.5-flash", "gemini"
    raise RuntimeError(
        "No LLM credentials available: set OPENROUTER_API_KEY (preferred) or GEMINI_API_KEY"
    )


def _build_llm_call():
    """Return an async llm_call(system, user) -> str routed via litellm.

    Imports happen lazily so failures show up as a clear extraction error
    rather than a module-load crash for callers that never run extraction.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tools.llm import complete

    model, provider = _pick_extraction_route()

    async def call(system: str, user: str) -> str:
        def _sync() -> str:
            return complete(
                prompt=user,
                system=system,
                model=model,
                provider=provider,
                task_kind="tool_classifier",
                temperature=0.1,
                # No max_tokens cap: extraction emits structured JSON whose
                # length scales with the artifact. The previous direct-Gemini
                # call had no cap; the litellm migration accidentally added a
                # 2048-token default that truncated long extractions.
                max_tokens=None,
                extra={"tool": "history_extract"},
            )

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
        llm_call = _build_llm_call()
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

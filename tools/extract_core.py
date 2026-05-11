"""Provider-agnostic fragment extraction.

This module is the schema contract between the LLM and the `hist_fragments`
table: which fragment kinds exist, what each payload looks like, what counts
as a valid fragment. Both homer (WhatsApp / nanobot) and the portal historian
agent must agree on this — drift here means the curator UI breaks.

No IO, no env vars, no SDK imports. The caller injects an `llm_call`
coroutine, supplies the artifact body + kind, and gets back validated
fragment dicts ready to insert. Provider, key resolution, transport, and
storage all live in the caller.

Mirror of homer/tools/extract_core.py — sync changes by hand.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

FRAGMENT_KINDS: tuple[str, ...] = (
    "person",
    "place",
    "event",
    "relation",
    "quote",
    "date",
    "correction",
)

ATTRIBUTION_KINDS: tuple[str, ...] = ("verbatim", "paraphrased", "inferred")

EXTRACT_SYSTEM_PROMPT = """You are a careful oral-history archivist. Your job is to extract structured facts from a contributor's message. Return ONLY a JSON array of fragment objects. No preamble, no markdown fences.

Each fragment object has:
  kind        — one of: person, place, event, relation, quote, date, correction
  payload     — free-form object with the relevant data for that kind (see below)
  confidence  — float 0.0–1.0 (how certain you are this fact is accurate)
  attribution — one of: verbatim (contributor's exact words), paraphrased (your words, their meaning), inferred (reasonable inference from context)

Payload shape by kind:
  person      — {"name": str, "birth_year": int|null, "death_year": int|null, "notes": str}
  place       — {"name": str, "country": str|null, "state": str|null, "notes": str}
  event       — {"description": str, "year": int|null, "month": int|null, "era": str|null}
  relation    — {"person_a": str, "person_b": str, "relationship": str, "notes": str}
  quote       — {"speaker": str, "text": str, "context": str}
  date        — {"description": str, "year": int|null, "month": int|null, "uncertainty": str|null}
  correction  — {"corrects": str, "original_claim": str, "corrected_claim": str}

Rules:
- Extract ALL facts, no matter how minor.
- A single message can yield many fragments.
- Do NOT hallucinate. If you're unsure, lower confidence.
- For verbatim quotes, the payload.text must be the exact words from the message.
- If no facts are present, return [].
"""


LlmCall = Callable[[str, str], Awaitable[str]]


def validate_fragment(frag: Any) -> bool:
    return (
        isinstance(frag, dict)
        and frag.get("kind") in FRAGMENT_KINDS
        and isinstance(frag.get("payload"), dict)
        and isinstance(frag.get("confidence"), (int, float))
        and frag.get("attribution") in ATTRIBUTION_KINDS
    )


def _strip_fences(text: str) -> str:
    """Models occasionally add ```json fences despite instructions."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def extract_fragments(
    *, body: str, kind: str, llm_call: LlmCall
) -> list[dict[str, Any]]:
    """`llm_call(system, user) -> raw_text` is the only seam — caller picks the
    provider, model, and key. No retry; flakiness is the caller's problem.
    Malformed model output is treated as "no facts" rather than raised."""
    if not body or not body.strip():
        return []

    user_msg = f"Contributor message ({kind}):\n\n{body}"
    raw = await llm_call(EXTRACT_SYSTEM_PROMPT, user_msg)
    text = _strip_fences(raw or "")
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    out: list[dict[str, Any]] = []
    for frag in parsed:
        if not validate_fragment(frag):
            continue
        confidence = max(0.0, min(1.0, float(frag["confidence"])))
        out.append(
            {
                "kind": frag["kind"],
                "payload": frag["payload"],
                "confidence": confidence,
                "attribution": frag["attribution"],
            }
        )
    return out

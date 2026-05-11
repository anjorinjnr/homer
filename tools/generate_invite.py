#!/usr/bin/env python3
"""
generate_invite.py — Generate an event invitation image using Gemini's image generation.

Creates a polished invite image from event details. The image is saved to the
workspace files/ directory. Homer (the LLM) sends it to the owner for approval
and to guests via the nanobot message tool.

Usage (via Homer exec tool):
    python tools/generate_invite.py --event-id kemi_bday
    python tools/generate_invite.py --event-id kemi_bday --style "watercolor, festive"
    python tools/generate_invite.py --event-id kemi_bday --model gemini-3-pro-image-preview
    python tools/generate_invite.py --title "Alex's 5th Birthday" --date "July 12, 2026" \
        --time "2:00 PM" --location "123 Main St" --style "pool party, colorful"

Models: gemini-3.1-flash-image-preview (default, fast), gemini-3-pro-image-preview (higher quality)
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
EVENTS_DIR = Path(os.environ["HOMER_EVENTS_DIR"]) if os.environ.get("HOMER_EVENTS_DIR") else REPO_ROOT / "context" / "events"
WORKSPACE_DIR = Path(os.environ.get("HOMER_WORKSPACE",
                     str(REPO_ROOT / "context" / ".nanobot_workspace")))
FILES_DIR = WORKSPACE_DIR / "files"

DEFAULT_MODEL = "gemini-3.1-flash-image-preview"


def sanitize_event_id(event_id: str) -> str:
    """Sanitize event_id to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", os.path.basename(event_id))


def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print(json.dumps({"error": "GEMINI_API_KEY not set"}))
        sys.exit(1)
    return key


def read_event_details(event_id: str) -> dict:
    """Read event details from status.md for prompt construction."""
    status_path = EVENTS_DIR / sanitize_event_id(event_id) / "status.md"
    if not status_path.exists():
        return {}

    content = status_path.read_text(encoding="utf-8")
    details: dict = {}

    name_m = re.search(r"^# (.+)", content, re.MULTILINE)
    if name_m:
        details["title"] = name_m.group(1).strip()

    dates_m = re.search(r"^Dates:\s*(.+)", content, re.MULTILINE)
    if dates_m and dates_m.group(1).strip().lower() != "tbd":
        details["date"] = dates_m.group(1).strip()

    # Extract confirmed details (Location, Time, etc.)
    confirmed_m = re.search(r"## Confirmed Details\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if confirmed_m:
        for line in confirmed_m.group(1).strip().split("\n"):
            m = re.match(r"^- \*\*(.+?)\*\*:\s*(.+)", line.strip())
            if m:
                key = m.group(1).strip().lower().replace(" ", "_")
                val = m.group(2).strip()
                details[key] = val

    return details


def build_prompt(
    title: str,
    date: str | None = None,
    time: str | None = None,
    location: str | None = None,
    details: str | None = None,
    style: str | None = None,
    hosts: str | None = None,
    rsvp_by: str | None = None,
) -> str:
    """Construct the image generation prompt from event details."""
    event_lines = [f'- Title: "{title}"']
    if date:
        event_lines.append(f"- Date: {date}")
    if time:
        event_lines.append(f"- Time: {time}")
    if location:
        event_lines.append(f"- Location: {location}")
    if details:
        event_lines.append(f"- Details: {details}")
    if rsvp_by:
        event_lines.append(f"- RSVP by: {rsvp_by}")
    if hosts:
        event_lines.append(f"- Hosted by: {hosts}")

    event_block = "\n".join(event_lines)
    style_desc = style or "colorful, festive, clean readable text"

    return f"""Create a beautiful invitation image.

EVENT DETAILS (must appear legibly on the invite):
{event_block}

STYLE: {style_desc}
FORMAT: Square (1:1), high resolution, suitable for WhatsApp/SMS sharing.
IMPORTANT: All text must be sharp, correctly spelled, and fully legible."""


try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]


def generate_image(prompt: str, model: str, api_key: str) -> tuple[bytes, str]:
    """Call Gemini API to generate an invite image.

    Returns (image_bytes, mime_type).
    Raises RuntimeError on failure.
    """
    if genai is None:
        raise RuntimeError("google-genai package not installed")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )

    if not response.candidates:
        raise RuntimeError("No candidates returned from Gemini API")

    candidate = response.candidates[0]
    if candidate.content is None:
        finish = getattr(candidate, "finish_reason", "unknown")
        raise RuntimeError(f"Generation blocked (finish_reason={finish}) — try a different style or prompt")

    for part in candidate.content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            return part.inline_data.data, part.inline_data.mime_type

    raise RuntimeError("No image found in Gemini response — model may not support image generation")


def save_image(image_bytes: bytes, event_id: str, mime_type: str = "image/png") -> Path:
    """Save image to workspace files/ directory."""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    ext = "png" if "png" in mime_type else "jpeg" if "jpeg" in mime_type else "png"
    safe_id = sanitize_event_id(event_id)
    output_path = FILES_DIR / f"{safe_id}_invite.{ext}"
    output_path.write_bytes(image_bytes)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an event invitation image using Gemini.")
    parser.add_argument("--event-id", help="Event identifier (reads details from status.md)")
    parser.add_argument("--title", help="Event title (overrides status.md)")
    parser.add_argument("--date", help="Event date")
    parser.add_argument("--time", help="Event time")
    parser.add_argument("--location", help="Event location")
    parser.add_argument("--details", help="Additional details (e.g. 'Pool party — bring swimsuits')")
    parser.add_argument("--style", help="Visual style (e.g. 'watercolor, festive, colorful')")
    parser.add_argument("--hosts", help="Host names (e.g. 'Alex & Jordan')")
    parser.add_argument("--rsvp-by", help="RSVP deadline (e.g. 'July 5')")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Gemini image model (default: {DEFAULT_MODEL})")

    args = parser.parse_args()

    # Must have either --event-id or --title
    if not args.event_id and not args.title:
        print(json.dumps({"error": "--event-id or --title is required"}))
        sys.exit(1)

    # Read event details from status.md if event-id provided
    event_details = {}
    if args.event_id:
        event_details = read_event_details(args.event_id)

    # CLI args override event details
    title = args.title or event_details.get("title")
    if not title:
        print(json.dumps({"error": "No title found. Provide --title or ensure the event has a name."}))
        sys.exit(1)

    date = args.date or event_details.get("date")
    time_val = args.time or event_details.get("time")
    location = args.location or event_details.get("location")
    details = args.details or event_details.get("details")
    style = args.style
    hosts = args.hosts or event_details.get("hosts") or event_details.get("hosted_by")
    rsvp_by = args.rsvp_by or event_details.get("rsvp_by")

    api_key = get_api_key()

    prompt = build_prompt(
        title=title, date=date, time=time_val, location=location,
        details=details, style=style, hosts=hosts, rsvp_by=rsvp_by,
    )

    try:
        image_bytes, mime_type = generate_image(prompt, args.model, api_key)
    except Exception as e:
        print(json.dumps({"error": f"Image generation failed: {e}"}))
        sys.exit(1)

    event_id = args.event_id or re.sub(r"[^a-z0-9]", "_", title.lower())[:30]
    output_path = save_image(image_bytes, event_id, mime_type)

    print(json.dumps({
        "status": "ok",
        "event_id": args.event_id or "",
        "image_path": str(output_path),
        "model": args.model,
        "prompt_length": len(prompt),
    }, indent=2))


if __name__ == "__main__":
    main()

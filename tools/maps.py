#!/usr/bin/env python3
"""
maps.py — Google Maps Places search and Distance Matrix for Homer.

Usage:
    python tools/maps.py --mode places --query "Italian restaurants near home"
    python tools/maps.py --mode places --query "plumbers" --near "Anytown, ST"
    python tools/maps.py --mode details --place-id "ChIJxxxxxxxx"
    python tools/maps.py --mode distance --destination "Atlanta Airport"
    python tools/maps.py --mode distance --origin "123 Main St, Othertown ST" --destination "Atlanta Airport" --travel-mode transit

Default origin is the home address parsed from context/household.md
(the `- **Address**: …` line under `## Home`). If household.md doesn't
exist or has no Address line, `--origin` / `--near` is required.

Output (JSON):
    places:   { "results": [{ "name", "address", "rating", "open_now", "place_id", "types" }] }
    details:  { "name", "address", "phone", "website", "maps_url", "rating", "hours", "open_now" }
    distance: { "origin", "destination", "distance", "duration", "travel_mode" }
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

PLACES_SEARCH_URL  = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACE_DETAILS_URL  = "https://maps.googleapis.com/maps/api/place/details/json"
DISTANCE_URL       = "https://maps.googleapis.com/maps/api/distancematrix/json"

REPO_ROOT = Path(__file__).resolve().parent.parent
# household.md is the canonical source; user_context/ is where build_context.py
# expects the populated file, root context/ is the legacy fallback.
HOUSEHOLD_MD_CANDIDATES = [
    REPO_ROOT / "context" / "user_context" / "household.md",
    REPO_ROOT / "context" / "household.md",
]
_ADDRESS_RE = re.compile(r"^\s*-\s*\*\*Address\*\*:\s*(.+?)\s*$", re.IGNORECASE)


def get_default_origin() -> str:
    """Household home address parsed from context/household.md.

    Single source of truth — same file build_context.py treats as canonical
    for household identity. Tries user_context/household.md first, then
    falls back to root context/household.md (matches build_context's lookup
    order). Empty string means "no Address line found"; callers must pass
    --origin/--near.
    """
    for path in HOUSEHOLD_MD_CANDIDATES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            m = _ADDRESS_RE.match(line)
            if m:
                return m.group(1).strip()
    return ""


def get_api_key() -> str:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        print(json.dumps({"error": "GOOGLE_MAPS_API_KEY not set in environment."}))
        sys.exit(1)
    return key


def get(url: str, params: dict) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(full_url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        print(json.dumps({"error": f"HTTP {e.code}: {body}"}))
        sys.exit(1)
    except URLError as e:
        print(json.dumps({"error": f"Request failed: {e.reason}"}))
        sys.exit(1)

    status = data.get("status", "")
    if status not in ("OK", "ZERO_RESULTS"):
        print(json.dumps({"error": f"API error: {status} — {data.get('error_message', '')}"}))
        sys.exit(1)

    return data


def do_places(query: str, api_key: str, near: str = None, max_results: int = 5) -> None:
    full_query = f"{query} near {near}" if near else query
    data = get(PLACES_SEARCH_URL, {"query": full_query, "key": api_key})

    results = []
    for r in data.get("results", [])[:max_results]:
        oh = r.get("opening_hours", {})
        results.append({
            "name": r.get("name", ""),
            "address": r.get("formatted_address", ""),
            "rating": r.get("rating"),
            "user_ratings_total": r.get("user_ratings_total"),
            "open_now": oh.get("open_now"),
            "place_id": r.get("place_id", ""),
            "types": r.get("types", [])[:3],
        })

    print(json.dumps({"query": full_query, "results": results}, indent=2))


def do_details(place_id: str, api_key: str) -> None:
    fields = "name,formatted_address,formatted_phone_number,opening_hours,rating,user_ratings_total,website,url"
    data = get(PLACE_DETAILS_URL, {"place_id": place_id, "fields": fields, "key": api_key})

    r = data.get("result", {})
    oh = r.get("opening_hours", {})

    output = {
        "name": r.get("name", ""),
        "address": r.get("formatted_address", ""),
        "phone": r.get("formatted_phone_number"),
        "website": r.get("website"),
        "maps_url": r.get("url"),
        "rating": r.get("rating"),
        "user_ratings_total": r.get("user_ratings_total"),
        "open_now": oh.get("open_now"),
        "hours": oh.get("weekday_text", []),
    }

    print(json.dumps(output, indent=2))


def do_distance(destination: str, api_key: str, origin: str = None, travel_mode: str = "driving") -> None:
    orig = origin or get_default_origin()
    if not orig:
        print(json.dumps({
            "error": "No origin: pass --origin (or add `- **Address**: ...` under `## Home` in context/household.md).",
        }))
        sys.exit(1)
    data = get(DISTANCE_URL, {
        "origins": orig,
        "destinations": destination,
        "mode": travel_mode,
        "key": api_key,
    })

    rows = data.get("rows", [])
    if not rows or not rows[0].get("elements"):
        print(json.dumps({"error": "No route found."}))
        sys.exit(1)

    element = rows[0]["elements"][0]
    if element.get("status") != "OK":
        print(json.dumps({"error": f"Route error: {element.get('status')}"}))
        sys.exit(1)

    print(json.dumps({
        "origin": data["origin_addresses"][0],
        "destination": data["destination_addresses"][0],
        "distance": element["distance"]["text"],
        "duration": element["duration"]["text"],
        "travel_mode": travel_mode,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Maps Places and Distance for Homer.")
    parser.add_argument("--mode", required=True, choices=["places", "details", "distance"])

    # places
    parser.add_argument("--query", help="Place search query")
    parser.add_argument("--near", help="Location to search near (appended to query)")
    parser.add_argument("--max-results", type=int, default=5, help="Max results (default: 5)")

    # details
    parser.add_argument("--place-id", help="Google Place ID for detail lookup")

    # distance
    parser.add_argument("--origin", help="Origin address (default: Address from context/household.md)")
    parser.add_argument("--destination", help="Destination address")
    parser.add_argument("--travel-mode", default="driving",
                        choices=["driving", "walking", "bicycling", "transit"],
                        help="Travel mode (default: driving)")

    args = parser.parse_args()
    api_key = get_api_key()

    if args.mode == "places":
        if not args.query:
            print(json.dumps({"error": "--query is required for places mode"}))
            sys.exit(1)
        do_places(args.query, api_key, near=args.near, max_results=args.max_results)

    elif args.mode == "details":
        if not args.place_id:
            print(json.dumps({"error": "--place-id is required for details mode"}))
            sys.exit(1)
        do_details(args.place_id, api_key)

    elif args.mode == "distance":
        if not args.destination:
            print(json.dumps({"error": "--destination is required for distance mode"}))
            sys.exit(1)
        do_distance(args.destination, api_key, origin=args.origin, travel_mode=args.travel_mode)


if __name__ == "__main__":
    main()

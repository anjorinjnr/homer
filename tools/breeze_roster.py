#!/usr/bin/env python3
"""breeze_roster.py — BreezeRoster volunteer scheduling tool.

All output is JSON. Auth uses machine-to-machine client credentials
(BREEZE_CLIENT_ID + BREEZE_CLIENT_SECRET → short-lived bearer token, cached
55 minutes in /tmp/.breeze_token_<hash>.json).

Required env vars:
  BREEZE_BASE_URL        e.g. https://cherry.breezeroster.com
  BREEZE_CLIENT_ID
  BREEZE_CLIENT_SECRET
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BreezeClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("BREEZE_BASE_URL", "").rstrip("/")
        self.client_id = os.environ.get("BREEZE_CLIENT_ID", "")
        self.client_secret = os.environ.get("BREEZE_CLIENT_SECRET", "")
        if not (self.base_url and self.client_id and self.client_secret):
            _die("BREEZE_BASE_URL, BREEZE_CLIENT_ID, and BREEZE_CLIENT_SECRET must be set")

    # --- token management ---

    def _token_cache_path(self) -> Path:
        h = hashlib.sha256(self.client_id.encode()).hexdigest()[:12]
        return Path(f"/tmp/.breeze_token_{h}.json")

    def _load_cached_token(self) -> Optional[str]:
        p = self._token_cache_path()
        try:
            data = json.loads(p.read_text())
            if data.get("expires_at", 0) > time.time() + 60:
                return data["access_token"]
        except Exception:
            pass
        return None

    def _fetch_token(self) -> str:
        payload = json.dumps({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/auth/token",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            _die(f"Token exchange failed ({e.code}): {body}")
        token = data.get("access_token")
        if not token:
            _die(f"No access_token in response: {data}")
        # BreezeRoster tokens are short-lived (1 hour); cache for 55 min
        cache = {"access_token": token, "expires_at": time.time() + 3300}
        try:
            self._token_cache_path().write_text(json.dumps(cache))
        except OSError:
            pass
        return token

    def _token(self) -> str:
        return self._load_cached_token() or self._fetch_token()

    # --- HTTP helpers ---

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            _die(f"HTTP {e.code} {method.upper()} {path}: {body_text}")

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        return self._request("POST", path, body=body or {})

    def patch(self, path: str, body: dict) -> Any:
        return self._request("PATCH", path, body=body)

    # -----------------------------------------------------------------------
    # Teams
    # -----------------------------------------------------------------------

    def list_teams(self) -> Any:
        return self.get("/api/teams")

    def get_team(self, team_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}")

    def list_roster(self, team_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/roster")

    def list_members(self, team_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/members")

    # -----------------------------------------------------------------------
    # Volunteers
    # -----------------------------------------------------------------------

    def list_volunteers(self) -> Any:
        return self.get("/api/volunteers")

    def search_volunteers(self, query: str, team_id: Optional[str] = None) -> Any:
        params: dict = {"q": query}
        if team_id:
            params["teamId"] = team_id
        return self.get("/api/volunteers/search", params=params)

    def get_volunteer(self, volunteer_id: str) -> Any:
        return self.get(f"/api/volunteers/{volunteer_id}")

    # -----------------------------------------------------------------------
    # Schedules
    # -----------------------------------------------------------------------

    def list_schedules(self) -> Any:
        return self.get("/api/schedules")

    def get_schedule(self, schedule_id: str) -> Any:
        return self.get(f"/api/schedules/{schedule_id}")

    def generate_schedule(self, schedule_id: str) -> Any:
        return self.post(f"/api/schedules/{schedule_id}/generate")

    def publish_schedule(self, schedule_id: str) -> Any:
        return self.post(f"/api/schedules/{schedule_id}/publish")

    def unpublish_schedule(self, schedule_id: str) -> Any:
        return self.post(f"/api/schedules/{schedule_id}/unpublish")

    # -----------------------------------------------------------------------
    # Events / Instances
    # -----------------------------------------------------------------------

    def list_events(self, team_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/events")

    def get_event(self, team_id: str, event_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/events/{event_id}")

    def list_instances(self, team_id: str, event_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/events/{event_id}/instances")

    def get_instance(self, instance_id: str) -> Any:
        return self.get(f"/api/event-instances/{instance_id}")

    # -----------------------------------------------------------------------
    # Slots
    # -----------------------------------------------------------------------

    def assign_slot(self, slot_id: str, volunteer_id: Optional[str]) -> Any:
        body: dict = {"volunteerId": volunteer_id}
        return self.patch(f"/api/slots/{slot_id}", body)

    def bulk_assign(self, assignments: list) -> Any:
        return self.post("/api/slots/bulk", {"assignments": assignments})

    # -----------------------------------------------------------------------
    # Availability
    # -----------------------------------------------------------------------

    def get_availability(self, schedule_id: str) -> Any:
        return self.get("/api/availability", params={"scheduleId": schedule_id})

    def send_outreach(self, schedule_id: str) -> Any:
        return self.post("/api/availability/send-bulk", {"scheduleId": schedule_id})

    # -----------------------------------------------------------------------
    # Songs
    # -----------------------------------------------------------------------

    def list_songs(self, team_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/songs")

    def get_song(self, team_id: str, song_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/songs/{song_id}")

    def event_songs(self, team_id: str, instance_id: str) -> Any:
        return self.get(f"/api/teams/{team_id}/schedule/{instance_id}/songs")

    # -----------------------------------------------------------------------
    # Rules
    # -----------------------------------------------------------------------

    def list_rules(self) -> Any:
        return self.get("/api/rules")

    def parse_rule(self, text: str) -> Any:
        return self.post("/api/rules/parse", {"text": text})

    # -----------------------------------------------------------------------
    # Org
    # -----------------------------------------------------------------------

    def get_org(self) -> Any:
        return self.get("/api/org")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(json.dumps({"error": msg}))
    sys.exit(1)


def _out(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="breeze_roster.py",
        description="BreezeRoster volunteer scheduling",
    )

    # Teams
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list-teams", action="store_true", help="List all teams")
    g.add_argument("--get-team", metavar="TEAM_ID", help="Get one team")
    g.add_argument("--list-roster", action="store_true", help="List volunteers on a team (requires --team)")
    g.add_argument("--list-members", action="store_true", help="List admin/viewer members of a team (requires --team)")

    # Volunteers
    g.add_argument("--list-volunteers", action="store_true", help="List org volunteer pool")
    g.add_argument("--search-volunteers", metavar="QUERY", help="Search volunteers by name")
    g.add_argument("--get-volunteer", metavar="VOLUNTEER_ID", help="Get one volunteer")

    # Schedules
    g.add_argument("--list-schedules", action="store_true", help="List schedules")
    g.add_argument("--get-schedule", metavar="SCHEDULE_ID", help="Get schedule with instances + slots")
    g.add_argument("--generate-schedule", metavar="SCHEDULE_ID", help="AI-generate assignments")
    g.add_argument("--publish-schedule", metavar="SCHEDULE_ID", help="Publish a schedule")
    g.add_argument("--unpublish-schedule", metavar="SCHEDULE_ID", help="Revert schedule to draft")

    # Events / Instances
    g.add_argument("--list-events", action="store_true", help="List event templates (requires --team)")
    g.add_argument("--get-event", metavar="EVENT_ID", help="Get event template (requires --team)")
    g.add_argument("--list-instances", metavar="EVENT_ID", help="List instances of an event (requires --team)")
    g.add_argument("--get-instance", metavar="INSTANCE_ID", help="Get one event instance with slot grid")

    # Slots
    g.add_argument("--assign-slot", metavar="SLOT_ID", help="Assign a volunteer to a slot (requires --volunteer)")
    g.add_argument("--unassign-slot", metavar="SLOT_ID", help="Unassign a slot")

    # Availability
    g.add_argument("--get-availability", action="store_true", help="Get availability matrix (requires --schedule)")
    g.add_argument("--send-outreach", action="store_true", help="Send bulk availability outreach (requires --schedule)")

    # Songs
    g.add_argument("--list-songs", action="store_true", help="List team songs (requires --team)")
    g.add_argument("--event-songs", metavar="INSTANCE_ID", help="Songs assigned to an event instance (requires --team)")

    # Rules
    g.add_argument("--list-rules", action="store_true", help="List scheduling rules")
    g.add_argument("--parse-rule", metavar="TEXT", help="Parse natural-language rule into structured form")

    # Org
    g.add_argument("--get-org", action="store_true", help="Read org settings")

    # Shared modifiers (not in the mutex group)
    p.add_argument("--team", metavar="TEAM_ID", help="Team context for roster/events/songs")
    p.add_argument("--schedule", metavar="SCHEDULE_ID", help="Schedule context for availability")
    p.add_argument("--volunteer", metavar="VOLUNTEER_ID", help="Volunteer ID for slot assignment")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    client = BreezeClient()

    # --- Teams ---
    if args.list_teams:
        _out(client.list_teams())

    elif args.get_team:
        _out(client.get_team(args.get_team))

    elif args.list_roster:
        if not args.team:
            _die("--list-roster requires --team TEAM_ID")
        _out(client.list_roster(args.team))

    elif args.list_members:
        if not args.team:
            _die("--list-members requires --team TEAM_ID")
        _out(client.list_members(args.team))

    # --- Volunteers ---
    elif args.list_volunteers:
        _out(client.list_volunteers())

    elif args.search_volunteers:
        _out(client.search_volunteers(args.search_volunteers, team_id=args.team))

    elif args.get_volunteer:
        _out(client.get_volunteer(args.get_volunteer))

    # --- Schedules ---
    elif args.list_schedules:
        _out(client.list_schedules())

    elif args.get_schedule:
        _out(client.get_schedule(args.get_schedule))

    elif args.generate_schedule:
        _out(client.generate_schedule(args.generate_schedule))

    elif args.publish_schedule:
        _out(client.publish_schedule(args.publish_schedule))

    elif args.unpublish_schedule:
        _out(client.unpublish_schedule(args.unpublish_schedule))

    # --- Events / Instances ---
    elif args.list_events:
        if not args.team:
            _die("--list-events requires --team TEAM_ID")
        _out(client.list_events(args.team))

    elif args.get_event:
        if not args.team:
            _die("--get-event requires --team TEAM_ID")
        _out(client.get_event(args.team, args.get_event))

    elif args.list_instances:
        if not args.team:
            _die("--list-instances requires --team TEAM_ID")
        _out(client.list_instances(args.team, args.list_instances))

    elif args.get_instance:
        _out(client.get_instance(args.get_instance))

    # --- Slots ---
    elif args.assign_slot:
        if not args.volunteer:
            _die("--assign-slot requires --volunteer VOLUNTEER_ID")
        _out(client.assign_slot(args.assign_slot, args.volunteer))

    elif args.unassign_slot:
        _out(client.assign_slot(args.unassign_slot, None))

    # --- Availability ---
    elif args.get_availability:
        if not args.schedule:
            _die("--get-availability requires --schedule SCHEDULE_ID")
        _out(client.get_availability(args.schedule))

    elif args.send_outreach:
        if not args.schedule:
            _die("--send-outreach requires --schedule SCHEDULE_ID")
        _out(client.send_outreach(args.schedule))

    # --- Songs ---
    elif args.list_songs:
        if not args.team:
            _die("--list-songs requires --team TEAM_ID")
        _out(client.list_songs(args.team))

    elif args.event_songs:
        if not args.team:
            _die("--event-songs requires --team TEAM_ID")
        _out(client.event_songs(args.team, args.event_songs))

    # --- Rules ---
    elif args.list_rules:
        _out(client.list_rules())

    elif args.parse_rule:
        _out(client.parse_rule(args.parse_rule))

    # --- Org ---
    elif args.get_org:
        _out(client.get_org())

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

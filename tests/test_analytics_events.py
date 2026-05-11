"""Tests for the explicit member / guest lifecycle event helpers in
tools/analytics/events.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.analytics import events


@pytest.fixture(autouse=True)
def _household_env(monkeypatch):
    monkeypatch.setenv("HOMER_HOUSEHOLD_ID", "hh-test")


def _capture_args(mock: MagicMock, event_name: str) -> dict:
    for call in mock.capture.call_args_list:
        args = call.args
        if len(args) >= 2 and args[1] == event_name:
            return args[2] if len(args) >= 3 else call.kwargs.get("properties", {})
    raise AssertionError(f"event {event_name!r} never captured: {mock.capture.call_args_list}")


def test_household_member_added_shape():
    with patch("tools.analytics.events.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        events.track_household_member_added(
            "d-new", member_count_after=3, role="member",
        )
    props = _capture_args(client, "household_member_added")
    assert props == {
        "household_id": "hh-test",
        "member_count_after": 3,
        "role": "member",
    }


def test_household_member_removed_shape():
    with patch("tools.analytics.events.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        events.track_household_member_removed(
            "d-gone", member_count_after=2, role="member",
        )
    props = _capture_args(client, "household_member_removed")
    assert props["member_count_after"] == 2
    assert props["role"] == "member"
    assert props["household_id"] == "hh-test"


def test_guest_added_includes_scope_id_and_channel():
    with patch("tools.analytics.events.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        events.track_guest_added(
            "d-guest", scope_id="trip_paris_2026", channel="whatsapp",
        )
    props = _capture_args(client, "guest_added")
    assert props == {
        "household_id": "hh-test",
        "scope_id": "trip_paris_2026",
        "channel": "whatsapp",
        "scope_type": "event",
    }


def test_guest_removed_includes_scope_id_and_channel():
    with patch("tools.analytics.events.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        events.track_guest_removed(
            "d-guest", scope_id="trip_paris_2026", channel="telegram",
        )
    props = _capture_args(client, "guest_removed")
    assert props["scope_id"] == "trip_paris_2026"
    assert props["channel"] == "telegram"


def test_scope_type_defaults_to_event_but_overridable():
    with patch("tools.analytics.events.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        events.track_guest_added(
            "d", scope_id="brief_2026_q2", channel="email", scope_type="briefing",
        )
    props = _capture_args(client, "guest_added")
    assert props["scope_type"] == "briefing"


def test_household_id_missing_is_omitted(monkeypatch):
    """Without HOMER_HOUSEHOLD_ID the event still fires but omits the key —
    that way group-level retention queries don't get a bogus empty-string
    household bucket."""
    monkeypatch.delenv("HOMER_HOUSEHOLD_ID", raising=False)
    with patch("tools.analytics.events.get_client") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        events.track_household_member_added("d", member_count_after=1, role="admin")
    props = _capture_args(client, "household_member_added")
    assert "household_id" not in props

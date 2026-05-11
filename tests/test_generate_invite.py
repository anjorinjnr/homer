"""Tests for generate_invite.py — invite image generation tool."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.generate_invite as gi


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    monkeypatch.setattr(gi, "EVENTS_DIR", events_dir)
    monkeypatch.setattr(gi, "FILES_DIR", tmp_path / "files")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return tmp_path


@pytest.fixture()
def event(isolated_env):
    """Create a test event with details."""
    edir = isolated_env / "events" / "kemi_bday"
    edir.mkdir()
    (edir / "status.md").write_text("""\
# Kemi's 5th Birthday Party
Status: Coordinating
Dates: July 12, 2026
Created: 2026-03-31

## Guests (3)
3 pending

## Open Items

## Confirmed Details
- **Location**: 123 Pool Lane, Anytown ST
- **Time**: 2:00 – 5:00 PM

## Notes

## Budget

## Activity Log
| Date | What |
|------|------|
""")
    return "kemi_bday"


# ── read_event_details ───────────────────────────────────────────────────────

class TestReadEventDetails:
    def test_reads_title(self, event, isolated_env):
        details = gi.read_event_details("kemi_bday")
        assert details["title"] == "Kemi's 5th Birthday Party"

    def test_reads_dates(self, event):
        details = gi.read_event_details("kemi_bday")
        assert details["date"] == "July 12, 2026"

    def test_reads_confirmed_details(self, event):
        details = gi.read_event_details("kemi_bday")
        assert details["location"] == "123 Pool Lane, Anytown ST"
        assert details["time"] == "2:00 – 5:00 PM"

    def test_skips_tbd_dates(self, isolated_env):
        edir = isolated_env / "events" / "trip"
        edir.mkdir()
        (edir / "status.md").write_text("# Trip\nDates: TBD\n")
        details = gi.read_event_details("trip")
        assert "date" not in details

    def test_nonexistent_event_returns_empty(self):
        assert gi.read_event_details("nonexistent") == {}


# ── build_prompt ─────────────────────────────────────────────────────────────

class TestBuildPrompt:
    def test_includes_title(self):
        prompt = gi.build_prompt(title="Kemi's Birthday")
        assert "Kemi's Birthday" in prompt

    def test_includes_all_details(self):
        prompt = gi.build_prompt(
            title="Party", date="July 12", time="2 PM",
            location="123 Main St", details="Pool party",
            hosts="Alex & Sam", rsvp_by="July 5",
        )
        assert "July 12" in prompt
        assert "2 PM" in prompt
        assert "123 Main St" in prompt
        assert "Pool party" in prompt
        assert "Alex & Sam" in prompt
        assert "July 5" in prompt

    def test_custom_style(self):
        prompt = gi.build_prompt(title="Party", style="watercolor, purple theme")
        assert "watercolor, purple theme" in prompt

    def test_default_style(self):
        prompt = gi.build_prompt(title="Party")
        assert "colorful, festive" in prompt

    def test_omits_none_fields(self):
        prompt = gi.build_prompt(title="Party")
        assert "Location" not in prompt
        assert "Time" not in prompt


# ── generate_image ───────────────────────────────────────────────────────────

class TestGenerateImage:
    def _mock_response(self, image_data=b"fake_png", mime_type="image/png"):
        """Create a mock Gemini response with an image part."""
        part = MagicMock()
        part.inline_data = MagicMock()
        part.inline_data.mime_type = mime_type
        part.inline_data.data = image_data

        candidate = MagicMock()
        candidate.content.parts = [part]

        response = MagicMock()
        response.candidates = [candidate]
        return response

    @patch("tools.generate_invite.genai")
    def test_returns_image_bytes(self, mock_genai_module):
        mock_client = MagicMock()
        mock_genai_module.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = self._mock_response(b"png_data")

        data, mime = gi.generate_image("test prompt", "test-model", "test-key")
        assert data == b"png_data"
        assert mime == "image/png"

    @patch("tools.generate_invite.genai")
    def test_passes_model_and_prompt(self, mock_genai_module):
        mock_client = MagicMock()
        mock_genai_module.Client.return_value = mock_client
        mock_client.models.generate_content.return_value = self._mock_response()

        gi.generate_image("my prompt", "gemini-3.1-flash-image-preview", "key123")
        call_args = mock_client.models.generate_content.call_args
        assert call_args.kwargs["model"] == "gemini-3.1-flash-image-preview"
        assert call_args.kwargs["contents"] == "my prompt"

    @patch("tools.generate_invite.genai")
    def test_no_candidates_raises(self, mock_genai_module):
        mock_client = MagicMock()
        mock_genai_module.Client.return_value = mock_client
        response = MagicMock()
        response.candidates = []
        mock_client.models.generate_content.return_value = response

        with pytest.raises(RuntimeError, match="No candidates"):
            gi.generate_image("prompt", "model", "key")

    @patch("tools.generate_invite.genai")
    def test_no_image_in_response_raises(self, mock_genai_module):
        mock_client = MagicMock()
        mock_genai_module.Client.return_value = mock_client

        # Text-only response (no image)
        text_part = MagicMock()
        text_part.inline_data = None
        candidate = MagicMock()
        candidate.content.parts = [text_part]
        response = MagicMock()
        response.candidates = [candidate]
        mock_client.models.generate_content.return_value = response

        with pytest.raises(RuntimeError, match="No image found"):
            gi.generate_image("prompt", "model", "key")


# ── save_image ───────────────────────────────────────────────────────────────

class TestSaveImage:
    def test_saves_png(self, isolated_env):
        path = gi.save_image(b"fake_png", "kemi_bday", "image/png")
        assert path.exists()
        assert path.name == "kemi_bday_invite.png"
        assert path.read_bytes() == b"fake_png"

    def test_saves_jpeg(self, isolated_env):
        path = gi.save_image(b"fake_jpeg", "trip", "image/jpeg")
        assert path.name == "trip_invite.jpeg"

    def test_creates_files_dir(self, isolated_env):
        files_dir = isolated_env / "files"
        assert not files_dir.exists()
        gi.save_image(b"data", "test", "image/png")
        assert files_dir.exists()

    def test_overwrites_existing(self, isolated_env):
        gi.save_image(b"first", "test", "image/png")
        gi.save_image(b"second", "test", "image/png")
        path = isolated_env / "files" / "test_invite.png"
        assert path.read_bytes() == b"second"


# ── get_api_key ──────────────────────────────────────────────────────────────

class TestGetApiKey:
    def test_reads_from_env(self):
        assert gi.get_api_key() == "test-key"

    def test_missing_key_exits(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY")
        with pytest.raises(SystemExit):
            gi.get_api_key()


# ── main (end-to-end with mocked API) ───────────────────────────────────────

class TestMain:
    def _mock_generate(self, monkeypatch, image_data=b"test_image"):
        monkeypatch.setattr(gi, "generate_image",
                            lambda prompt, model, key: (image_data, "image/png"))

    def test_generate_from_event(self, event, monkeypatch, capsys, isolated_env):
        self._mock_generate(monkeypatch)
        monkeypatch.setattr("sys.argv", ["generate_invite.py", "--event-id", "kemi_bday"])
        gi.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert "kemi_bday_invite.png" in out["image_path"]
        assert Path(out["image_path"]).exists()

    def test_generate_from_args(self, monkeypatch, capsys, isolated_env):
        self._mock_generate(monkeypatch)
        monkeypatch.setattr("sys.argv", [
            "generate_invite.py", "--title", "Test Party",
            "--date", "July 4", "--style", "patriotic",
        ])
        gi.main()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"

    def test_no_title_fails(self, monkeypatch, capsys, isolated_env):
        monkeypatch.setattr("sys.argv", ["generate_invite.py", "--event-id", "nonexistent"])
        with pytest.raises(SystemExit):
            gi.main()

    def test_custom_model(self, event, monkeypatch, capsys, isolated_env):
        calls = []

        def mock_gen(prompt, model, key):
            calls.append(model)
            return (b"img", "image/png")

        monkeypatch.setattr(gi, "generate_image", mock_gen)
        monkeypatch.setattr("sys.argv", [
            "generate_invite.py", "--event-id", "kemi_bday",
            "--model", "gemini-3-pro-image-preview",
        ])
        gi.main()
        assert calls[0] == "gemini-3-pro-image-preview"

    def test_api_failure_returns_error(self, event, monkeypatch, capsys):
        monkeypatch.setattr(gi, "generate_image",
                            MagicMock(side_effect=RuntimeError("API down")))
        monkeypatch.setattr("sys.argv", ["generate_invite.py", "--event-id", "kemi_bday"])
        with pytest.raises(SystemExit):
            gi.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out
        assert "API down" in out["error"]

    def test_cli_args_override_event_details(self, event, monkeypatch, capsys, isolated_env):
        prompts = []

        def mock_gen(prompt, model, key):
            prompts.append(prompt)
            return (b"img", "image/png")

        monkeypatch.setattr(gi, "generate_image", mock_gen)
        monkeypatch.setattr("sys.argv", [
            "generate_invite.py", "--event-id", "kemi_bday",
            "--title", "Custom Title", "--location", "Custom Location",
        ])
        gi.main()
        assert "Custom Title" in prompts[0]
        assert "Custom Location" in prompts[0]

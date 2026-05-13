"""Tests for log_motivation.py — rolling-7 motivation log."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import log_motivation as lm


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    store = tmp_path / "recent_motivations.txt"
    monkeypatch.setattr(lm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(lm, "MOTIVATIONS_FILE", store)
    return store


def test_append_creates_file():
    lm.append("first line")
    assert lm._load() == ["first line"]


def test_append_trims_to_keep():
    for i in range(10):
        lm.append(f"line {i}")
    history = lm._load()
    assert len(history) == lm.KEEP
    assert history[0] == f"line {10 - lm.KEEP}"
    assert history[-1] == "line 9"


def test_append_strips_whitespace():
    lm.append("  padded line  ")
    assert lm._load() == ["padded line"]


def test_load_ignores_blank_rows(isolated_store):
    isolated_store.write_text("real line\n\n  \nanother\n", encoding="utf-8")
    assert lm._load() == ["real line", "another"]


class TestCLI:
    def test_logs_via_main(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["log_motivation.py", "--line", "Choose one good thing."])
        lm.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "logged"
        assert out["kept"] == 1

    def test_empty_line_errors(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["log_motivation.py", "--line", "   "])
        with pytest.raises(SystemExit):
            lm.main()
        import json
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

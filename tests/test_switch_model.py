"""Tests for switch_model.py — CURRENT_MODEL file and config update."""

import json
from pathlib import Path

import pytest

import tools.switch_model as sm


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    """Wire switch_model to use a tmp config and workspace.

    Provides every provider API key by default so existing tests don't trip
    the credential validation gate. Tests that exercise the gate explicitly
    delete the relevant env var via monkeypatch.
    """
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = {
        "agents": {
            "defaults": {
                "model": "gemini/gemini-2.5-pro",
                "provider": "gemini",
                "workspace": str(workspace),
            }
        }
    }
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(sm, "CONFIG_PATH", config_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    return config_path, workspace


def run_switch(model: str, fake_env, monkeypatch) -> None:
    config_path, _ = fake_env
    monkeypatch.setattr(sm.subprocess, "run",
                        lambda *a, **kw: type("R", (), {"returncode": 0})())
    monkeypatch.setattr("sys.argv", ["switch_model.py", "--model", model])
    sm.main()


def test_switch_updates_config(fake_env, monkeypatch):
    config_path, _ = fake_env
    run_switch("sonnet", fake_env, monkeypatch)
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "claude-sonnet-4-6"
    assert config["agents"]["defaults"]["provider"] == "anthropic"


def test_switch_writes_current_model_file(fake_env, monkeypatch):
    _, workspace = fake_env
    run_switch("sonnet", fake_env, monkeypatch)
    current = (workspace / "CURRENT_MODEL").read_text()
    assert current == "claude-sonnet-4-6"


def test_switch_updates_current_model_on_second_switch(fake_env, monkeypatch):
    _, workspace = fake_env
    run_switch("flash", fake_env, monkeypatch)
    run_switch("haiku", fake_env, monkeypatch)
    current = (workspace / "CURRENT_MODEL").read_text()
    assert current == "claude-haiku-4-5-20251001"


def test_claude_alias_resolves_to_sonnet(fake_env, monkeypatch):
    _, workspace = fake_env
    run_switch("claude", fake_env, monkeypatch)
    current = (workspace / "CURRENT_MODEL").read_text()
    assert current == "claude-sonnet-4-6"


def test_default_cheap_preset_has_valid_model_id():
    """Lock the regression from issue #250 — the deprecated
    `openrouter/deepseek/deepseek-chat-v3.2:free` id must not come back."""
    assert sm.MODELS["default-cheap"]["model"] == "deepseek/deepseek-v3.2"
    assert sm.MODELS["default-cheap"]["provider"] == "openrouter"


def test_switch_to_provider_without_key_exits_1(fake_env, monkeypatch, capsys):
    """Default-tier container (only OPENROUTER_API_KEY) cannot switch to a
    Claude preset — should exit 1 and name the missing env var."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # OPENROUTER_API_KEY stays set from the fixture.
    monkeypatch.setattr("sys.argv",
                        ["switch_model.py", "--model", "haiku", "--no-restart"])
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "default-tier" in err


def test_switch_to_provider_with_key_succeeds(fake_env, monkeypatch):
    """BYOK container (ANTHROPIC_API_KEY set) can switch to haiku."""
    config_path, workspace = fake_env
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv",
                        ["switch_model.py", "--model", "haiku", "--no-restart"])
    sm.main()
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "claude-haiku-4-5-20251001"
    assert (workspace / "CURRENT_MODEL").read_text() == "claude-haiku-4-5-20251001"


def test_switch_to_default_cheap_with_openrouter_succeeds(fake_env, monkeypatch):
    """default-tier container can switch to the deepseek preset."""
    config_path, workspace = fake_env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv",
                        ["switch_model.py", "--model", "default-cheap", "--no-restart"])
    sm.main()
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "deepseek/deepseek-v3.2"
    assert (workspace / "CURRENT_MODEL").read_text() == "deepseek/deepseek-v3.2"


def test_switch_to_default_cheap_without_openrouter_fails(fake_env, monkeypatch, capsys):
    """No OPENROUTER_API_KEY → cannot use default-cheap, even on a BYOK box."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv",
                        ["switch_model.py", "--model", "default-cheap", "--no-restart"])
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err


def test_validation_runs_before_config_write(fake_env, monkeypatch, capsys):
    """If validation fails, neither config.json nor CURRENT_MODEL is touched."""
    config_path, workspace = fake_env
    original_config = config_path.read_text()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv",
                        ["switch_model.py", "--model", "sonnet", "--no-restart"])
    with pytest.raises(SystemExit):
        sm.main()
    assert config_path.read_text() == original_config
    assert not (workspace / "CURRENT_MODEL").exists()


def test_container_mode_signals_pid_1_instead_of_systemctl(fake_env, monkeypatch, tmp_path):
    """In a container (/.dockerenv exists) the script must SIGTERM PID 1 so
    Docker restarts the container — calling systemctl would silently no-op."""
    sent = {}

    def fake_kill(pid, sig):
        sent["pid"] = pid
        sent["sig"] = sig

    fake_dockerenv = tmp_path / ".dockerenv"
    fake_dockerenv.write_text("")

    monkeypatch.setattr(sm, "DOCKERENV", fake_dockerenv)
    monkeypatch.setattr(sm.os, "kill", fake_kill)

    def boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called in container mode")

    monkeypatch.setattr(sm.subprocess, "run", boom)
    monkeypatch.setattr("sys.argv", ["switch_model.py", "--model", "sonnet"])
    sm.main()

    assert sent["pid"] == 1
    assert sent["sig"] == sm.signal.SIGTERM

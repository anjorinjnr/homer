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
    run_switch("claude-balanced", fake_env, monkeypatch)
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4.6"
    # Every preset now routes via OpenRouter — providers field reflects that
    # uniformly, regardless of which upstream model the preset names.
    assert config["agents"]["defaults"]["provider"] == "openrouter"


def test_switch_writes_current_model_file(fake_env, monkeypatch):
    _, workspace = fake_env
    run_switch("claude-balanced", fake_env, monkeypatch)
    current = (workspace / "CURRENT_MODEL").read_text()
    assert current == "anthropic/claude-sonnet-4.6"


def test_switch_updates_current_model_on_second_switch(fake_env, monkeypatch):
    _, workspace = fake_env
    run_switch("gemini-fast", fake_env, monkeypatch)
    run_switch("claude-fast", fake_env, monkeypatch)
    current = (workspace / "CURRENT_MODEL").read_text()
    assert current == "anthropic/claude-haiku-4.5"


def test_auto_preset_lets_openrouter_route(fake_env, monkeypatch):
    """`auto` is the recommended default for reminder tasks — it writes
    `openrouter/auto` so OpenRouter picks the cheapest viable model per
    request rather than pinning a specific SKU."""
    config_path, workspace = fake_env
    run_switch("auto", fake_env, monkeypatch)
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "openrouter/auto"
    assert config["agents"]["defaults"]["provider"] == "openrouter"
    assert (workspace / "CURRENT_MODEL").read_text() == "openrouter/auto"


def test_default_cheap_preset_has_valid_model_id():
    """Lock the regression from issue #250 — the deprecated
    `openrouter/deepseek/deepseek-chat-v3.2:free` id must not come back."""
    assert sm.MODELS["default-cheap"]["model"] == "deepseek/deepseek-v4-flash"
    assert sm.MODELS["default-cheap"]["provider"] == "openrouter"


def test_every_preset_routes_via_openrouter():
    """Invariant lock — post-consolidation, no direct-provider entries
    are allowed in MODELS. A future contributor adding e.g. a raw
    Anthropic preset would silently bypass per-tenant OR cost
    attribution; this test forces the consolidation discussion to
    happen at PR time."""
    for name, spec in sm.MODELS.items():
        assert spec["provider"] == "openrouter", (
            f"preset {name!r} must route via openrouter post-consolidation "
            f"(got provider={spec['provider']!r})"
        )


def test_switch_to_any_preset_without_openrouter_key_exits_1(
    fake_env, monkeypatch, capsys
):
    """Every preset routes via OpenRouter — without `OPENROUTER_API_KEY`
    in the container env there's nothing to authenticate the call with,
    so the script must refuse and name the missing variable."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["switch_model.py", "--model", "claude-fast", "--no-restart"]
    )
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err


def test_switch_to_openrouter_preset_with_key_succeeds(fake_env, monkeypatch):
    """With `OPENROUTER_API_KEY` set (the only key any preset needs now),
    every preset switch lands and writes the configured model id.

    ANTHROPIC_API_KEY / GEMINI_API_KEY are deliberately left untouched —
    no preset reads those env vars after consolidation, so deleting them
    here would just be noise. (Earlier versions of this test deleted them
    because some legacy presets required specific direct-provider keys;
    that requirement is gone.)
    """
    config_path, workspace = fake_env
    monkeypatch.setattr(
        "sys.argv", ["switch_model.py", "--model", "claude-fast", "--no-restart"]
    )
    sm.main()
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "anthropic/claude-haiku-4.5"
    assert (workspace / "CURRENT_MODEL").read_text() == "anthropic/claude-haiku-4.5"


def test_switch_to_default_cheap_with_openrouter_succeeds(fake_env, monkeypatch):
    """default-tier container can switch to the deepseek preset."""
    config_path, workspace = fake_env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv",
                        ["switch_model.py", "--model", "default-cheap", "--no-restart"])
    sm.main()
    config = json.loads(config_path.read_text())
    assert config["agents"]["defaults"]["model"] == "deepseek/deepseek-v4-flash"
    assert (workspace / "CURRENT_MODEL").read_text() == "deepseek/deepseek-v4-flash"


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
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["switch_model.py", "--model", "claude-balanced", "--no-restart"],
    )
    with pytest.raises(SystemExit):
        sm.main()
    assert config_path.read_text() == original_config
    assert not (workspace / "CURRENT_MODEL").exists()


def test_container_mode_defers_restart_via_background_kill(fake_env, monkeypatch, tmp_path):
    """In a container (/.dockerenv exists) the script must NOT call
    `os.kill(1, SIGTERM)` synchronously — that tears the agent process
    down before the current turn's reply gets flushed to the user
    (observed in prod on 2026-05-14). Instead, spawn a detached
    background process that sleeps a few seconds and signals PID 1
    afterward, so the agent loop can finish the turn first."""
    fake_dockerenv = tmp_path / ".dockerenv"
    fake_dockerenv.write_text("")
    monkeypatch.setattr(sm, "DOCKERENV", fake_dockerenv)

    # Refuse to call `os.kill` directly — the bug we're fixing.
    def boom_kill(pid, sig):
        raise AssertionError(
            f"os.kill({pid}, {sig}) called synchronously — must defer via "
            "background subprocess instead"
        )

    monkeypatch.setattr(sm.os, "kill", boom_kill)

    # `subprocess.run` is the systemd path; container mode must not hit it.
    def boom_run(*a, **kw):
        raise AssertionError("subprocess.run must not be called in container mode")

    monkeypatch.setattr(sm.subprocess, "run", boom_run)

    # Capture the deferred-kill subprocess.Popen call.
    popen_calls: list[dict] = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            popen_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)

    monkeypatch.setattr("sys.argv", ["switch_model.py", "--model", "claude-balanced"])
    sm.main()

    assert len(popen_calls) == 1, "expected exactly one deferred-kill subprocess"
    call = popen_calls[0]
    # Detached so this script's exit doesn't take the background kill with it.
    assert call["kwargs"].get("start_new_session") is True
    # Sleeps before killing — exact duration is implementation detail but
    # the command must combine a wait with a TERM to PID 1.
    cmd_str = " ".join(call["args"]) if isinstance(call["args"], list) else call["args"]
    assert "sleep" in cmd_str
    assert "kill" in cmd_str
    assert " 1" in cmd_str  # target PID 1

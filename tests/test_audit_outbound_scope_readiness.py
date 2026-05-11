"""Tests for tools/audit_outbound_scope_readiness.py."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import tools.scope_store as ss


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated configs + scope DB + users.yaml. Reload the audit module so
    its outbound_scope_lookup import sees the test fixture paths."""
    nanobot_dir = tmp_path / ".nanobot"
    nanobot_dir.mkdir()
    main_cfg = nanobot_dir / "config.json"
    guest_cfg = nanobot_dir / "guest_config.json"
    main_cfg.write_text(json.dumps({
        "channels": {
            "telegram": {"allowFrom": ["5550000001"]},  # household member
        }
    }))
    guest_cfg.write_text(json.dumps({
        "channels": {
            "whatsapp": {
                "allow_from": [
                    "14125550001@s.whatsapp.net",   # household
                    "15551239999@s.whatsapp.net",   # ok-with-scope
                    "15551240000@s.whatsapp.net",   # acl-only, stranded
                    "*",                             # ignored
                ]
            },
            "telegram": {"allowFrom": ["123456789"]},  # stranded
        }
    }))

    # users.yaml with one household member per channel.
    users_yaml = tmp_path / "users.yaml"
    users_yaml.write_text(yaml.safe_dump({
        "users": [
            {"name": "Alex", "channels": {
                "telegram": "5550000001",
                "whatsapp": "14125550001",
                "email": "alex@example.com",
            }}
        ]
    }))

    db_path = tmp_path / "scopes.db"
    monkeypatch.setenv("HOMER_SCOPE_DB", str(db_path))
    monkeypatch.setenv("HOMER_USERS_YAML", str(users_yaml))
    ss._SCHEMA_INITIALISED.clear()

    # Force fresh import of audit + lookup so they pick up the env vars.
    # audit.py adds tools/ to sys.path at import time, so this is also when
    # the bare-name `manage_guest` module first becomes importable.
    for mod_name in (
        "tools.audit_outbound_scope_readiness",
        "tools.outbound_scope_lookup",
        "outbound_scope_lookup",
        "audit_outbound_scope_readiness",
        "manage_guest",
    ):
        importlib.sys.modules.pop(mod_name, None)
    audit_mod = importlib.import_module("tools.audit_outbound_scope_readiness")

    # tools/ is importable both as `tools.manage_guest` and bare `manage_guest`
    # (the latter only after audit_mod's import-time sys.path tweak). The audit
    # script uses the bare form at runtime, so patch both module objects.
    import tools.manage_guest as _pkg_mg
    _bare_mg = importlib.import_module("manage_guest")
    for mod in (_pkg_mg, _bare_mg):
        monkeypatch.setattr(mod, "NANOBOT_CONFIG_PATH", main_cfg)
        monkeypatch.setattr(mod, "GUEST_NANOBOT_CONFIG_PATH", guest_cfg)
        monkeypatch.setattr(mod, "ACL_FILE", tmp_path / "acl.json")

    # Seed an active scope-with-context for one stranded WA recipient.
    env_doc = ss.make_interaction_envelope(
        scope_id="int_bob", name="Bob",
        participant_id="15551239999@s.whatsapp.net",
        purpose="Quote for exterior painting",
    )
    ss.create_scope(env_doc)
    return tmp_path, audit_mod


def test_clean_setup_only_household(env, monkeypatch):
    tmp_path, audit_mod = env
    # Reset configs to only-household entries.
    guest_cfg = tmp_path / ".nanobot" / "guest_config.json"
    guest_cfg.write_text(json.dumps({
        "channels": {
            "whatsapp": {"allow_from": ["14125550001@s.whatsapp.net"]},
        }
    }))
    grouped = audit_mod.audit()
    assert grouped.get("no_scope", []) == []
    assert grouped.get("scope_no_context", []) == []
    # Household member shows up as ok.
    assert any(
        r["recipient"] == "14125550001@s.whatsapp.net"
        for r in grouped.get("ok", [])
    )


def test_classifies_stranded_and_ok(env):
    _, audit_mod = env
    grouped = audit_mod.audit()

    # Bob — has a scope with purpose → ok.
    ok_recipients = {(r["channel"], r["recipient"]) for r in grouped.get("ok", [])}
    assert ("whatsapp", "15551239999@s.whatsapp.net") in ok_recipients
    assert ("whatsapp", "14125550001@s.whatsapp.net") in ok_recipients
    assert ("telegram", "5550000001") in ok_recipients

    # 15551240000 + telegram 123456789 — no scope, stranded.
    stranded = {(r["channel"], r["recipient"]) for r in grouped.get("no_scope", [])}
    assert ("whatsapp", "15551240000@s.whatsapp.net") in stranded
    assert ("telegram", "123456789") in stranded


def test_scope_no_context_branch(env):
    _, audit_mod = env
    # Add a scope-without-purpose for a new recipient — should land in
    # the "scope_no_context" bucket, not "no_scope".
    ss.create_scope(ss.make_interaction_envelope(
        scope_id="int_silent",
        name="Silent",
        participant_id="15559999999@s.whatsapp.net",
        purpose="",
    ))
    # Inject the recipient into the guest WA allow_from so the audit picks it up.
    guest_cfg_path = list((Path("/") / "tmp").glob("**/.nanobot/guest_config.json"))
    # Simpler: re-derive via the same monkeypatched path.
    import tools.manage_guest as mg
    cfg = json.loads(mg.GUEST_NANOBOT_CONFIG_PATH.read_text())
    cfg["channels"]["whatsapp"]["allow_from"].append("15559999999@s.whatsapp.net")
    mg.GUEST_NANOBOT_CONFIG_PATH.write_text(json.dumps(cfg))

    grouped = audit_mod.audit()
    silent = {(r["channel"], r["recipient"]) for r in grouped.get("scope_no_context", [])}
    assert ("whatsapp", "15559999999@s.whatsapp.net") in silent


def test_main_exit_code_clean_run(env, capsys, monkeypatch):
    tmp_path, audit_mod = env
    # Strip configs to only-household.
    (tmp_path / ".nanobot" / "guest_config.json").write_text(json.dumps({
        "channels": {
            "whatsapp": {"allow_from": ["14125550001@s.whatsapp.net"]},
        }
    }))
    (tmp_path / ".nanobot" / "config.json").write_text(json.dumps({
        "channels": {"telegram": {"allowFrom": ["5550000001"]}},
    }))
    monkeypatch.setattr("sys.argv", ["audit_outbound_scope_readiness.py"])
    rc = audit_mod.main()
    assert rc == 0


def test_main_exit_code_stranded(env, capsys, monkeypatch):
    _, audit_mod = env
    monkeypatch.setattr("sys.argv", ["audit_outbound_scope_readiness.py"])
    rc = audit_mod.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "stranded" in out
    assert "no_scope" in out


def test_json_output(env, capsys, monkeypatch):
    _, audit_mod = env
    monkeypatch.setattr("sys.argv", ["audit_outbound_scope_readiness.py", "--json"])
    audit_mod.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "no_scope" in parsed
    assert any(r["recipient"] == "15551240000@s.whatsapp.net"
               for r in parsed.get("no_scope", []))

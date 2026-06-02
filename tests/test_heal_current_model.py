"""Tests for heal_current_model.py — boot-time stale CURRENT_MODEL pin removal."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import heal_current_model as h  # noqa: E402
from presets import PRESETS  # noqa: E402


def _a_valid_id() -> str:
    return next(iter(PRESETS.values()))["model"]


def test_keeps_valid_pin(tmp_path):
    f = tmp_path / "CURRENT_MODEL"
    f.write_text(_a_valid_id())
    assert h.heal(f) is None
    assert f.is_file() and f.read_text() == _a_valid_id()


def test_clears_stale_pin(tmp_path):
    f = tmp_path / "CURRENT_MODEL"
    f.write_text("deepseek/deepseek-v0-retired")
    reason = h.heal(f)
    assert reason is not None
    assert "no longer in the preset slate" in reason
    assert not f.exists()


def test_clears_the_real_v3_2_to_v4_regression(tmp_path):
    """The exact case that bit us: a pin on the now-removed v3.2 id."""
    f = tmp_path / "CURRENT_MODEL"
    f.write_text("deepseek/deepseek-v3.2")
    # v3.2 was dropped from the slate when v4-flash became the default tier.
    assert "deepseek/deepseek-v3.2" not in h.valid_model_ids()
    assert h.heal(f) is not None
    assert not f.exists()


def test_absent_file_is_noop(tmp_path):
    f = tmp_path / "CURRENT_MODEL"
    assert h.heal(f) is None


def test_empty_file_is_noop_and_untouched(tmp_path):
    f = tmp_path / "CURRENT_MODEL"
    f.write_text("   \n")
    assert h.heal(f) is None
    assert f.is_file()  # left as-is; entrypoint treats empty as "no pin"


def test_whitespace_around_valid_id_is_kept(tmp_path):
    f = tmp_path / "CURRENT_MODEL"
    f.write_text("  " + _a_valid_id() + "\n")
    assert h.heal(f) is None
    assert f.is_file()


def test_default_tier_id_is_always_valid():
    # The default-tier model must never be considered stale, or the heal
    # would clear a freshly-correct pin and thrash on every boot.
    assert PRESETS["default-cheap"]["model"] in h.valid_model_ids()


def test_auto_route_is_valid():
    assert PRESETS["auto"]["model"] in h.valid_model_ids()


def test_main_prints_reason_for_stale(tmp_path, capsys):
    f = tmp_path / "CURRENT_MODEL"
    f.write_text("retired/model-x")
    rc = h.main(["heal_current_model.py", str(f)])
    assert rc == 0
    assert "no longer in the preset slate" in capsys.readouterr().out
    assert not f.exists()


def test_main_no_args_is_noop():
    assert h.main(["heal_current_model.py"]) == 0

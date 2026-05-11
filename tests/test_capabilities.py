"""Tests for the capability gating pass in build_context.py."""

import textwrap

import pytest

import tools.build_context as bc


# ── apply_capability_markers ──────────────────────────────────────────────────

def test_enabled_capability_keeps_content_and_strips_markers():
    text = textwrap.dedent(
        """\
        preamble
        <!-- CAPABILITY: finance_plaid -->
        - Balance check details here.
        <!-- /CAPABILITY -->
        postamble
        """
    )
    result = bc.apply_capability_markers(text, {"finance_plaid"})
    assert "Balance check details here." in result
    assert "CAPABILITY" not in result
    assert "preamble" in result and "postamble" in result


def test_disabled_capability_removes_block_entirely():
    text = textwrap.dedent(
        """\
        preamble
        <!-- CAPABILITY: finance_plaid -->
        - Balance check details here.
        <!-- /CAPABILITY -->
        postamble
        """
    )
    result = bc.apply_capability_markers(text, set())
    assert "Balance check" not in result
    assert "CAPABILITY" not in result
    assert "preamble" in result and "postamble" in result


def test_unknown_capability_is_treated_as_disabled_fail_closed():
    text = "before\n<!-- CAPABILITY: typo_name -->\ninside\n<!-- /CAPABILITY -->\nafter\n"
    result = bc.apply_capability_markers(text, {"finance_plaid"})
    assert "inside" not in result
    assert "before" in result and "after" in result


def test_multiple_blocks_are_handled_independently():
    text = textwrap.dedent(
        """\
        <!-- CAPABILITY: cap_a -->
        a-content
        <!-- /CAPABILITY -->
        <!-- CAPABILITY: cap_b -->
        b-content
        <!-- /CAPABILITY -->
        <!-- CAPABILITY: cap_c -->
        c-content
        <!-- /CAPABILITY -->
        """
    )
    result = bc.apply_capability_markers(text, {"cap_a", "cap_c"})
    assert "a-content" in result
    assert "b-content" not in result
    assert "c-content" in result


def test_no_markers_means_text_unchanged():
    text = "# Plain doc\n\nno markers here\n"
    assert bc.apply_capability_markers(text, {"finance_plaid"}) == text


def test_disabled_block_does_not_leave_orphan_blank_lines():
    # Key readability property: stripping a block should not leave a triple
    # blank line where surrounding paragraphs now collide.
    text = "alpha\n<!-- CAPABILITY: off -->\nbody\n<!-- /CAPABILITY -->\nomega\n"
    result = bc.apply_capability_markers(text, set())
    assert result == "alpha\nomega\n"


def test_enabled_block_preserves_surrounding_newlines():
    # Lock current regex behavior: when a block is enabled, the inner content
    # keeps its trailing newline and the block's own trailing newline is
    # consumed — so a following header stays flush without an extra blank line.
    text = "intro\n<!-- CAPABILITY: on -->\n- bullet\n<!-- /CAPABILITY -->\n## Next\n"
    result = bc.apply_capability_markers(text, {"on"})
    assert result == "intro\n- bullet\n## Next\n"


def test_all_capabilities_enabled_sentinel_keeps_every_block():
    # When the manifest file is missing entirely we hand back a sentinel that
    # apply_capability_markers treats as "every block enabled" — this is the
    # bare-VPS parity path.
    text = textwrap.dedent(
        """\
        <!-- CAPABILITY: anything -->
        kept
        <!-- /CAPABILITY -->
        <!-- CAPABILITY: also_anything -->
        also kept
        <!-- /CAPABILITY -->
        """
    )
    result = bc.apply_capability_markers(text, bc.ALL_CAPABILITIES_ENABLED)
    assert "kept" in result and "also kept" in result
    assert "CAPABILITY" not in result


# ── load_enabled_capabilities ─────────────────────────────────────────────────

MANIFEST_YAML = textwrap.dedent(
    """\
    capabilities:
      finance_plaid:
        skills: [finance]
        requires_env: [PLAID_CLIENT_ID]
      skyvern:
        skills: [skyvern]
      health:
        skills: [health]
    """
)


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "capabilities.yaml"
    path.write_text(MANIFEST_YAML)
    monkeypatch.setattr(bc, "CAPABILITIES_MANIFEST_PATH", path)
    return path


def test_missing_features_yaml_enables_everything(manifest, tmp_path, monkeypatch):
    """Bare-VPS parity: no features.yaml ⇒ every manifest capability is on."""
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "nonexistent.yaml")
    monkeypatch.setenv("PLAID_CLIENT_ID", "x")  # silence the env-var warning
    enabled = bc.load_enabled_capabilities()
    assert enabled == {"finance_plaid", "skyvern", "health"}


def test_features_yaml_can_disable_a_capability(manifest, tmp_path, monkeypatch):
    features = tmp_path / "features.yaml"
    features.write_text("skyvern: false\nhealth: false\n")
    monkeypatch.setattr(bc, "FEATURES_PATH", features)
    monkeypatch.setenv("PLAID_CLIENT_ID", "x")
    enabled = bc.load_enabled_capabilities()
    assert enabled == {"finance_plaid"}


def test_features_yaml_unknown_key_is_ignored_with_warning(
    manifest, tmp_path, monkeypatch, capsys
):
    features = tmp_path / "features.yaml"
    features.write_text("not_a_real_capability: true\n")
    monkeypatch.setattr(bc, "FEATURES_PATH", features)
    monkeypatch.setenv("PLAID_CLIENT_ID", "x")
    enabled = bc.load_enabled_capabilities()
    assert "not_a_real_capability" not in enabled
    assert enabled == {"finance_plaid", "skyvern", "health"}
    captured = capsys.readouterr()
    assert "not_a_real_capability" in captured.err
    assert "unknown capability" in captured.err.lower()


def test_missing_required_env_warns_only_when_verify_flag_set(
    manifest, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "nonexistent.yaml")
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.setenv("HOMER_VERIFY_CAPABILITIES", "1")
    enabled = bc.load_enabled_capabilities()
    assert "finance_plaid" in enabled
    captured = capsys.readouterr()
    assert "PLAID_CLIENT_ID" in captured.err
    assert "finance_plaid" in captured.err


def test_env_var_warnings_suppressed_by_default(
    manifest, tmp_path, monkeypatch, capsys
):
    """Env-var preflight is opt-in. context_updater.py rebuilds the workspace
    on every approved write; warning every run would be spammy noise."""
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "nonexistent.yaml")
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("HOMER_VERIFY_CAPABILITIES", raising=False)
    bc.load_enabled_capabilities()
    captured = capsys.readouterr()
    assert "PLAID_CLIENT_ID" not in captured.err


def test_missing_manifest_returns_sentinel_for_bare_vps_parity(tmp_path, monkeypatch):
    """No manifest ⇒ every CAPABILITY block in the agent templates stays in.
    This is the bare-VPS path — deployments without a manifest must not have
    their content silently stripped."""
    monkeypatch.setattr(bc, "CAPABILITIES_MANIFEST_PATH", tmp_path / "none.yaml")
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "none.yaml")
    enabled = bc.load_enabled_capabilities()
    assert enabled is bc.ALL_CAPABILITIES_ENABLED
    # And it behaves as "all enabled" when passed to apply_capability_markers
    text = "before\n<!-- CAPABILITY: anything -->\ninside\n<!-- /CAPABILITY -->\nafter\n"
    assert "inside" in bc.apply_capability_markers(text, enabled)


# ── default_enabled ──────────────────────────────────────────────────────────

DEFAULTS_MANIFEST_YAML = textwrap.dedent(
    """\
    capabilities:
      finance_plaid:
        default_enabled: false
        skills: [finance]
      skyvern:
        default_enabled: false
        skills: [skyvern]
      health:
        skills: [health]
    """
)


@pytest.fixture()
def defaults_manifest(tmp_path, monkeypatch):
    path = tmp_path / "capabilities.yaml"
    path.write_text(DEFAULTS_MANIFEST_YAML)
    monkeypatch.setattr(bc, "CAPABILITIES_MANIFEST_PATH", path)
    return path


def test_default_enabled_false_keeps_capability_off_without_features_yaml(
    defaults_manifest, tmp_path, monkeypatch
):
    """A new household (no features.yaml) gets only the caps whose manifest
    entry does not set default_enabled: false."""
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "nonexistent.yaml")
    enabled = bc.load_enabled_capabilities()
    assert enabled == {"health"}


def test_features_yaml_overrides_default_enabled_false(
    defaults_manifest, tmp_path, monkeypatch
):
    """A household can opt in to a default-off capability via features.yaml."""
    features = tmp_path / "features.yaml"
    features.write_text("finance_plaid: true\n")
    monkeypatch.setattr(bc, "FEATURES_PATH", features)
    enabled = bc.load_enabled_capabilities()
    assert "finance_plaid" in enabled
    assert "skyvern" not in enabled  # still default-off
    assert "health" in enabled       # still default-on


def test_features_yaml_can_force_disable_default_on_capability(
    defaults_manifest, tmp_path, monkeypatch
):
    """Symmetric: a household can also opt out of a default-on capability."""
    features = tmp_path / "features.yaml"
    features.write_text("health: false\n")
    monkeypatch.setattr(bc, "FEATURES_PATH", features)
    enabled = bc.load_enabled_capabilities()
    assert enabled == set()


def test_explicit_default_enabled_true_behaves_same_as_absent(tmp_path, monkeypatch):
    """Locks the contract: `default_enabled: true` is equivalent to omitting
    the field. Protects against a future refactor that flips the absent-field
    default from True to False without updating call sites."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(textwrap.dedent("""\
        capabilities:
          explicit_on:
            default_enabled: true
          implicit_on:
            skills: [foo]
    """))
    monkeypatch.setattr(bc, "CAPABILITIES_MANIFEST_PATH", manifest)
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "nonexistent.yaml")
    enabled = bc.load_enabled_capabilities()
    assert enabled == {"explicit_on", "implicit_on"}


def test_non_bool_default_enabled_is_ignored(tmp_path, monkeypatch):
    """A quoted string like "false" or a number shouldn't silently enable a
    capability. Non-bool values fall back to the True default, same as the
    features.yaml parser's isinstance(v, bool) guard."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(textwrap.dedent("""\
        capabilities:
          quoted_false:
            default_enabled: "false"
          numeric_zero:
            default_enabled: 0
          actual_false:
            default_enabled: false
    """))
    monkeypatch.setattr(bc, "CAPABILITIES_MANIFEST_PATH", manifest)
    monkeypatch.setattr(bc, "FEATURES_PATH", tmp_path / "nonexistent.yaml")
    enabled = bc.load_enabled_capabilities()
    # Non-bool values (string "false", int 0) fall through to the True default
    assert "quoted_false" in enabled
    assert "numeric_zero" in enabled
    # Only an actual bool false disables the capability
    assert "actual_false" not in enabled


# ── _skills_gated_by_capabilities ─────────────────────────────────────────────

def test_skills_gated_map_excludes_core_skills(manifest):
    gated = bc._skills_gated_by_capabilities()
    assert gated == {"finance": "finance_plaid", "skyvern": "skyvern", "health": "health"}
    # Core skills (gmail, calendar, drive) are not in the manifest and must
    # not appear in the gated map — they should always load.
    assert "gmail" not in gated
    assert "calendar" not in gated

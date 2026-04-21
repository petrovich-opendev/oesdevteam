"""Tests for the Step 1 deliverable: explicit Opus 4.7 model routing.

These tests answer four yes/no questions:
  1. Does every AgentRole resolve to a model without hitting a global CLI default?
  2. Are heavy roles pinned to Opus 4.7 as specified?
  3. Do environment variable overrides win over the YAML default?
  4. Does the assembled ``claude -p`` argv contain ``--model <name>``
     and ``--max-budget-usd <amount>``?

If all four are green, Step 1 has achieved its objective: the pipeline no
longer depends on the user's global Claude CLI config for model selection.

The ``_isolate_oesdevteam_env`` fixture in ``conftest.py`` autouse-resets
``OESDEVTEAM_*`` env vars and the YAML cache between tests.
"""

from __future__ import annotations

import pytest

from src.claude_bridge import (
    ClaudeCliCommand,
    build_claude_cli_command,
)
from src.config import (
    ModelSpec,
    get_model_for_role,
    get_model_spec_for_profile,
    get_model_spec_for_role,
)
from src.models import AgentRole

# -----------------------------------------------------------------------------
# Config loader — every role maps, and Opus 4.7 is the default for heavies
# -----------------------------------------------------------------------------


class TestEveryRoleResolves:
    """Every AgentRole must resolve without falling back to a global default."""

    def test_every_role_has_a_model(self):
        """Missing a role here means the pipeline could not launch it."""
        for role in AgentRole:
            spec = get_model_spec_for_role(role)
            assert isinstance(spec, ModelSpec)
            assert spec.model, f"Role {role.value!r} resolved to an empty model"
            assert spec.max_cost_usd > 0

    def test_heavy_roles_pinned_to_opus_4_7(self):
        """Roles that perform reasoning must use Opus 4.7 by default."""
        heavy = [
            AgentRole.DEVELOPER,
            AgentRole.ARCHITECT,
            AgentRole.SENIOR_BACKEND,
            AgentRole.SENIOR_FRONTEND,
            AgentRole.SENIOR_DATA,
            AgentRole.SENIOR_PERFORMANCE,
            AgentRole.BUSINESS_EXPERT,
            AgentRole.DEVOPS,
            AgentRole.APPSEC,
        ]
        for role in heavy:
            assert get_model_for_role(role) == "claude-opus-4-7", (
                f"Expected Opus 4.7 for {role.value!r}, got {get_model_for_role(role)!r}"
            )

    def test_lightweight_roles_use_sonnet(self):
        """QA, UX reviewer, Support — fast enough on Sonnet."""
        light = [AgentRole.QA, AgentRole.UX_REVIEWER, AgentRole.SUPPORT]
        for role in light:
            model = get_model_for_role(role)
            assert model.startswith("claude-sonnet"), (
                f"Expected Sonnet for {role.value!r}, got {model!r}"
            )

    def test_profiles_present(self):
        """Named profiles for utility calls (drift, verifier, reflection) exist."""
        drift = get_model_spec_for_profile("drift_classifier")
        assert "haiku" in drift.model

        verifier = get_model_spec_for_profile("verifier")
        assert "sonnet" in verifier.model

        reflection = get_model_spec_for_profile("reflection")
        assert reflection.max_cost_usd > 0

    def test_unknown_role_raises(self):
        """A typo in a NATS message must fail fast, not silently route to a default."""
        with pytest.raises(KeyError):
            # "architect_of_doom" is intentionally not a member of AgentRole;
            # we exercise the string-path here because NATS subjects arrive
            # as strings before being mapped to the enum.
            get_model_spec_for_role("architect_of_doom")

    def test_unknown_profile_raises(self):
        with pytest.raises(KeyError):
            get_model_spec_for_profile("nonexistent_profile")


# -----------------------------------------------------------------------------
# Env overrides
# -----------------------------------------------------------------------------


class TestEnvOverrides:
    """Env vars must beat YAML — enables cheap A/B testing without edits.

    Env vars are read fresh on every ``get_model_spec_*`` call, so no
    ``reload_config()`` is needed for env-only changes. If a test seems to
    require ``reload_config()`` in the env path, that's a bug — the loader
    should be reading env anew, not caching it.
    """

    def test_role_override_via_env(self, monkeypatch):
        monkeypatch.setenv("OESDEVTEAM_MODEL_DEVELOPER", "claude-sonnet-4-6")
        assert get_model_for_role(AgentRole.DEVELOPER) == "claude-sonnet-4-6"

    def test_empty_env_var_falls_back_to_yaml(self, monkeypatch):
        monkeypatch.setenv("OESDEVTEAM_MODEL_DEVELOPER", "   ")
        # Whitespace-only value must be treated as "not set" — otherwise a
        # mis-edited shell profile could push garbage into the CLI call.
        assert get_model_for_role(AgentRole.DEVELOPER) == "claude-opus-4-7"

    def test_profile_override_via_env(self, monkeypatch):
        monkeypatch.setenv("OESDEVTEAM_PROFILE_DRIFT_CLASSIFIER", "claude-haiku-test")
        spec = get_model_spec_for_profile("drift_classifier")
        assert spec.model == "claude-haiku-test"


# -----------------------------------------------------------------------------
# CLI command builder
# -----------------------------------------------------------------------------


def _dummy_spec(model: str = "claude-opus-4-7", cost: float = 1.0) -> ModelSpec:
    """Build a ModelSpec for unit tests without touching the YAML loader."""
    return ModelSpec(
        role_or_profile="developer",
        model=model,
        max_cost_usd=cost,
        rationale="test",
    )


class TestBuildClaudeCliCommand:
    """The assembled argv must carry --model (the whole point of Step 1)."""

    def test_argv_includes_explicit_model(self):
        cmd = build_claude_cli_command(
            role=AgentRole.DEVELOPER,
            task="Write a hello-world endpoint.",
            system_prompt="You are a Developer.",
            model_spec=_dummy_spec(),
            claude_bin="/usr/bin/claude",
        )

        assert isinstance(cmd, ClaudeCliCommand)
        assert "--model" in cmd.argv
        model_index = cmd.argv.index("--model")
        assert cmd.argv[model_index + 1] == "claude-opus-4-7"

    def test_argv_includes_max_budget_usd(self):
        """Cost ceiling must land in argv — otherwise it's decorative."""
        cmd = build_claude_cli_command(
            role=AgentRole.DEVELOPER,
            task="ship",
            system_prompt="dev",
            model_spec=_dummy_spec(cost=2.0),
            claude_bin="/usr/bin/claude",
        )
        assert "--max-budget-usd" in cmd.argv
        idx = cmd.argv.index("--max-budget-usd")
        assert cmd.argv[idx + 1] == "2.0000"

    def test_argv_uses_kebab_case_flags(self):
        """We standardise on kebab-case for long flags across the codebase."""
        cmd = build_claude_cli_command(
            role=AgentRole.QA,
            task="Run the verify suite.",
            system_prompt="You are QA.",
            allowed_tools=("Bash", "Read"),
            model_spec=_dummy_spec(model="claude-sonnet-4-6"),
            claude_bin="/usr/bin/claude",
        )

        assert "-p" in cmd.argv
        assert "Run the verify suite." in cmd.argv
        assert "--system-prompt" in cmd.argv
        assert "--allowed-tools" in cmd.argv
        # Make sure the deprecated camelCase form is NOT used
        assert "--allowedTools" not in cmd.argv
        tools_idx = cmd.argv.index("--allowed-tools")
        assert cmd.argv[tools_idx + 1] == "Bash,Read"

    def test_settings_path_optional(self):
        cmd = build_claude_cli_command(
            role=AgentRole.DEVELOPER,
            task="Ship it.",
            system_prompt="You are Developer.",
            model_spec=_dummy_spec(),
            claude_bin="/usr/bin/claude",
            settings_path="/tmp/settings.json",
        )
        assert "--settings" in cmd.argv
        idx = cmd.argv.index("--settings")
        assert cmd.argv[idx + 1] == "/tmp/settings.json"

    def test_trace_omits_every_sensitive_positional(self):
        """Trace output must NOT include task, system prompt, or tools list."""
        sensitive_task = "SECRET-BUSINESS-PLAN-XYZ"
        sensitive_prompt = "CONFIDENTIAL-INSTRUCTIONS-123"
        cmd = build_claude_cli_command(
            role=AgentRole.DEVELOPER,
            task=sensitive_task,
            system_prompt=sensitive_prompt,
            model_spec=_dummy_spec(),
            claude_bin="/usr/bin/claude",
        )
        trace = cmd.trace()
        serialised = repr(trace)
        assert sensitive_task not in serialised, "Task prompt leaked into trace"
        assert sensitive_prompt not in serialised, "System prompt leaked into trace"
        assert trace["role"] == "developer"
        assert trace["model"] == "claude-opus-4-7"
        assert "--model" in trace["flags_used"]
        assert "--max-budget-usd" in trace["flags_used"]

    def test_default_allowed_tools_are_restrictive(self):
        """Default toolset must NOT grant network or remote access."""
        from src.claude_bridge import DEFAULT_ALLOWED_TOOLS

        assert "WebSearch" not in DEFAULT_ALLOWED_TOOLS
        assert "WebFetch" not in DEFAULT_ALLOWED_TOOLS
        # Basic local tools should be present — the controller relies on them.
        for required in ("Read", "Write", "Edit", "Bash"):
            assert required in DEFAULT_ALLOWED_TOOLS


# -----------------------------------------------------------------------------
# Integration — pipeline goal alignment
# -----------------------------------------------------------------------------


class TestBusinessGoalAlignment:
    """Step 1 contributes to the business goal of reproducible codegen.

    A reproducible pipeline is one where the same spec on two machines
    produces the same agent behaviour — which requires explicit model choice.
    """

    def test_no_role_relies_on_cli_default(self):
        """Every role must resolve to a non-empty model, independent of env."""
        for role in AgentRole:
            assert get_model_for_role(role), (
                f"Role {role.value!r} resolved to empty — means the CLI "
                "would fall back to its global default. That is exactly "
                "what Step 1 eliminates."
            )

    def test_every_built_command_names_its_model_and_budget(self):
        """Every call must be auditable post-hoc by model name AND dollar cap."""
        for role in AgentRole:
            cmd = build_claude_cli_command(
                role=role,
                task="probe",
                system_prompt="probe",
                claude_bin="/usr/bin/claude",
            )
            assert cmd.model, f"{role.value} has empty model in command"
            assert "--model" in cmd.argv
            assert "--max-budget-usd" in cmd.argv
            assert cmd.max_cost_usd > 0

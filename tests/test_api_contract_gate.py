"""Tests for the API Contract Gate (Step 4).

These tests answer:
  1. Does the gate PASS when schema, OpenAPI, and TS types all move together?
  2. Does it BLOCK when a backend schema change arrives without OpenAPI?
  3. Does it BLOCK when OpenAPI changes without regenerated TS types?
  4. Does it correctly short-circuit to PASS on docs-only PRs (not applicable)?
  5. Does the YAML loader reject malformed config?
  6. Does the Markdown renderer produce grep-friendly, emoji-free output?
"""

from __future__ import annotations

import textwrap

import pytest

from src.gates import ApiContractConfig, ApiContractGate, GateInput, run_api_contract_gate
from src.gates.api_contract_gate import render_api_contract_report
from src.models import QualityGateType

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def config() -> ApiContractConfig:
    """Narrow patterns tailored to the tests — independent of repo YAML."""
    return ApiContractConfig(
        backend_schema_patterns=("**/schemas.py", "**/routes/*.py"),
        openapi_patterns=("openapi.json",),
        types_patterns=("frontend/api-types.ts",),
    )


def _mk_input(files: list[str], *, feature_id: str = "C-001") -> GateInput:
    """Build a GateInput with only files_changed filled — enough for this gate."""
    return GateInput(
        feature_id=feature_id,
        feature_goal="exercise the API contract gate",
        files_changed=files,
    )


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------


class TestGatePasses:
    async def test_not_applicable_for_docs_only_change(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["README.md", "docs/ARCHITECTURE.md"]))

        assert result.passed is True
        assert result.gate_type == QualityGateType.API_CONTRACT
        assert "not applicable" in result.reason.lower()
        assert result.allow_retry is False

    async def test_all_three_synced_passes(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(
            _mk_input(
                [
                    "backend/schemas.py",
                    "openapi.json",
                    "frontend/api-types.ts",
                ]
            )
        )
        assert result.passed is True
        assert "synchronised" in result.reason

    async def test_openapi_and_types_without_schema_passes(self, config):
        """A hand-edit of openapi.json with regenerated types is fine."""
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["openapi.json", "frontend/api-types.ts"]))
        assert result.passed is True


# -----------------------------------------------------------------------------
# Blocking path
# -----------------------------------------------------------------------------


class TestGateBlocks:
    async def test_schema_changed_without_openapi_blocks(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["backend/schemas.py"]))

        assert result.passed is False
        assert "OpenAPI" in result.reason
        assert result.allow_retry is True
        violations = result.details["violations"]
        assert len(violations) == 1
        assert "openapi" in violations[0].lower()

    async def test_openapi_changed_without_types_blocks(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["openapi.json"]))

        assert result.passed is False
        assert "TypeScript" in result.reason
        assert result.details["types_changed"] == []

    async def test_both_violations_reported(self, config):
        """Schema only — both invariants broken in a realistic "schema only" PR."""
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["backend/routes/users.py"]))
        assert result.passed is False
        # The reason quotes the first violation; details carries both.
        assert "Backend schema" in result.reason

    async def test_routes_subdir_matches_schema_pattern(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["backend/routes/auth.py"]))
        assert result.passed is False
        assert "backend/routes/auth.py" in result.details["schema_changed"]


# -----------------------------------------------------------------------------
# Config loading
# -----------------------------------------------------------------------------


class TestConfigLoad:
    def test_default_config_loads(self):
        """The repo-root YAML must parse — we ship it, it must work out of the box."""
        cfg = ApiContractConfig.load()
        assert cfg.backend_schema_patterns
        assert cfg.openapi_patterns
        assert cfg.types_patterns

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ApiContractConfig.load(tmp_path / "does-not-exist.yaml")

    def test_malformed_config_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            textwrap.dedent(
                """
                version: 1
                # deliberately missing backend_schema
                openapi_artefact:
                  patterns: [openapi.json]
                frontend_types:
                  patterns: [api-types.ts]
                """
            )
        )
        with pytest.raises(ValueError, match="Malformed"):
            ApiContractConfig.load(bad)


# -----------------------------------------------------------------------------
# Functional wrapper equivalence
# -----------------------------------------------------------------------------


class TestFunctionAndClassAgree:
    async def test_function_form_matches_class_form(self, config):
        gate = ApiContractGate(config=config)
        inp = _mk_input(["backend/schemas.py"])

        via_class = await gate.check(inp)
        via_func = await run_api_contract_gate(inp, config=config)

        assert via_class.passed == via_func.passed
        assert via_class.reason == via_func.reason


# -----------------------------------------------------------------------------
# Markdown report
# -----------------------------------------------------------------------------


class TestRenderReport:
    async def test_block_report_lists_violations_and_changed_files(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["backend/schemas.py"]))
        md = render_api_contract_report(result)

        assert "[BLOCK]" in md
        assert "## Violations" in md
        assert "Backend schema files" in md
        assert "backend/schemas.py" in md

    async def test_pass_report_is_concise(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(
            _mk_input(["backend/schemas.py", "openapi.json", "frontend/api-types.ts"])
        )
        md = render_api_contract_report(result)

        assert "[PASS]" in md
        assert "Violations" not in md

    async def test_renderer_is_emoji_free(self, config):
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["backend/schemas.py"]))
        md = render_api_contract_report(result)
        for forbidden in ("✅", "❌", "🚧", "⚠"):
            assert forbidden not in md

    async def test_renderer_rejects_wrong_gate_type(self):
        from src.gates.base import GateResult

        foreign = GateResult(
            gate_type=QualityGateType.SENIOR_REVIEW,
            passed=True,
            reason="approved",
        )
        with pytest.raises(ValueError):
            render_api_contract_report(foreign)


# -----------------------------------------------------------------------------
# Business-goal alignment
# -----------------------------------------------------------------------------


class TestBusinessGoalAlignment:
    """Closes the classic "username vs chat_id" bug class.

    The feedback loop from v1 named this contract-drift as one of the
    most expensive repeated failure modes. The gate enforces a
    deterministic invariant that refuses to merge unless all three sides
    move together.
    """

    async def test_partial_sync_is_not_enough(self, config):
        """Must have ALL of schema+openapi+types, not just two of three."""
        gate = ApiContractGate(config=config)
        result = await gate.check(_mk_input(["backend/schemas.py", "openapi.json"]))
        assert result.passed is False
        assert "TypeScript" in result.reason

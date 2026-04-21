"""Tests for the SRE Review Gate (Step 5).

Answers:
  1. Does the gate PASS when the change does not touch deploy surface?
  2. Does the gate PASS when SRE reviewer approves with no findings?
  3. Does the gate BLOCK on a blocker or a major finding?
  4. Does it handle reviewer infrastructure faults without silent-approve?
  5. Does the Markdown renderer stay grep-friendly and emoji-free?
"""

from __future__ import annotations

import json
import textwrap

import pytest

from src.gates import GateInput, SreReviewConfig, SreReviewGate, run_sre_review_gate
from src.gates.sre_review_gate import render_sre_review_report
from src.models import AgentRole, QualityGateType
from src.reviewers import MockReviewerRunner

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _approve() -> str:
    return json.dumps(
        {
            "reviewer": "senior_sre",
            "verdict": "approve",
            "findings": [],
            "positive_notes": ["Health check hits a real query."],
        }
    )


def _blocker(summary: str) -> str:
    return json.dumps(
        {
            "reviewer": "senior_sre",
            "verdict": "needs_rework",
            "findings": [
                {
                    "severity": "blocker",
                    "file": "deploy/k8s/prod.yaml",
                    "line": 77,
                    "category": "rollback",
                    "summary": summary,
                    "why": "A 3 AM rollback would require manual data surgery.",
                    "fix": "Add reverse migration script and document rollback path.",
                }
            ],
        }
    )


def _major(summary: str) -> str:
    return json.dumps(
        {
            "reviewer": "senior_sre",
            "verdict": "needs_rework",
            "findings": [
                {
                    "severity": "major",
                    "file": "Dockerfile",
                    "line": 12,
                    "category": "resources",
                    "summary": summary,
                    "why": "Missing memory limit invites noisy-neighbor incidents.",
                    "fix": "Declare memory request + limit on the container spec.",
                }
            ],
        }
    )


@pytest.fixture
def config() -> SreReviewConfig:
    """Narrow deploy-surface patterns — independent of repo YAML."""
    return SreReviewConfig(
        deploy_surface_patterns=(
            "Dockerfile",
            "docker-compose.yml",
            "**/k8s/**/*.yaml",
            "**/migrations/**",
        )
    )


def _mk_input(files: list[str], *, feature_id: str = "SRE-001") -> GateInput:
    return GateInput(
        feature_id=feature_id,
        feature_goal="Prepare service for production deploy",
        files_changed=files,
        diff="diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n",
    )


# -----------------------------------------------------------------------------
# Applicability short-circuit
# -----------------------------------------------------------------------------


class TestNotApplicable:
    async def test_no_deploy_files_yields_pass(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["src/app.py", "tests/test_app.py"]))

        assert result.passed is True
        assert result.gate_type == QualityGateType.SRE_REVIEW
        assert "not applicable" in result.reason.lower()
        # Critical: reviewer must NOT have been invoked.
        assert runner.calls == [], "Reviewer was invoked despite no deploy files"

    async def test_empty_files_list_yields_pass(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input([]))
        assert result.passed is True
        assert runner.calls == []


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------


class TestGatePasses:
    async def test_approve_yields_pass(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))

        assert result.passed is True
        assert result.details["deploy_files"] == ["Dockerfile"]
        assert result.details["review"]["verdict"] == "approve"

    async def test_migrations_trigger_review(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["backend/migrations/0042_add_users.sql"]))
        assert result.passed is True
        assert len(runner.calls) == 1
        # Sanity: the prompt sent to the reviewer is the SRE one, not
        # another reviewer's.
        _role, prompt, _task = runner.calls[0]
        assert "Senior SRE" in prompt or "senior_sre" in prompt.lower()


# -----------------------------------------------------------------------------
# Blocking path
# -----------------------------------------------------------------------------


class TestGateBlocks:
    async def test_blocker_finding_blocks_gate(self, config):
        runner = MockReviewerRunner(
            {AgentRole.SENIOR_SRE: _blocker("Migration has no rollback path")}
        )
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))

        assert result.passed is False
        assert "Migration has no rollback path" in result.reason
        assert result.allow_retry is True

    async def test_major_finding_also_blocks(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _major("Missing memory limit")})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))

        assert result.passed is False
        assert "Missing memory limit" in result.reason
        findings = result.details["review"]["findings"]
        assert len(findings) == 1
        assert findings[0]["severity"] == "major"


# -----------------------------------------------------------------------------
# Reviewer fault
# -----------------------------------------------------------------------------


class _CrashingRunner:
    async def run(self, *, role: AgentRole, system_prompt: str, task: str) -> str:
        raise RuntimeError("subprocess died")


class TestReviewerFault:
    async def test_crash_blocks_gate_not_silent_approve(self, config):
        gate = SreReviewGate(runner=_CrashingRunner(), config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))

        assert result.passed is False
        assert result.allow_retry is True
        assert "could not complete" in result.reason.lower()
        assert result.details["review"] is None

    async def test_invalid_json_from_reviewer_blocks(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: "not json at all"})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))

        # parse_review_response emits a reviewer_fault finding; gate
        # sees it as MAJOR → blocks.
        assert result.passed is False


# -----------------------------------------------------------------------------
# Config loading
# -----------------------------------------------------------------------------


class TestConfigLoad:
    def test_default_config_loads(self):
        cfg = SreReviewConfig.load()
        assert cfg.deploy_surface_patterns
        # Sanity: core patterns must be present.
        assert "Dockerfile" in cfg.deploy_surface_patterns

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SreReviewConfig.load(tmp_path / "does-not-exist.yaml")

    def test_malformed_config_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            textwrap.dedent(
                """
                version: 1
                # missing deploy_surface key
                """
            )
        )
        with pytest.raises(ValueError, match="Malformed"):
            SreReviewConfig.load(bad)


# -----------------------------------------------------------------------------
# Report
# -----------------------------------------------------------------------------


class TestRenderReport:
    async def test_pass_report_lists_deploy_files(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))
        md = render_sre_review_report(result)

        assert "[PASS]" in md
        assert "Dockerfile" in md
        assert "## Deploy-surface files" in md

    async def test_block_report_shows_findings(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _blocker("No rollback")})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))
        md = render_sre_review_report(result)

        assert "[BLOCK]" in md
        assert "## Findings" in md
        assert "No rollback" in md
        assert "Why:" in md
        assert "Fix:" in md

    async def test_report_emoji_free(self, config):
        runner = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        gate = SreReviewGate(runner=runner, config=config)
        result = await gate.check(_mk_input(["Dockerfile"]))
        md = render_sre_review_report(result)
        for forbidden in ("✅", "❌", "🚧"):
            assert forbidden not in md


# -----------------------------------------------------------------------------
# Convenience function
# -----------------------------------------------------------------------------


class TestFunctionWrapper:
    async def test_function_and_class_agree(self, config):
        runner_a = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        runner_b = MockReviewerRunner({AgentRole.SENIOR_SRE: _approve()})
        inp = _mk_input(["Dockerfile"])

        via_class = await SreReviewGate(runner=runner_a, config=config).check(inp)
        via_func = await run_sre_review_gate(inp, runner_b, config=config)

        assert via_class.passed == via_func.passed

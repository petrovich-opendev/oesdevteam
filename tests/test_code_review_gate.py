"""Tests for the blocking code-review gate (Step 3).

These tests answer:
  1. Does the gate PASS when all five reviewers approve with no findings?
  2. Does the gate BLOCK on any blocker from any reviewer?
  3. Does a majors-only review still block (v2 rule: majors are blockers too)?
  4. Does a reviewer-infrastructure failure block and suggest retry?
  5. Is the reason string useful at a glance?
  6. Does the Markdown report render cleanly on a passing and a blocking result?
  7. Is the passed result's ``aggregate_verdict`` actually APPROVE (no silent pass)?
"""

from __future__ import annotations

import json

import pytest

from src.gates import CodeReviewGate, GateInput, run_code_review_gate
from src.gates.code_review_gate import render_code_review_report
from src.models import AgentRole, QualityGateType
from src.reviewers import REVIEWER_ROLES, MockReviewerRunner

# -----------------------------------------------------------------------------
# Canned reviewer responses
# -----------------------------------------------------------------------------


def _approve(reviewer: str) -> str:
    return json.dumps(
        {
            "reviewer": reviewer,
            "verdict": "approve",
            "findings": [],
            "positive_notes": ["Looks good."],
        }
    )


def _blocker(reviewer: str, summary: str, category: str = "security") -> str:
    return json.dumps(
        {
            "reviewer": reviewer,
            "verdict": "needs_rework",
            "findings": [
                {
                    "severity": "blocker",
                    "file": "src/app.py",
                    "line": 42,
                    "category": category,
                    "summary": summary,
                    "why": "Will break production.",
                    "fix": "Fix it the right way.",
                }
            ],
        }
    )


def _major(reviewer: str, summary: str) -> str:
    return json.dumps(
        {
            "reviewer": reviewer,
            "verdict": "needs_rework",
            "findings": [
                {
                    "severity": "major",
                    "file": "src/x.py",
                    "line": 10,
                    "category": "correctness",
                    "summary": summary,
                    "why": "Will cause user pain.",
                    "fix": "Address before merge.",
                }
            ],
        }
    )


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def gate_input() -> GateInput:
    return GateInput(
        feature_id="GATE-001",
        feature_goal="Add /health endpoint",
        files_changed=["src/app.py", "tests/test_app.py"],
        diff="@@ -1 +1,2 @@\n old\n+new\n",
        verify_commands=["pytest", "curl -s localhost:8000/health"],
        domain_context="Generic FastAPI service.",
    )


def _all_approve() -> dict[AgentRole, str]:
    return {role: _approve(role.value) for role in REVIEWER_ROLES}


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------


class TestGatePasses:
    async def test_all_approve_yields_pass(self, gate_input):
        runner = MockReviewerRunner(_all_approve())
        result = await run_code_review_gate(gate_input, runner)

        assert result.passed is True
        assert result.gate_type == QualityGateType.SENIOR_REVIEW
        assert "approved" in result.reason.lower()

    async def test_pass_details_contains_approve_verdict(self, gate_input):
        runner = MockReviewerRunner(_all_approve())
        result = await run_code_review_gate(gate_input, runner)
        assert result.details["aggregate_verdict"] == "approve"
        assert result.details["blockers"] == []
        assert result.details["majors"] == []

    async def test_class_form_matches_function_form(self, gate_input):
        """``CodeReviewGate`` and ``run_code_review_gate`` must agree."""
        runner_a = MockReviewerRunner(_all_approve())
        runner_b = MockReviewerRunner(_all_approve())

        via_func = await run_code_review_gate(gate_input, runner_a)
        via_class = await CodeReviewGate(runner=runner_b).check(gate_input)

        assert via_func.passed == via_class.passed
        assert via_func.gate_type == via_class.gate_type


# -----------------------------------------------------------------------------
# Blocking path
# -----------------------------------------------------------------------------


class TestGateBlocks:
    async def test_single_blocker_blocks_the_gate(self, gate_input):
        responses = _all_approve()
        responses[AgentRole.SENIOR_BACKEND] = _blocker("senior_backend", "SQL string concatenation")
        runner = MockReviewerRunner(responses)
        result = await run_code_review_gate(gate_input, runner)

        assert result.passed is False
        assert "blocker" in result.reason.lower()
        assert result.allow_retry is True
        assert len(result.details["blockers"]) == 1

    async def test_majors_only_also_block(self, gate_input):
        """v2 rule: majors escalate to needs_rework; gate must respect that."""
        responses = _all_approve()
        responses[AgentRole.SENIOR_BACKEND] = _major("senior_backend", "Missing retry on HTTP 429")
        runner = MockReviewerRunner(responses)
        result = await run_code_review_gate(gate_input, runner)

        assert result.passed is False
        # Proof of correct aggregation (not just string matching):
        assert result.details["aggregate_verdict"] == "needs_rework"
        assert result.details["blockers"] == []
        assert len(result.details["majors"]) == 1
        # Reason surfaces the major via summary — resilient to wording tweaks:
        assert "Missing retry on HTTP 429" in result.reason

    async def test_blocker_reason_cites_most_severe_finding(self, gate_input):
        responses = _all_approve()
        responses[AgentRole.SENIOR_DATA] = _major("senior_data", "cold quadratic")
        responses[AgentRole.SENIOR_BACKEND] = _blocker("senior_backend", "No auth on /admin")
        runner = MockReviewerRunner(responses)
        result = await run_code_review_gate(gate_input, runner)
        # The blocker ought to surface in the reason line over the major.
        assert "No auth on /admin" in result.reason
        # And cold quadratic (major) should NOT be cited as THE reason:
        assert "cold quadratic" not in result.reason


# -----------------------------------------------------------------------------
# Reviewer infrastructure failure
# -----------------------------------------------------------------------------


class _CrashingRunner:
    def __init__(self, crash_role: AgentRole):
        self.crash_role = crash_role

    async def run(self, *, role: AgentRole, system_prompt: str, task: str) -> str:
        if role == self.crash_role:
            raise RuntimeError("subprocess died")
        return _approve(role.value)


class TestReviewerFaultBlocks:
    async def test_one_reviewer_crash_blocks_gate(self, gate_input):
        runner = _CrashingRunner(crash_role=AgentRole.SENIOR_PERFORMANCE)
        result = await run_code_review_gate(gate_input, runner)

        assert result.passed is False
        assert result.allow_retry is True, (
            "Reviewer infrastructure faults should be retryable — a hung "
            "CLI on the first attempt is often fine on the second."
        )
        # The detail payload contains per-reviewer info so the controller
        # can see WHICH reviewer failed.
        per_reviewer = {r["name"]: r for r in result.details["reviewers"]}
        assert per_reviewer["senior_performance"]["verdict"] == "needs_rework"


# -----------------------------------------------------------------------------
# Markdown report
# -----------------------------------------------------------------------------


class TestRenderCodeReviewReport:
    async def test_pass_report_mentions_pass_and_each_reviewer(self, gate_input):
        runner = MockReviewerRunner(_all_approve())
        result = await run_code_review_gate(gate_input, runner)
        md = render_code_review_report(result)

        assert "PASS" in md
        # Every reviewer should be listed in the per-reviewer table.
        for role in REVIEWER_ROLES:
            assert role.value in md

    async def test_block_report_has_blocker_section(self, gate_input):
        responses = _all_approve()
        responses[AgentRole.SENIOR_BACKEND] = _blocker(
            "senior_backend", "path traversal via unsafe join"
        )
        runner = MockReviewerRunner(responses)
        result = await run_code_review_gate(gate_input, runner)
        md = render_code_review_report(result)

        assert "BLOCK" in md
        assert "## Blockers" in md
        assert "path traversal" in md

    async def test_block_report_shows_majors_section_when_present(self, gate_input):
        responses = _all_approve()
        responses[AgentRole.SENIOR_DATA] = _major("senior_data", "missing DELETE+INSERT")
        runner = MockReviewerRunner(responses)
        result = await run_code_review_gate(gate_input, runner)
        md = render_code_review_report(result)

        assert "## Majors" in md
        assert "missing DELETE+INSERT" in md

    async def test_renderer_rejects_wrong_gate_type(self):
        """Defensive — render only code-review outcomes."""
        from src.gates.base import GateResult

        foreign = GateResult(
            gate_type=QualityGateType.STATIC_ANALYSIS,
            passed=True,
            reason="ruff passed",
        )
        with pytest.raises(ValueError):
            render_code_review_report(foreign)

    async def test_renderer_is_grep_friendly_no_emoji(self, gate_input):
        """Project rule: reports must be plain ASCII-safe text."""
        runner = MockReviewerRunner(_all_approve())
        result = await run_code_review_gate(gate_input, runner)
        md = render_code_review_report(result)
        for forbidden in ("✅", "❌", "🚧", "⏳", "✨"):
            assert forbidden not in md, f"Emoji {forbidden!r} leaked into report"


# -----------------------------------------------------------------------------
# Business-goal alignment
# -----------------------------------------------------------------------------


class TestBusinessGoalAlignment:
    """Step 3 contributes to 'production-grade codegen without manual intervention'.

    Without a blocking gate, the Senior Reviewer squad is an elaborate
    advisory system — helpful but not a prerequisite for production code.
    Wiring the verdict into pass/fail is what turns Step 2's work into an
    actual safety rail.
    """

    async def test_blocked_features_cannot_silently_pass(self, gate_input):
        responses = _all_approve()
        # Any single reviewer flagging a blocker MUST block.
        responses[AgentRole.BUSINESS_EXPERT] = _blocker(
            "business_expert",
            "Output uses banned term 'флот' instead of 'парк техники'",
            category="terminology",
        )
        runner = MockReviewerRunner(responses)
        result = await run_code_review_gate(gate_input, runner)
        assert result.passed is False

    async def test_passing_features_have_real_approval(self, gate_input):
        """A gate PASS must be accompanied by aggregate_verdict=approve."""
        runner = MockReviewerRunner(_all_approve())
        result = await run_code_review_gate(gate_input, runner)
        assert result.passed is True
        assert result.details["aggregate_verdict"] == "approve"

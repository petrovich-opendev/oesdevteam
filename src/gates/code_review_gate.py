"""Code Review Gate — blocks a commit unless the Senior squad approves.

This is the Step 3 deliverable: the point in the pipeline where the
Senior Reviewer squad's verdict actually prevents a broken change from
landing in git. v1 of DevTeam had a reviewer-like agent but its opinion
was advisory. v2 makes it binding.

Decision rule
-------------
The gate passes iff ``SquadResult.aggregate_verdict == APPROVE``. That
is strictly pessimistic: a single reviewer's ``needs_rework`` OR a
single BLOCKER finding anywhere in the squad flips the gate to
``blocked``. See :mod:`src.reviewers.findings` for the aggregation
semantics.

Why a class (``CodeReviewGate``) plus a function
(``run_code_review_gate``)
-----------------------------------------------
The function is convenient for simple, one-shot calls (the typical
controller usage). The class holds configuration — runner, role list,
timeout — when the controller wants to construct it once and reuse
across features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import AgentRole, QualityGateType
from ..reviewers import (
    REVIEWER_ROLES,
    ReviewerRunner,
    ReviewInput,
    SquadResult,
    Verdict,
    run_reviewer_squad,
)
from ..reviewers.findings import Severity
from .base import GateInput, GateResult

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Default wall-clock cap for one gate evaluation.
#
# 600 seconds = 10 minutes. Rationale: in v1 production data a typical
# reviewer call takes 60-120 s end-to-end; five in parallel with some
# queue jitter fits comfortably in 5 min, and 10 min gives a 2× safety
# margin. Longer than this and "reviewer stuck in a tool loop" becomes
# more likely than "reviewer still working" — a timeout here is cheaper
# than letting costs run away. Matches
# ``src.reviewers.squad.DEFAULT_SQUAD_TIMEOUT_SECONDS`` so the
# controller's expectations line up with the squad's internals.
DEFAULT_GATE_TIMEOUT_SECONDS = 600


@dataclass
class CodeReviewGate:
    """Callable gate object reusable across features.

    Attributes:
        runner: LLM backend (``ClaudeCliReviewerRunner`` in production,
            ``MockReviewerRunner`` in tests).
        roles: Which reviewer roles to involve. Defaults to the full
            five-role squad; rare subsets (e.g. docs-only PR) may pass
            ``(SENIOR_BACKEND,)``. Duplicate roles are rejected by the
            squad runner.
        squad_timeout_seconds: Overall wall-time cap. See the squad
            module for the graceful-cancellation behaviour.
    """

    runner: ReviewerRunner
    roles: tuple[AgentRole, ...] = field(default=REVIEWER_ROLES)
    squad_timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS

    # Protocol attribute — identifies this gate in QualityGateType-driven
    # dispatch tables. Keeping it as a class attribute (not an instance
    # field) avoids every caller having to pass it in.
    gate_type: QualityGateType = field(
        default=QualityGateType.SENIOR_REVIEW,
        init=False,
    )

    async def check(self, gate_input: GateInput) -> GateResult:
        """Run the squad and convert the ``SquadResult`` into a ``GateResult``.

        Failure modes (return ``passed=False``):
          1. Any reviewer returned ``NEEDS_REWORK``.
          2. Any BLOCKER finding surfaced across the squad.
          3. Any reviewer crashed or timed out (``reviewer_fault``).

        In every blocked case the gate returns ``allow_retry=True`` —
        re-running the squad may resolve the issue (reviewer
        infrastructure flakes, or the controller feeds back a rework
        prompt). The controller decides how many retries to attempt.
        """
        review_input = _review_input_from_gate_input(gate_input)

        squad_result: SquadResult = await run_reviewer_squad(
            review_input,
            self.runner,
            roles=self.roles,
            squad_timeout_seconds=self.squad_timeout_seconds,
        )

        if squad_result.aggregate_verdict == Verdict.APPROVE:
            return GateResult(
                gate_type=self.gate_type,
                passed=True,
                reason=(
                    f"All {len(squad_result.reviews)} Senior Reviewers approved "
                    f"without blocker or major findings."
                ),
                details=_squad_details(squad_result, passed=True),
            )

        # Blocked: compose a reason that is useful in a single log line.
        reason = _format_block_reason(squad_result)
        return GateResult(
            gate_type=self.gate_type,
            passed=False,
            reason=reason,
            details=_squad_details(squad_result, passed=False),
            allow_retry=True,
        )


# -----------------------------------------------------------------------------
# Functional wrapper
# -----------------------------------------------------------------------------


async def run_code_review_gate(
    gate_input: GateInput,
    runner: ReviewerRunner,
    *,
    roles: tuple[AgentRole, ...] = REVIEWER_ROLES,
    squad_timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS,
) -> GateResult:
    """One-shot convenience wrapper around :class:`CodeReviewGate`.

    Use this when the controller is operating at the feature level and
    does not retain gate objects between calls.
    """
    gate = CodeReviewGate(
        runner=runner,
        roles=roles,
        squad_timeout_seconds=squad_timeout_seconds,
    )
    return await gate.check(gate_input)


# -----------------------------------------------------------------------------
# Internal helpers — keep the public class small and readable
# -----------------------------------------------------------------------------


def _review_input_from_gate_input(gate_input: GateInput) -> ReviewInput:
    """Narrow the generic ``GateInput`` into the squad's ``ReviewInput``.

    The squad has no need for fields the gate didn't populate; we copy
    only what it reads. Keeping the transformation explicit makes future
    additions to ``GateInput`` (e.g. security-scan artefacts) unambiguous.
    """
    return ReviewInput(
        feature_id=gate_input.feature_id,
        feature_goal=gate_input.feature_goal,
        files_changed=gate_input.files_changed,
        diff=gate_input.diff,
        domain_context=gate_input.domain_context,
        verify_commands=gate_input.verify_commands,
    )


def _squad_details(squad_result: SquadResult, *, passed: bool) -> dict[str, Any]:
    """Build the ``GateResult.details`` payload from a SquadResult.

    Kept as a plain dict (not another Pydantic model) because the
    controller publishes ``details`` verbatim over NATS; dicts round-trip
    through JSON without ``model_dump`` ceremony.
    """
    return {
        "passed": passed,
        "aggregate_verdict": squad_result.aggregate_verdict.value,
        "blockers": [f.model_dump() for f in squad_result.blockers()],
        "majors": [f.model_dump() for f in squad_result.majors()],
        "reviewers": [
            {
                "name": r.reviewer,
                "verdict": r.verdict.value,
                "findings_count": len(r.findings),
                "blocker_count": len([f for f in r.findings if f.severity == Severity.BLOCKER]),
            }
            for r in squad_result.reviews
        ],
    }


def _format_block_reason(squad_result: SquadResult) -> str:
    """One-line human-readable rationale for a blocked gate.

    Worked examples:
      - "Senior review blocked: 2 blocker(s) + 1 major(s) (senior_backend: SQL
        concatenation at src/app.py:42)."
      - "Senior review blocked: needs_rework from senior_data."
    """
    blockers = squad_result.blockers()
    majors = squad_result.majors()
    parts: list[str] = []

    if blockers:
        parts.append(f"{len(blockers)} blocker(s)")
    if majors:
        parts.append(f"{len(majors)} major(s)")

    if parts:
        # Pick the most severe finding as a representative for the reason
        # line — readers skim this first and want the worst thing that
        # happened. We draw ONLY from blockers+majors (severity-ordered
        # by ``all_findings`` does not give that guarantee if, somehow,
        # a reviewer returned verdict=approve+minor and another reviewer
        # verdict=needs_rework+major — see B-1 review note).
        representative = (blockers + majors)[0]
        location = (
            f"{representative.file}:{representative.line}"
            if representative.line
            else representative.file
        )
        return (
            f"Senior review blocked: {' + '.join(parts)} "
            f"(e.g. [{representative.severity.value}] "
            f"{representative.summary} @ {location})."
        )

    # No blocker/major findings yet aggregate_verdict is NEEDS_REWORK:
    # a reviewer returned ``needs_rework`` without supplying findings
    # (arguably a reviewer contract bug, but we still have to block).
    rejectors = [r.reviewer for r in squad_result.reviews if r.verdict == Verdict.NEEDS_REWORK]
    return f"Senior review blocked: needs_rework from {', '.join(rejectors) or 'reviewer(s)'}."


# -----------------------------------------------------------------------------
# Report renderer — Markdown for the pipeline log / PR description
# -----------------------------------------------------------------------------


def render_code_review_report(result: GateResult) -> str:
    """Render a full Markdown report for a code-review gate outcome.

    Call this when writing the feature's ``needs_rework`` feedback file
    or when appending to ``pipeline-log/<feature>.md``. The output is
    intentionally plain Markdown so it renders both in a GitHub comment
    and in a terminal ``mdcat`` viewer.
    """
    if result.gate_type != QualityGateType.SENIOR_REVIEW:
        raise ValueError(
            "render_code_review_report expects gate_type SENIOR_REVIEW, "
            f"got {result.gate_type.value!r}"
        )

    # No emoji: keeps the report grep-friendly and monospaced-column
    # aligned in CI logs (project rule — see CLAUDE.md).
    head = "[PASS]" if result.passed else "[BLOCK]"
    lines: list[str] = [f"# Senior Review Gate — {head}", "", f"_{result.reason}_", ""]

    reviewers = result.details.get("reviewers", [])
    if reviewers:
        lines += ["## Per-reviewer verdict", ""]
        lines += ["| Reviewer | Verdict | Findings | Blockers |", "|---|---|---|---|"]
        for r in reviewers:
            lines.append(
                f"| `{r['name']}` | {r['verdict']} | {r['findings_count']} | {r['blocker_count']} |"
            )
        lines.append("")

    blockers = result.details.get("blockers", [])
    if blockers:
        lines += ["## Blockers", ""]
        for f in blockers:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            lines += [
                f"### `{f['category']}` — {f['summary']} @ `{loc}`",
                "",
                f"**Why:** {f['why']}",
                "",
                f"**Fix:** {f['fix']}",
                "",
            ]

    majors = result.details.get("majors", [])
    if majors:
        lines += ["## Majors", ""]
        for f in majors:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            lines += [f"- [{f['category']}] {f['summary']} @ `{loc}` — {f['fix']}"]
        lines.append("")

    return "\n".join(lines)

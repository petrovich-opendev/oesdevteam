"""SRE Review Gate — blocks a commit when deploy-layer changes look risky.

Where this fits
---------------
The five-reviewer Senior squad (Step 2) focuses on application code.
Most features only need that. But some features change the deploy
surface — Dockerfiles, k8s manifests, nginx config, migrations, CI
workflows. Those have a different failure mode: the app works fine in
isolation but breaks in production at rollout, degrades badly when
rolled back, or silently removes observability the on-call team relies
on.

This gate pulls a single specialised reviewer — ``SENIOR_SRE`` — and
runs it ONLY when a feature's changed files match the deploy-surface
patterns in ``config/sre_review.yaml``. Non-deploy features
short-circuit to PASS with ``reason="not applicable"``.

Design parallels
----------------
The gate deliberately reuses:
- The ``ReviewerRunner`` interface from Step 2, so mocks from the
  squad tests plug in unchanged.
- The ``parse_review_response`` parser, so the SRE reviewer's JSON
  contract is identical to the five squad reviewers'.
- The ``GateInput`` / ``GateResult`` shape from Step 3.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models import AgentRole, QualityGateType
from ..reviewers.findings import (
    ReviewInput,
    ReviewResult,
    Severity,
    Verdict,
    parse_review_response,
)
from ..reviewers.runner import ReviewerRunner
from ..reviewers.squad import build_task_message, load_reviewer_prompt
from .base import GateInput, GateResult

# -----------------------------------------------------------------------------
# Configuration — which files are "deploy surface"
# -----------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sre_review.yaml"

# Hard cap on one SRE review. Deploy reviews tend to be shorter than
# five-reviewer squads (single reviewer, narrow focus) but still involve
# reading manifests / scripts / migrations; 180 s is generous without
# inviting cost-runaway.
DEFAULT_SRE_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class SreReviewConfig:
    """fnmatch patterns identifying files that require SRE review."""

    deploy_surface_patterns: tuple[str, ...]

    @staticmethod
    def load(path: Path | None = None) -> SreReviewConfig:
        """Load patterns from YAML.

        Raises:
            FileNotFoundError: if the config file is missing.
            ValueError: if the YAML lacks the required ``deploy_surface``
                / ``patterns`` keys.
        """
        cfg_path = path or _DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"SRE review config not found: {cfg_path}")

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        try:
            patterns = tuple(raw["deploy_surface"]["patterns"])
        except (KeyError, TypeError) as e:
            raise ValueError(f"Malformed SRE review config at {cfg_path}: {e}") from e

        return SreReviewConfig(deploy_surface_patterns=patterns)


def _match_any(path: str, patterns: tuple[str, ...]) -> bool:
    """True if ``path`` matches any fnmatch pattern (repo-root relative)."""
    normalised = path.replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatchcase(normalised, p) for p in patterns)


# -----------------------------------------------------------------------------
# The gate
# -----------------------------------------------------------------------------


@dataclass
class SreReviewGate:
    """Blocking deploy-readiness review.

    Reuses the reviewer infrastructure (``ReviewerRunner``,
    ``parse_review_response``) so the SRE reviewer is structurally the
    same kind of agent as the Senior squad — just called through a
    different gate with a different prompt and a tighter applicability
    filter.
    """

    runner: ReviewerRunner
    config: SreReviewConfig
    timeout_seconds: int = DEFAULT_SRE_TIMEOUT_SECONDS
    gate_type: QualityGateType = field(
        default=QualityGateType.SRE_REVIEW,
        init=False,
    )

    @classmethod
    def from_default(cls, runner: ReviewerRunner) -> SreReviewGate:
        """Build a gate using the repo-root ``config/sre_review.yaml``."""
        return cls(runner=runner, config=SreReviewConfig.load())

    async def check(self, gate_input: GateInput) -> GateResult:
        """Evaluate deploy readiness; skip when the change is not deploy-adjacent."""
        deploy_files = [
            p
            for p in (gate_input.files_changed or [])
            if _match_any(p, self.config.deploy_surface_patterns)
        ]

        if not deploy_files:
            return GateResult(
                gate_type=self.gate_type,
                passed=True,
                reason="SRE review not applicable: no deploy-surface files touched.",
                details={"deploy_files": [], "review": None},
                allow_retry=False,
            )

        # The reviewer uses the same user-message plumbing as the squad,
        # but receives only this one role's prompt.
        review_input = ReviewInput(
            feature_id=gate_input.feature_id,
            feature_goal=gate_input.feature_goal,
            files_changed=gate_input.files_changed,
            diff=gate_input.diff,
            domain_context="",  # SRE review is domain-agnostic
            verify_commands=gate_input.verify_commands,
        )

        try:
            prompt = load_reviewer_prompt(AgentRole.SENIOR_SRE)
            task = build_task_message(review_input)
            raw = await self.runner.run(
                role=AgentRole.SENIOR_SRE,
                system_prompt=prompt,
                task=task,
            )
            review: ReviewResult = parse_review_response(
                raw, reviewer_name=AgentRole.SENIOR_SRE.value
            )
        except Exception as e:  # noqa: BLE001 - structured fault surface
            return _fault_result(self.gate_type, deploy_files, repr(e))

        has_blocker = any(f.severity == Severity.BLOCKER for f in review.findings)
        has_major = any(f.severity == Severity.MAJOR for f in review.findings)
        passed = review.verdict == Verdict.APPROVE and not has_blocker and not has_major

        if passed:
            return GateResult(
                gate_type=self.gate_type,
                passed=True,
                reason=(
                    f"SRE review approved {len(deploy_files)} deploy-surface "
                    "file(s) with no blocker or major findings."
                ),
                details=_details(deploy_files, review),
            )

        # Blocked — quote the worst finding for the reason line.
        worst = _worst_finding(review)
        loc = f"{worst.file}:{worst.line}" if worst.line else worst.file
        reason = f"SRE review blocked: [{worst.severity.value}] {worst.summary} @ {loc}"
        return GateResult(
            gate_type=self.gate_type,
            passed=False,
            reason=reason,
            details=_details(deploy_files, review),
            allow_retry=True,
        )


# -----------------------------------------------------------------------------
# Convenience function
# -----------------------------------------------------------------------------


async def run_sre_review_gate(
    gate_input: GateInput,
    runner: ReviewerRunner,
    *,
    config: SreReviewConfig | None = None,
    timeout_seconds: int = DEFAULT_SRE_TIMEOUT_SECONDS,
) -> GateResult:
    """Single-call helper around :class:`SreReviewGate`."""
    gate = SreReviewGate(
        runner=runner,
        config=config or SreReviewConfig.load(),
        timeout_seconds=timeout_seconds,
    )
    return await gate.check(gate_input)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _worst_finding(review: ReviewResult):
    """Pick the most severe finding; prefer blockers, then majors, then minors."""
    by_severity = {
        Severity.BLOCKER: [f for f in review.findings if f.severity == Severity.BLOCKER],
        Severity.MAJOR: [f for f in review.findings if f.severity == Severity.MAJOR],
        Severity.MINOR: [f for f in review.findings if f.severity == Severity.MINOR],
    }
    for level in (Severity.BLOCKER, Severity.MAJOR, Severity.MINOR):
        if by_severity[level]:
            return by_severity[level][0]
    # Should never reach here when gate has decided to block — but the
    # defensive path keeps the caller's invariants intact even on
    # contract-violating reviewer output.
    return review.findings[0] if review.findings else None


def _details(deploy_files: list[str], review: ReviewResult) -> dict[str, Any]:
    return {
        "deploy_files": deploy_files,
        "review": {
            "verdict": review.verdict.value,
            "findings": [f.model_dump() for f in review.findings],
            "positive_notes": review.positive_notes,
        },
    }


def _fault_result(
    gate_type: QualityGateType,
    deploy_files: list[str],
    reason: str,
) -> GateResult:
    """Structured failure when the reviewer subprocess / parser crashed."""
    return GateResult(
        gate_type=gate_type,
        passed=False,
        reason=(f"SRE review could not complete — silence is not consent. Cause: {reason}"),
        details={
            "deploy_files": deploy_files,
            "review": None,
            "fault_reason": reason,
        },
        allow_retry=True,
    )


# -----------------------------------------------------------------------------
# Report rendering
# -----------------------------------------------------------------------------


def render_sre_review_report(result: GateResult) -> str:
    """Render an SRE review outcome as plain-Markdown (no emoji)."""
    head = "[PASS]" if result.passed else "[BLOCK]"
    lines: list[str] = [f"# SRE Review Gate — {head}", "", f"_{result.reason}_", ""]

    deploy_files = result.details.get("deploy_files", [])
    if deploy_files:
        lines += ["## Deploy-surface files", ""]
        lines += [f"- `{f}`" for f in deploy_files]
        lines.append("")

    review = result.details.get("review")
    if review and review.get("findings"):
        lines += ["## Findings", ""]
        for f in review["findings"]:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            lines += [
                f"- **[{f['severity']}]** `{f['category']}` — {f['summary']} @ `{loc}`",
                f"  - Why: {f['why']}",
                f"  - Fix: {f['fix']}",
                "",
            ]

    return "\n".join(lines)

"""Tests for the Finding / ReviewResult / SquadResult schema and parser.

These tests answer: can the squad digest noisy real-world LLM output
into a deterministic verdict, and does the aggregate verdict behave
correctly as individual reviews combine?
"""

from __future__ import annotations

import pytest

from src.reviewers.findings import (
    Finding,
    ReviewResult,
    Severity,
    SquadResult,
    Verdict,
    parse_review_response,
    severity_rank,
)

# -----------------------------------------------------------------------------
# Severity ordering
# -----------------------------------------------------------------------------


class TestSeverity:
    """Severity must order consistently so aggregators surface blockers first."""

    def test_rank_ordering(self):
        assert severity_rank(Severity.BLOCKER) > severity_rank(Severity.MAJOR)
        assert severity_rank(Severity.MAJOR) > severity_rank(Severity.MINOR)

    def test_rank_accepts_string(self):
        assert severity_rank("blocker") == severity_rank(Severity.BLOCKER)


# -----------------------------------------------------------------------------
# parse_review_response — tolerant of LLM cosmetic habits
# -----------------------------------------------------------------------------

VALID_PAYLOAD = """
{
  "reviewer": "senior_backend",
  "verdict": "needs_rework",
  "findings": [
    {
      "severity": "blocker",
      "file": "src/app.py",
      "line": 42,
      "category": "security",
      "summary": "SQL concatenation",
      "why": "Attacker can drop tables.",
      "fix": "Use parameterised queries."
    }
  ],
  "positive_notes": []
}
"""


class TestParseReviewResponse:
    def test_parses_raw_json(self):
        result = parse_review_response(VALID_PAYLOAD, reviewer_name="senior_backend")
        assert result.verdict == Verdict.NEEDS_REWORK
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.BLOCKER

    def test_parses_json_inside_markdown_fence(self):
        wrapped = f"Here is my review:\n\n```json\n{VALID_PAYLOAD}\n```\nLet me know."
        result = parse_review_response(wrapped, reviewer_name="senior_backend")
        assert result.verdict == Verdict.NEEDS_REWORK
        assert result.findings[0].category == "security"

    def test_parses_bare_fence(self):
        wrapped = f"```\n{VALID_PAYLOAD}\n```"
        result = parse_review_response(wrapped, reviewer_name="senior_backend")
        assert result.verdict == Verdict.NEEDS_REWORK

    def test_parses_with_leading_prose(self):
        noisy = f"Sure, here is my verdict.\n\n{VALID_PAYLOAD}\n\n(end of review)"
        result = parse_review_response(noisy, reviewer_name="senior_backend")
        assert result.verdict == Verdict.NEEDS_REWORK

    def test_reviewer_name_is_overridden_by_caller(self):
        """Even if the LLM reports the wrong name, the orchestrator is authoritative."""
        mislabelled = VALID_PAYLOAD.replace("senior_backend", "SOME_OTHER_NAME")
        result = parse_review_response(mislabelled, reviewer_name="senior_backend")
        assert result.reviewer == "senior_backend"

    def test_invalid_json_yields_reviewer_fault(self):
        result = parse_review_response("not json at all", reviewer_name="senior_backend")
        assert result.verdict == Verdict.NEEDS_REWORK
        assert len(result.findings) == 1
        assert result.findings[0].category == "reviewer_fault"

    def test_wrong_schema_yields_reviewer_fault(self):
        bad = '{"reviewer": "senior_backend", "verdict": "definitely-merge"}'
        result = parse_review_response(bad, reviewer_name="senior_backend")
        assert result.findings[0].category == "reviewer_fault"

    def test_empty_response_yields_reviewer_fault(self):
        result = parse_review_response("", reviewer_name="senior_backend")
        assert result.findings[0].category == "reviewer_fault"


# -----------------------------------------------------------------------------
# ReviewResult helpers
# -----------------------------------------------------------------------------


class TestReviewResult:
    def test_blockers_filter(self):
        r = ReviewResult(
            reviewer="senior_backend",
            verdict=Verdict.NEEDS_REWORK,
            findings=[
                _finding(severity=Severity.BLOCKER, summary="critical"),
                _finding(severity=Severity.MINOR, summary="nit"),
            ],
        )
        blockers = r.blockers()
        assert len(blockers) == 1
        assert blockers[0].summary == "critical"


# -----------------------------------------------------------------------------
# SquadResult aggregation — the contract with the blocking gate
# -----------------------------------------------------------------------------


def _finding(
    *,
    severity: Severity = Severity.MAJOR,
    file: str = "src/x.py",
    line: int | None = 1,
    category: str = "test",
    summary: str = "s",
    why: str = "w",
    fix: str = "f",
) -> Finding:
    return Finding(
        severity=severity,
        file=file,
        line=line,
        category=category,
        summary=summary,
        why=why,
        fix=fix,
    )


def _review(
    reviewer: str,
    verdict: Verdict = Verdict.APPROVE,
    findings: list[Finding] | None = None,
) -> ReviewResult:
    return ReviewResult(
        reviewer=reviewer,
        verdict=verdict,
        findings=findings or [],
    )


class TestSquadResult:
    def test_all_approve_yields_approve(self):
        squad = SquadResult(
            reviews=[
                _review("senior_backend"),
                _review("senior_frontend"),
                _review("senior_data"),
            ]
        )
        assert squad.aggregate_verdict == Verdict.APPROVE

    def test_any_needs_rework_flips_verdict(self):
        squad = SquadResult(
            reviews=[
                _review("senior_backend"),
                _review("senior_frontend", verdict=Verdict.NEEDS_REWORK),
                _review("senior_data"),
            ]
        )
        assert squad.aggregate_verdict == Verdict.NEEDS_REWORK

    def test_single_blocker_wins_over_four_approves(self):
        """Intentionally pessimistic: a blocker from any reviewer blocks the gate."""
        squad = SquadResult(
            reviews=[
                _review("senior_backend"),
                _review("senior_frontend"),
                _review("senior_data"),
                _review("senior_performance"),
                # Approver who nonetheless reports a blocker — the gate
                # trusts the finding severity over the reviewer verdict.
                _review(
                    "business_expert",
                    verdict=Verdict.APPROVE,
                    findings=[_finding(severity=Severity.BLOCKER)],
                ),
            ]
        )
        assert squad.aggregate_verdict == Verdict.NEEDS_REWORK

    def test_all_minor_findings_still_approve(self):
        squad = SquadResult(
            reviews=[
                _review(
                    "senior_backend",
                    findings=[_finding(severity=Severity.MINOR)],
                ),
                _review(
                    "senior_data",
                    findings=[_finding(severity=Severity.MINOR)],
                ),
            ]
        )
        assert squad.aggregate_verdict == Verdict.APPROVE

    def test_all_findings_sorted_most_severe_first(self):
        squad = SquadResult(
            reviews=[
                _review(
                    "senior_backend",
                    findings=[_finding(severity=Severity.MINOR, summary="minor1")],
                ),
                _review(
                    "senior_data",
                    findings=[
                        _finding(severity=Severity.BLOCKER, summary="blocker1"),
                        _finding(severity=Severity.MAJOR, summary="major1"),
                    ],
                ),
            ]
        )
        ordered = squad.all_findings()
        assert [f.summary for f in ordered] == ["blocker1", "major1", "minor1"]

    def test_blockers_and_majors_filters(self):
        squad = SquadResult(
            reviews=[
                _review(
                    "senior_backend",
                    findings=[
                        _finding(severity=Severity.BLOCKER, summary="crit"),
                        _finding(severity=Severity.MAJOR, summary="maj"),
                    ],
                )
            ]
        )
        assert [f.summary for f in squad.blockers()] == ["crit"]
        assert [f.summary for f in squad.majors()] == ["maj"]


class TestSchemaValidation:
    """Raw data-class validation rules — catch schema regressions."""

    def test_finding_requires_nonempty_fields(self):
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            Finding(
                severity=Severity.MAJOR,
                file="",
                line=1,
                category="x",
                summary="x",
                why="x",
                fix="x",
            )

    def test_finding_rejects_line_zero(self):
        with pytest.raises(Exception):  # noqa: B017
            Finding(
                severity=Severity.MINOR,
                file="src/x.py",
                line=0,
                category="x",
                summary="x",
                why="x",
                fix="x",
            )

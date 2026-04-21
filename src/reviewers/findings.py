"""Data schema and JSON-parsing for Senior Reviewer output.

Every reviewer returns JSON matching the schema defined here (see
``ReviewResult``). The controller aggregates five ``ReviewResult``s into
one ``SquadResult`` — that is the structure the blocking code-review gate
(Step 3) will consume.

Design notes
------------
- ``Severity`` is ordered (blocker > major > minor); sorting helps surface
  the most serious issues first in downstream UI.
- ``parse_review_response`` accepts fenced / unfenced JSON; LLMs sometimes
  wrap JSON in a ``` code fence despite instructions. We tolerate that
  rather than making the pipeline brittle to LLM cosmetic habits.
- Parsing failures do NOT return silent approvals — they surface as a
  synthetic ``needs_rework`` with a ``reviewer_fault`` finding, so the
  controller can retry or escalate explicitly.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError

# -----------------------------------------------------------------------------
# Enums — severity and verdict
# -----------------------------------------------------------------------------


class Severity(StrEnum):
    """Finding severity, ordered by operational impact.

    The underlying string values are the contract with the LLM output;
    comparisons across the codebase use the enum for readability.
    """

    BLOCKER = "blocker"  # Cannot merge as-is. Production risk.
    MAJOR = "major"  # Fix before merge unless explicitly deferred.
    MINOR = "minor"  # Nit / polish. May merge with a follow-up.


# Numeric ranks for sorting — kept in one place so UI / aggregator use the
# same ordering. Higher rank = more severe.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.BLOCKER: 3,
    Severity.MAJOR: 2,
    Severity.MINOR: 1,
}


def severity_rank(s: Severity | str) -> int:
    """Return a numeric rank for a severity (BLOCKER > MAJOR > MINOR)."""
    return _SEVERITY_RANK[Severity(s)]


class Verdict(StrEnum):
    """Per-reviewer top-level decision."""

    APPROVE = "approve"
    NEEDS_REWORK = "needs_rework"


# -----------------------------------------------------------------------------
# Data classes — one finding, one reviewer's result, the whole squad
# -----------------------------------------------------------------------------


class Finding(BaseModel):
    """A single issue reported by a reviewer.

    ``line`` is optional because some issues (e.g. "CI config missing")
    don't attach to a specific line. ``file`` is required — a finding
    without a file is too vague to action.
    """

    severity: Severity
    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    category: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    why: str = Field(min_length=1)
    fix: str = Field(min_length=1)


class ReviewResult(BaseModel):
    """Output of one reviewer.

    ``raw_text`` preserves the LLM's original response for debugging when
    parsing drops ambiguous content. It is not shown to downstream
    consumers by default; the blocking gate only reads ``findings``.
    """

    reviewer: str = Field(min_length=1)
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    positive_notes: list[str] = Field(default_factory=list)
    raw_text: str | None = None

    def blockers(self) -> list[Finding]:
        """Return only the blocker-severity findings, for quick inspection."""
        return [f for f in self.findings if f.severity == Severity.BLOCKER]


class ReviewInput(BaseModel):
    """What the controller hands each reviewer.

    The same payload goes to every reviewer except ``domain_context``,
    which is only filled for the Business Domain Expert (see
    ``squad.load_reviewer_prompt``).
    """

    feature_id: str
    feature_goal: str
    files_changed: list[str]
    diff: str  # unified diff or equivalent
    domain_context: str = ""  # filled from namespaces/<env>/<domain>/CLAUDE.md
    verify_commands: list[str] = Field(default_factory=list)


class SquadResult(BaseModel):
    """Aggregate of every reviewer's ``ReviewResult`` for a single gate call.

    The blocking code-review gate (Step 3) asks two questions of this
    object: ``aggregate_verdict`` and ``blockers()``. Both are pure
    functions of the contained reviews — deterministic and auditable.
    """

    reviews: list[ReviewResult]

    @property
    def aggregate_verdict(self) -> Verdict:
        """Overall verdict: APPROVE only if EVERY reviewer approved.

        Any ``needs_rework`` from any reviewer, OR any BLOCKER finding
        anywhere, flips the squad verdict. A single reviewer cannot be
        out-voted — the gate is strictly pessimistic on purpose: a BLOCKER
        missed by four reviewers but caught by the fifth is still a
        BLOCKER.
        """
        if any(r.verdict == Verdict.NEEDS_REWORK for r in self.reviews):
            return Verdict.NEEDS_REWORK
        if any(f.severity == Severity.BLOCKER for r in self.reviews for f in r.findings):
            return Verdict.NEEDS_REWORK
        return Verdict.APPROVE

    def all_findings(self) -> list[Finding]:
        """Flatten findings across reviewers, most severe first."""
        out: list[Finding] = [f for r in self.reviews for f in r.findings]
        out.sort(key=lambda f: severity_rank(f.severity), reverse=True)
        return out

    def blockers(self) -> list[Finding]:
        """All BLOCKER findings across the squad."""
        return [f for f in self.all_findings() if f.severity == Severity.BLOCKER]

    def majors(self) -> list[Finding]:
        """All MAJOR findings across the squad."""
        return [f for f in self.all_findings() if f.severity == Severity.MAJOR]


# -----------------------------------------------------------------------------
# Parsing — LLM output → ReviewResult
# -----------------------------------------------------------------------------

# Match a ```json ... ``` or bare ``` ... ``` fenced block. LLMs commonly
# wrap JSON in these even when told not to; we tolerate both forms.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_payload(raw: str) -> str:
    """Return the JSON slice of an LLM response, with fences stripped.

    Heuristic:
      1. If the response contains a code fence, return the first fenced
         payload.
      2. Otherwise return the substring between the first '{' and the
         last '}' (inclusive) — this handles chatty LLMs that prefix
         prose to the JSON.
      3. If neither is present, return the raw text; the JSON decoder
         will raise a readable error.
    """
    if not raw:
        return raw

    fenced = _FENCE_RE.search(raw)
    if fenced:
        return fenced.group(1).strip()

    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return raw[first_brace : last_brace + 1]

    return raw.strip()


def parse_review_response(raw: str, *, reviewer_name: str) -> ReviewResult:
    """Parse an LLM reviewer response into a ``ReviewResult``.

    Args:
        raw: Raw stdout text from the reviewer LLM call.
        reviewer_name: Expected reviewer name (e.g. ``"senior_backend"``).
            If the JSON payload omits or disagrees on the name, the value
            passed here is used — the orchestrator knows who it called.

    Returns:
        A ``ReviewResult``. If parsing fails for any reason, the returned
        result has ``verdict=needs_rework`` and a single ``reviewer_fault``
        finding describing what went wrong — never a silent approval.
    """
    payload = _extract_json_payload(raw)
    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError as e:
        return _reviewer_fault(reviewer_name, raw, f"invalid JSON: {e}")

    if not isinstance(data, dict):
        return _reviewer_fault(
            reviewer_name, raw, f"expected JSON object, got {type(data).__name__}"
        )

    # Force the reviewer name to what we actually called — if the LLM
    # reports a different one we log the discrepancy rather than trust it.
    data["reviewer"] = reviewer_name

    try:
        result = ReviewResult.model_validate(data)
    except ValidationError as e:
        return _reviewer_fault(reviewer_name, raw, f"schema violation: {e}")

    result.raw_text = raw
    return result


def _reviewer_fault(reviewer_name: str, raw: str, reason: str) -> ReviewResult:
    """Build a synthetic needs_rework result for an unparseable response.

    A parse failure is treated as a MAJOR, not a BLOCKER, because the
    underlying code under review might still be fine — we just couldn't
    read the reviewer's opinion. The controller can retry the reviewer
    once; a second failure escalates to a human.
    """
    return ReviewResult(
        reviewer=reviewer_name,
        verdict=Verdict.NEEDS_REWORK,
        findings=[
            Finding(
                severity=Severity.MAJOR,
                file="<reviewer-output>",
                line=None,
                category="reviewer_fault",
                summary=f"Could not parse reviewer response ({reviewer_name})",
                why=(
                    f"Reason: {reason}. Without a parseable verdict the "
                    "blocking gate must be pessimistic and refuse to merge."
                ),
                fix=(
                    "Re-run this reviewer. If the second attempt also fails, "
                    "escalate to a human; do not merge on a silent result."
                ),
            )
        ],
        raw_text=raw,
    )

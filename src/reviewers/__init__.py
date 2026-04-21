"""Senior Reviewer squad — five parallel review agents + aggregation.

Public surface of this package:

- ``Finding``, ``ReviewResult``, ``SquadResult`` — data schema (findings.py)
- ``run_reviewer_squad`` — entry point; accepts a runner, returns SquadResult
- ``REVIEWER_ROLES`` — the canonical five-role tuple
- ``ReviewerRunner``, ``ClaudeCliReviewerRunner``, ``MockReviewerRunner`` —
  pluggable LLM backends; tests use the mock

Nothing else in the codebase should need to reach into this package's
submodules directly.
"""

from __future__ import annotations

from .findings import (
    Finding,
    ReviewInput,
    ReviewResult,
    Severity,
    SquadResult,
    Verdict,
    parse_review_response,
)
from .runner import (
    ClaudeCliReviewerRunner,
    MockReviewerRunner,
    ReviewerRunner,
)
from .squad import REVIEWER_ROLES, load_reviewer_prompt, run_reviewer_squad

__all__ = [
    "REVIEWER_ROLES",
    "ClaudeCliReviewerRunner",
    "Finding",
    "MockReviewerRunner",
    "ReviewInput",
    "ReviewResult",
    "ReviewerRunner",
    "Severity",
    "SquadResult",
    "Verdict",
    "load_reviewer_prompt",
    "parse_review_response",
    "run_reviewer_squad",
]

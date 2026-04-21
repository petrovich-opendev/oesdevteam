"""Abstract ``Gate`` protocol and shared data types.

A gate is a deterministic check that either passes or blocks a feature's
progress. The ``FeatureController`` runs a configured list of gates in
order; the first blocker halts the feature with a ``needs_rework`` state
and the gate's reason is fed back into the next attempt's prompt.

This module intentionally carries no domain knowledge — it defines the
shape of every gate so the controller can treat them uniformly. Concrete
gates live in sibling modules (``code_review_gate.py``, future
``api_contract_gate.py``, etc.).
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..models import QualityGateType

# -----------------------------------------------------------------------------
# Data in / data out
# -----------------------------------------------------------------------------


class GateInput(BaseModel):
    """Everything a gate may need to make a decision.

    Fields are intentionally a superset — individual gates read only the
    subset they care about. Keeping one input type means the controller
    has a single preparation step and gates compose predictably.
    """

    feature_id: str = Field(min_length=1)
    feature_goal: str = Field(min_length=1)

    # Paths of files changed by this feature, relative to repo root.
    files_changed: list[str] = Field(default_factory=list)

    # Unified diff (or equivalent) spanning the changes. Leaves gate
    # implementations free to further slice it if they want per-file
    # checks.
    diff: str = ""

    # Verification commands declared with the feature — some gates
    # (SRE, future API-contract) inspect these to spot missing health
    # checks or missing contract tests.
    verify_commands: list[str] = Field(default_factory=list)

    # Free-text domain brief for the Business Expert reviewer; empty
    # for domain-agnostic features. See src.reviewers.squad for the
    # substitution rules.
    domain_context: str = ""


class GateResult(BaseModel):
    """Structured result of a single gate evaluation.

    ``details`` is gate-specific — for example the code-review gate
    puts a serialised ``SquadResult`` in there for downstream reporting.
    Controllers treat it as opaque; only the dedicated report renderer
    (per-gate) knows how to format it.
    """

    gate_type: QualityGateType

    # True if the feature may proceed past this gate. Exactly one of
    # passed / blocked across the whole gate chain decides whether the
    # feature gets committed — we never have ambiguous "soft pass".
    passed: bool

    # One-line description of the outcome, suitable for logs and NATS
    # events. Longer prose lives in ``details`` and / or in the per-gate
    # report renderer.
    reason: str = Field(min_length=1)

    # Arbitrary gate-specific extension. Keep it JSON-serialisable: the
    # controller may publish it over NATS verbatim.
    details: dict[str, Any] = Field(default_factory=dict)

    # Hint to the controller: should the next attempt be handled as
    # ``needs_rework`` (true, default when passed is False) or as a
    # hard ``stuck`` (set by gates that know no retry will help, e.g.
    # a deployment-level failure). Gates default to retryable.
    #
    # Note: ``allow_retry=True`` does NOT imply infinite retries. The
    # controller is responsible for bounding retries and for discriminating
    # transient failures (reviewer_fault → retry) from persistent ones
    # (same blocker finding twice in a row → escalate to a human via
    # the stuck-feature report path, Step 7).
    allow_retry: bool = True

    @property
    def blocked(self) -> bool:
        """Convenience: the negative of ``passed``."""
        return not self.passed


# -----------------------------------------------------------------------------
# Protocol every gate must satisfy
# -----------------------------------------------------------------------------


class Gate(Protocol):
    """The interface the ``FeatureController`` calls on every gate.

    A gate is async because most gates involve I/O (subprocess, network,
    LLM). Synchronous gates can trivially implement it via
    ``async def check(...)``.
    """

    gate_type: QualityGateType

    async def check(self, gate_input: GateInput) -> GateResult:
        """Evaluate the gate; return ``GateResult`` with pass/fail."""
        ...


# -----------------------------------------------------------------------------
# Report rendering — shared Markdown shape
# -----------------------------------------------------------------------------


def format_gate_report(result: GateResult) -> str:
    """Render a minimal, consistent Markdown summary of a gate outcome.

    Per-gate modules can compose richer reports by prefixing / suffixing
    this header. Keeping the header consistent means a human skimming
    several gate reports in a log instantly finds the ``PASS`` or
    ``BLOCK`` status in the same visual spot.
    """
    tag = "PASS" if result.passed else "BLOCK"
    return f"### Gate `{result.gate_type.value}`: **{tag}**\n\n_Reason:_ {result.reason}\n"

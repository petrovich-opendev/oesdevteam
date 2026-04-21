"""Cost tracker — aggregate USD spend and enforce per-feature budgets.

Step 1 introduced ``--max-budget-usd`` as a **per-call** ceiling, honoured
by Claude Code itself. That is necessary but not sufficient: a feature
that triggers many calls (five reviewers + a worker + a verifier +
retries) can easily spend $10-$20 while every individual call stays
under its own cap. Unless someone aggregates across calls, a runaway
feature burns an unbounded amount of money before the pipeline notices.

This module provides that aggregation.

Usage
-----
::

    tracker = CostTracker()
    tracker.start_feature("FEAT-001", budget=FeatureBudget(max_cost_usd=5.0))

    # each LLM call reports its cost on completion
    tracker.record(feature_id="FEAT-001", role="developer",
                   model="claude-opus-4-7", cost_usd=1.23)

    # the controller checks periodically — raises BudgetExceeded at the
    # first call that crosses the ceiling
    tracker.assert_within_budget("FEAT-001")

Design notes
------------
- ``record`` does NOT raise — it only accumulates. Enforcement is a
  separate, explicit call (``assert_within_budget``). That keeps the
  recording path fast and keeps the controller in charge of the decision.
- The tracker is in-memory. Durable cost history is a separate
  concern (Langfuse export or a cost SQLite file, a future step).
- Thread-safe for the single-process async pipeline because every
  mutation is synchronous inside a single event loop.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

# -----------------------------------------------------------------------------
# Data
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CostEntry:
    """Immutable record of a single LLM call's cost.

    ``model`` is recorded explicitly so post-mortems can attribute a
    budget overrun to an individual model upgrade (for example, "we
    regressed when developer flipped from sonnet back to opus").
    """

    feature_id: str
    role: str
    model: str
    cost_usd: float
    ts: float  # unix seconds


@dataclass(frozen=True)
class FeatureBudget:
    """Per-feature ceiling for aggregate spend.

    ``max_cost_usd`` is the hard cap; ``warn_at_fraction`` controls the
    soft-warning threshold (default 70%). The controller can use the
    warning to proactively wind down retries before hitting the hard cap.
    """

    max_cost_usd: float
    warn_at_fraction: float = 0.7

    def warn_threshold(self) -> float:
        """Absolute USD threshold for the soft-warning signal."""
        return self.max_cost_usd * self.warn_at_fraction


class BudgetExceeded(RuntimeError):
    """Raised by ``assert_within_budget`` when the hard cap is crossed."""

    def __init__(self, feature_id: str, spent: float, cap: float):
        """Record the overrun so the caller can log / publish it."""
        super().__init__(f"Feature {feature_id!r} spent ${spent:.4f}, exceeding cap ${cap:.4f}")
        self.feature_id = feature_id
        self.spent = spent
        self.cap = cap


# -----------------------------------------------------------------------------
# Tracker
# -----------------------------------------------------------------------------


class CostTracker:
    """Aggregate cost across calls within a feature.

    The tracker is deliberately narrow: it answers "how much has this
    feature spent so far" and "is that over the budget". Anything more
    complex (dashboards, historical trends) is a downstream consumer of
    the entries list.
    """

    def __init__(self) -> None:
        """Initialise empty budgets and entry buckets."""
        self._budgets: dict[str, FeatureBudget] = {}
        self._entries: dict[str, list[CostEntry]] = defaultdict(list)

    # --- Lifecycle -----------------------------------------------------------

    def start_feature(
        self,
        feature_id: str,
        *,
        budget: FeatureBudget,
    ) -> None:
        """Register a feature's budget.

        Safe to call more than once for the same id — the latest budget
        wins. That lets the controller bump a cap mid-run (for example,
        to unblock a nearly-finished feature without restarting).
        """
        self._budgets[feature_id] = budget

    # --- Recording -----------------------------------------------------------

    def record(
        self,
        *,
        feature_id: str,
        role: str,
        model: str,
        cost_usd: float,
    ) -> CostEntry:
        """Append a cost entry. Does NOT raise on overrun.

        Separating recording from enforcement keeps the hot path (every
        LLM call) fast and lets the caller decide when to check.
        Negative costs are rejected — they indicate a parsing bug.
        """
        if cost_usd < 0:
            raise ValueError(
                f"Negative cost not allowed: {cost_usd!r}. "
                "A negative cost almost always indicates a parsing bug "
                "upstream; we fail rather than silently re-sign it."
            )

        entry = CostEntry(
            feature_id=feature_id,
            role=role,
            model=model,
            cost_usd=cost_usd,
            ts=time.time(),
        )
        self._entries[feature_id].append(entry)
        return entry

    # --- Accessors -----------------------------------------------------------

    def total_for_feature(self, feature_id: str) -> float:
        """Sum of recorded costs for a feature. Zero if nothing recorded yet."""
        return sum(e.cost_usd for e in self._entries.get(feature_id, []))

    def entries_for_feature(self, feature_id: str) -> list[CostEntry]:
        """Return a copy of the feature's entries (stable order, newest last)."""
        return list(self._entries.get(feature_id, []))

    def budget_for_feature(self, feature_id: str) -> FeatureBudget | None:
        """Lookup the registered budget, or None if the feature is unknown."""
        return self._budgets.get(feature_id)

    # --- Enforcement ---------------------------------------------------------

    def assert_within_budget(self, feature_id: str) -> None:
        """Raise ``BudgetExceeded`` if the feature has crossed its hard cap.

        The controller calls this at safe transition points (between
        agent invocations). If no budget was registered for the feature
        this is a no-op — we do not invent a default cap.
        """
        budget = self._budgets.get(feature_id)
        if budget is None:
            return

        spent = self.total_for_feature(feature_id)
        if spent > budget.max_cost_usd:
            raise BudgetExceeded(feature_id, spent, budget.max_cost_usd)

    def is_warn_threshold_crossed(self, feature_id: str) -> bool:
        """True if the feature has crossed its soft-warning threshold.

        Intended as a cheap polling signal for the controller to decide
        "skip the remaining optional retries, we're running hot".
        Returns False when no budget is registered — absence of budget
        means absence of warning.
        """
        budget = self._budgets.get(feature_id)
        if budget is None:
            return False
        return self.total_for_feature(feature_id) >= budget.warn_threshold()

    # --- Multi-feature summary ----------------------------------------------

    def totals_by_feature(self) -> dict[str, float]:
        """Return {feature_id: total_usd} across all features seen so far."""
        return {fid: self.total_for_feature(fid) for fid in self._entries}

    def grand_total(self) -> float:
        """Sum across every feature — the pipeline-wide spend so far."""
        return sum(self.totals_by_feature().values())

"""Tests for cost tracking and observability plumbing (Step 6)."""

from __future__ import annotations

import pytest

from src.observability import (
    BudgetExceeded,
    CostTracker,
    FeatureBudget,
    NullExporter,
)
from src.observability.langfuse_exporter import LlmSpan

# -----------------------------------------------------------------------------
# CostTracker — recording
# -----------------------------------------------------------------------------


class TestCostTrackerRecording:
    def test_record_accumulates(self):
        t = CostTracker()
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=0.5)
        t.record(feature_id="F1", role="qa", model="sonnet", cost_usd=0.1)
        assert t.total_for_feature("F1") == pytest.approx(0.6)

    def test_record_stores_entry_details(self):
        t = CostTracker()
        entry = t.record(feature_id="F1", role="developer", model="claude-opus-4-7", cost_usd=1.5)
        assert entry.role == "developer"
        assert entry.model == "claude-opus-4-7"
        assert entry.cost_usd == 1.5

    def test_negative_cost_rejected(self):
        t = CostTracker()
        with pytest.raises(ValueError, match="[Nn]egative"):
            t.record(feature_id="F1", role="developer", model="opus", cost_usd=-0.01)

    def test_total_zero_for_unknown_feature(self):
        t = CostTracker()
        assert t.total_for_feature("never-recorded") == 0.0


# -----------------------------------------------------------------------------
# CostTracker — budget enforcement
# -----------------------------------------------------------------------------


class TestCostTrackerBudget:
    def test_no_budget_means_no_enforcement(self):
        """Absence of a registered budget must NOT invent a default cap."""
        t = CostTracker()
        # $1000 worth of spend with no budget — must not raise.
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=1000.0)
        t.assert_within_budget("F1")

    def test_within_budget_does_not_raise(self):
        t = CostTracker()
        t.start_feature("F1", budget=FeatureBudget(max_cost_usd=5.0))
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=3.0)
        t.assert_within_budget("F1")

    def test_over_budget_raises_budget_exceeded(self):
        t = CostTracker()
        t.start_feature("F1", budget=FeatureBudget(max_cost_usd=1.0))
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=1.5)
        with pytest.raises(BudgetExceeded) as exc_info:
            t.assert_within_budget("F1")
        # The exception carries structured detail for logging / NATS.
        assert exc_info.value.feature_id == "F1"
        assert exc_info.value.spent == pytest.approx(1.5)
        assert exc_info.value.cap == 1.0

    def test_warn_threshold_signal(self):
        """Soft-warning at 70% of the cap by default."""
        t = CostTracker()
        t.start_feature("F1", budget=FeatureBudget(max_cost_usd=10.0))
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=5.0)
        assert not t.is_warn_threshold_crossed("F1")
        t.record(feature_id="F1", role="senior_backend", model="opus", cost_usd=3.0)
        assert t.is_warn_threshold_crossed("F1"), (
            "8.0 / 10.0 = 80% — should have crossed the 70% warn threshold"
        )

    def test_start_feature_can_be_called_twice_latest_wins(self):
        t = CostTracker()
        t.start_feature("F1", budget=FeatureBudget(max_cost_usd=1.0))
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=0.9)
        # Bump the cap mid-feature — simulates a controller granting
        # headroom to a nearly-finished feature.
        t.start_feature("F1", budget=FeatureBudget(max_cost_usd=5.0))
        t.record(feature_id="F1", role="qa", model="sonnet", cost_usd=0.5)
        t.assert_within_budget("F1")  # would raise under the old cap


# -----------------------------------------------------------------------------
# CostTracker — pipeline-wide rollups
# -----------------------------------------------------------------------------


class TestCostTrackerRollups:
    def test_totals_by_feature(self):
        t = CostTracker()
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=0.3)
        t.record(feature_id="F2", role="developer", model="opus", cost_usd=0.7)
        totals = t.totals_by_feature()
        assert totals == {"F1": pytest.approx(0.3), "F2": pytest.approx(0.7)}

    def test_grand_total(self):
        t = CostTracker()
        t.record(feature_id="F1", role="developer", model="opus", cost_usd=0.25)
        t.record(feature_id="F2", role="qa", model="sonnet", cost_usd=0.10)
        assert t.grand_total() == pytest.approx(0.35)


# -----------------------------------------------------------------------------
# LlmSpan — preview truncation
# -----------------------------------------------------------------------------


class TestLlmSpan:
    def test_finalise_truncates_long_output(self):
        huge = "A" * 10_000
        span = LlmSpan(
            feature_id="F1",
            role="developer",
            model="opus",
            started_at=0.0,
        )
        span.finalise(ended_at=1.0, output_preview=huge)
        assert len(span.output_preview) < len(huge)
        assert span.output_preview.endswith("[truncated]")

    def test_finalise_preserves_short_output(self):
        short = "ok"
        span = LlmSpan(
            feature_id="F1",
            role="developer",
            model="opus",
            started_at=0.0,
        )
        span.finalise(ended_at=1.0, output_preview=short)
        assert span.output_preview == "ok"

    def test_extra_fields_land_in_extra_dict(self):
        span = LlmSpan(
            feature_id="F1",
            role="developer",
            model="opus",
            started_at=0.0,
        )
        span.finalise(
            ended_at=1.0,
            output_preview="x",
            tokens_in=100,
            tokens_out=50,
            custom_metric=42,
        )
        assert span.tokens_in == 100
        assert span.tokens_out == 50
        assert span.extra["custom_metric"] == 42


# -----------------------------------------------------------------------------
# NullExporter
# -----------------------------------------------------------------------------


class TestNullExporter:
    def test_export_counts_without_storing(self):
        exporter = NullExporter()
        span = LlmSpan(
            feature_id="F1",
            role="developer",
            model="opus",
            started_at=0.0,
            ended_at=1.0,
        )
        exporter.export(span)
        exporter.export(span)
        assert exporter.span_count == 2

    def test_close_is_noop(self):
        exporter = NullExporter()
        exporter.close()  # must not raise
        assert exporter.span_count == 0


# -----------------------------------------------------------------------------
# LangfuseExporter — SDK-missing guard
# -----------------------------------------------------------------------------


class TestLangfuseExporterFallback:
    def test_construction_fails_loudly_without_sdk(self, monkeypatch):
        """If `langfuse` is not installed, construction raises ImportError."""
        import sys

        # Force the lazy import to fail even if the SDK happens to be
        # installed on the test box.
        monkeypatch.setitem(sys.modules, "langfuse", None)

        from src.observability.langfuse_exporter import LangfuseExporter

        with pytest.raises(ImportError, match="langfuse"):
            LangfuseExporter()


# -----------------------------------------------------------------------------
# Business goal alignment
# -----------------------------------------------------------------------------


class TestBusinessGoalAlignment:
    """Cost tracking keeps "autonomous" from meaning "unbounded".

    An autonomous pipeline without aggregate cost enforcement is a
    runaway-spend vector: five reviewers + worker + retries each stay
    under their per-call caps yet together blow a $50 budget. The
    tracker makes that impossible to do silently.
    """

    def test_aggregate_cap_catches_many_small_calls(self):
        t = CostTracker()
        t.start_feature("F1", budget=FeatureBudget(max_cost_usd=2.0))
        # Ten calls each under per-call Opus ceilings, but cumulative > cap.
        for _ in range(10):
            t.record(feature_id="F1", role="reviewer", model="opus", cost_usd=0.25)
        with pytest.raises(BudgetExceeded):
            t.assert_within_budget("F1")

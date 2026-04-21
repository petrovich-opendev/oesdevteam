"""Tests for controller-side utilities (Steps 7 + 8)."""

from __future__ import annotations

import time

from src.controller import (
    AttemptRecord,
    FeatureEscalation,
    SuccessPattern,
    append_success_pattern,
    extract_success_pattern,
    generate_escalation_report,
    load_memory_blob,
    should_escalate,
)

# =============================================================================
# Step 7 — stuck escalation
# =============================================================================


class TestShouldEscalate:
    def test_below_threshold_does_not_escalate(self):
        assert should_escalate(0) is False
        assert should_escalate(2) is False

    def test_at_threshold_escalates(self):
        assert should_escalate(3) is True

    def test_custom_threshold(self):
        assert should_escalate(5, max_attempts=6) is False
        assert should_escalate(6, max_attempts=6) is True


def _mk_escalation(
    *,
    feature_id: str = "FEAT-001",
    attempts: tuple[AttemptRecord, ...] = (),
    goal: str = "Add /login endpoint",
    cost: float = 0.0,
    files: tuple[str, ...] = (),
) -> FeatureEscalation:
    return FeatureEscalation(
        feature_id=feature_id,
        goal=goal,
        attempts=attempts,
        files_touched=files,
        cost_usd=cost,
    )


def _attempt(
    index: int,
    gate: str,
    reason: str,
    blockers: tuple[str, ...] = (),
) -> AttemptRecord:
    return AttemptRecord(
        attempt_index=index,
        gate=gate,
        reason=reason,
        blockers=blockers,
        elapsed_seconds=30.0,
    )


class TestGenerateEscalationReport:
    def test_header_has_feature_id_and_goal(self):
        esc = _mk_escalation()
        report = generate_escalation_report(esc)
        assert "FEAT-001" in report
        assert "Add /login endpoint" in report

    def test_same_gate_each_time_points_at_gate_config(self):
        esc = _mk_escalation(
            attempts=(
                _attempt(1, "code_review", "Senior review blocked: SQL injection"),
                _attempt(2, "code_review", "Senior review blocked: SQL injection"),
                _attempt(3, "code_review", "Senior review blocked: SQL injection"),
            ),
        )
        report = generate_escalation_report(esc)
        assert "Every attempt was rejected by the same gate" in report
        assert "`code_review`" in report

    def test_repeated_blocker_summary_gets_flagged(self):
        esc = _mk_escalation(
            attempts=(
                _attempt(1, "code_review", "r", blockers=("path traversal",)),
                _attempt(2, "code_review", "r", blockers=("path traversal",)),
                _attempt(3, "code_review", "r", blockers=("path traversal",)),
            ),
        )
        report = generate_escalation_report(esc)
        # Pattern analysis highlights the repeat
        assert "path traversal" in report
        assert "surfaced 3 times" in report

    def test_different_gates_each_time_points_at_spec(self):
        esc = _mk_escalation(
            attempts=(
                _attempt(1, "code_review", "r1"),
                _attempt(2, "api_contract", "r2"),
                _attempt(3, "sre_review", "r3"),
            ),
        )
        report = generate_escalation_report(esc)
        assert "under-specified goal" in report

    def test_cost_hint_when_expensive(self):
        esc = _mk_escalation(
            attempts=(_attempt(1, "code_review", "r"),),
            cost=3.5,
        )
        report = generate_escalation_report(esc)
        assert "Cost so far" in report
        assert "$3.50" in report

    def test_no_cost_hint_when_cheap(self):
        esc = _mk_escalation(
            attempts=(_attempt(1, "code_review", "r"),),
            cost=0.5,
        )
        report = generate_escalation_report(esc)
        assert "Cost so far" not in report

    def test_empty_attempts_handled_gracefully(self):
        esc = _mk_escalation()
        report = generate_escalation_report(esc)
        assert "No attempts recorded" in report
        assert "Restart the feature" in report

    def test_report_is_emoji_free(self):
        esc = _mk_escalation(
            attempts=(_attempt(1, "code_review", "r", blockers=("bug",)),),
        )
        report = generate_escalation_report(esc)
        for forbidden in ("✅", "❌", "🚧"):
            assert forbidden not in report

    def test_files_touched_listed(self):
        esc = _mk_escalation(
            attempts=(_attempt(1, "code_review", "r"),),
            files=("src/a.py", "src/b.py"),
        )
        report = generate_escalation_report(esc)
        assert "`src/a.py`" in report
        assert "`src/b.py`" in report


# =============================================================================
# Step 8 — success patterns
# =============================================================================


class TestExtractSuccessPattern:
    def test_reviewer_notes_become_summary_and_evidence(self):
        pattern = extract_success_pattern(
            feature_id="FEAT-2",
            feature_goal="Add health check",
            positive_notes=[
                "Clean pydantic boundary",
                "Good test coverage",
                "Idempotent DB migration",
            ],
            files_touched=["src/health.py"],
        )
        assert pattern.feature_id == "FEAT-2"
        assert pattern.summary == "Clean pydantic boundary"
        assert "Good test coverage" in pattern.evidence
        assert "Idempotent DB migration" in pattern.evidence

    def test_empty_notes_record_completion_as_pattern(self):
        pattern = extract_success_pattern(
            feature_id="FEAT-3",
            feature_goal="Bump dependency X",
            positive_notes=[],
            files_touched=["pyproject.toml"],
        )
        assert "Bump dependency X" in pattern.summary
        # Empty notes still record the touched file for a human to follow up.
        assert any("pyproject.toml" in ev for ev in pattern.evidence)

    def test_tags_carried_through(self):
        pattern = extract_success_pattern(
            feature_id="FEAT-4",
            feature_goal="Refactor auth",
            positive_notes=["Great async handling"],
            tags=["security", "async"],
        )
        assert "security" in pattern.tags
        assert "async" in pattern.tags

    def test_evidence_capped_to_five_items(self):
        pattern = extract_success_pattern(
            feature_id="FEAT-5",
            feature_goal="x",
            positive_notes=[f"note {i}" for i in range(20)],
        )
        # summary is notes[0], evidence is slice [1:6]
        assert len(pattern.evidence) == 5


class TestAppendSuccessPattern:
    def test_first_write_creates_file_with_header(self, tmp_path):
        path = tmp_path / "success_patterns.md"
        pattern = SuccessPattern(
            feature_id="F1",
            summary="All reviewers approved",
            evidence=("senior_backend: clean asyncio usage",),
        )
        append_success_pattern(pattern, path=path)

        content = path.read_text(encoding="utf-8")
        assert "Success patterns" in content
        assert "F1" in content
        assert "clean asyncio" in content

    def test_second_write_appends_without_rewriting_header(self, tmp_path):
        path = tmp_path / "success_patterns.md"
        p1 = SuccessPattern(feature_id="F1", summary="first")
        p2 = SuccessPattern(feature_id="F2", summary="second")
        append_success_pattern(p1, path=path)
        append_success_pattern(p2, path=path)

        content = path.read_text(encoding="utf-8")
        assert content.count("# Success patterns") == 1
        assert "first" in content
        assert "second" in content

    def test_stanza_separator_between_entries(self, tmp_path):
        path = tmp_path / "sp.md"
        append_success_pattern(SuccessPattern(feature_id="F1", summary="a"), path=path)
        append_success_pattern(SuccessPattern(feature_id="F2", summary="b"), path=path)
        content = path.read_text(encoding="utf-8")
        assert "\n---\n" in content


class TestLoadMemoryBlob:
    def test_missing_files_yield_empty_blob(self, tmp_path):
        blob = load_memory_blob(
            lessons_path=tmp_path / "nope1.md",
            success_patterns_path=tmp_path / "nope2.md",
        )
        assert blob == ""

    def test_lessons_and_patterns_both_present(self, tmp_path):
        lessons = tmp_path / "lessons.md"
        patterns = tmp_path / "patterns.md"
        lessons.write_text("Lesson: never hardcode.\n")
        patterns.write_text("Pattern: clean Pydantic boundaries.\n")

        blob = load_memory_blob(
            lessons_path=lessons,
            success_patterns_path=patterns,
        )
        assert "Past lessons" in blob
        assert "Success patterns" in blob
        assert "never hardcode" in blob
        assert "clean Pydantic" in blob

    def test_oversized_file_is_tail_sliced_not_head_sliced(self, tmp_path):
        lessons = tmp_path / "lessons.md"
        # 200 KB of "OLD" followed by a recent marker.
        lessons.write_text("OLD " * 50_000 + "\nRECENT_MARKER\n")

        blob = load_memory_blob(lessons_path=lessons, max_chars=1_000)
        assert "RECENT_MARKER" in blob, "Tail slice must preserve most-recent content"
        assert "earlier content omitted" in blob


# =============================================================================
# Business-goal alignment
# =============================================================================


class TestBusinessGoalAlignment:
    """Steps 7 and 8 make the pipeline self-improving over time.

    Step 7 turns a silent ``stuck`` into an actionable report so a human
    can unblock a feature in minutes rather than hours. Step 8 feeds
    successful patterns back into future prompts — the controller gets
    slightly smarter with every landed feature.
    """

    def test_escalation_report_is_actionable(self):
        """A stuck feature MUST emit a report with concrete next-step hints."""
        esc = _mk_escalation(
            attempts=(
                _attempt(1, "code_review", "r"),
                _attempt(2, "code_review", "r"),
                _attempt(3, "code_review", "r"),
            ),
        )
        report = generate_escalation_report(esc)
        assert "Suggested next steps" in report
        # At least one suggestion bullet.
        assert report.count("- ") >= 1

    def test_memory_blob_combines_both_directions(self, tmp_path):
        """Agents must see both failures AND successes before a run."""
        lessons = tmp_path / "lessons.md"
        patterns = tmp_path / "patterns.md"
        lessons.write_text("Failure mode X: avoid Y.\n")
        patterns.write_text("Success: pattern Z worked.\n")

        blob = load_memory_blob(
            lessons_path=lessons,
            success_patterns_path=patterns,
        )
        assert "Failure mode X" in blob
        assert "pattern Z worked" in blob


# =============================================================================
# Timestamp freshness sanity — SuccessPattern default ts is "now"
# =============================================================================


def test_success_pattern_default_ts_is_recent():
    """Defensive — if someone breaks the default factory, catch it."""
    p = SuccessPattern(feature_id="X", summary="y")
    assert abs(p.ts - time.time()) < 5.0

"""Tests for src/rules — the deterministic pre-review gate.

Each test builds a realistic ``RuleContext`` on a temp directory, runs
the engine, and asserts on the set of findings. No subprocess, no LLM,
fully in-process.

The rule IDs pinned here are the same ones referenced in
``config/rules.yaml`` and in lessons_learned.md (Rules 20-25).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.rules import (
    DEFAULT_RULES,
    RuleContext,
    load_rules_config,
    render_rules_report,
    run_rules_engine,
)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _write(project: Path, rel: str, content: str) -> str:
    """Create a file at ``project/rel`` and return the relative path.

    Makes the intent obvious in test bodies: the returned path is
    exactly what goes into ``files_changed``.
    """
    p = project / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return rel


def _ctx(
    project: Path,
    *,
    feature_id: str = "BI-TEST-001",
    feature_goal: str = "Test feature",
    files_changed: list[str] | None = None,
    diff: str = "",
) -> RuleContext:
    return RuleContext(
        feature_id=feature_id,
        feature_goal=feature_goal,
        files_changed=tuple(files_changed or []),
        project_dir=project,
        diff=diff,
    )


# -----------------------------------------------------------------------------
# R-scaffold-only
# -----------------------------------------------------------------------------


class TestScaffoldOnly:
    def test_only_empty_inits_and_generated_types_blocks(self, tmp_path):
        files = [
            _write(tmp_path, "backend/scripts/__init__.py", ""),
            _write(
                tmp_path,
                "frontend/src/api/types.generated.ts",
                "// auto-generated\nexport type Foo = string;",
            ),
        ]
        ctx = _ctx(tmp_path, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-scaffold-only"})

        assert not result.passed
        assert any(
            f.rule_id == "R-scaffold-only" and f.severity == "blocker" for f in result.findings
        )

    def test_one_real_module_passes(self, tmp_path):
        files = [
            _write(tmp_path, "backend/scripts/__init__.py", ""),
            _write(
                tmp_path,
                "backend/app/api/magic.py",
                "# real implementation, not a stub\n" + "x = 1\n" * 50,
            ),
        ]
        ctx = _ctx(tmp_path, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-scaffold-only"})

        assert result.passed


# -----------------------------------------------------------------------------
# R-goal-file-missing
# -----------------------------------------------------------------------------


class TestGoalFileMissing:
    def test_backticked_path_not_in_diff_blocks(self, tmp_path):
        goal = (
            "Commit `backend/app/api/auth/magic.py` with FOR UPDATE on "
            "auth.otp_code. Also add tests in `backend/tests/test_magic.py`."
        )
        # Diff only adds a TS types file — magic.py and test_magic.py are missing.
        files = [
            _write(tmp_path, "frontend/src/api/types.generated.ts", "x"),
        ]
        ctx = _ctx(tmp_path, feature_goal=goal, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-goal-file-missing"})

        assert not result.passed
        summaries = " ".join(f.summary for f in result.findings)
        assert "magic.py" in summaries

    def test_goal_paths_all_present_passes(self, tmp_path):
        goal = "Create `backend/app/foo.py`."
        files = [_write(tmp_path, "backend/app/foo.py", "def foo(): return 1\n")]
        ctx = _ctx(tmp_path, feature_goal=goal, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-goal-file-missing"})

        assert result.passed


# -----------------------------------------------------------------------------
# R-silent-except
# -----------------------------------------------------------------------------


class TestSilentExcept:
    def test_bare_except_with_silent_return_blocks(self, tmp_path):
        src = textwrap.dedent(
            """
            def load():
                try:
                    fetch()
                except Exception:
                    return frozenset()
            """
        ).strip()
        rel = _write(tmp_path, "backend/app/core/semantic.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-silent-except"})

        assert not result.passed
        f = result.findings[0]
        assert f.rule_id == "R-silent-except"
        assert f.file == rel
        assert f.line is not None

    def test_except_with_log_warning_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            import logging
            log = logging.getLogger()
            def load():
                try:
                    fetch()
                except Exception:
                    log.warning("load failed", exc_info=True)
                    return frozenset()
            """
        ).strip()
        rel = _write(tmp_path, "backend/app/core/semantic.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-silent-except"})

        assert result.passed

    def test_narrow_exception_class_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            class CHQueryError(Exception): pass
            def load():
                try:
                    fetch()
                except CHQueryError:
                    return frozenset()
            """
        ).strip()
        rel = _write(tmp_path, "backend/app/core/semantic.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-silent-except"})

        assert result.passed

    def test_test_files_are_skipped(self, tmp_path):
        # Tests often use bare `except Exception` intentionally.
        src = textwrap.dedent(
            """
            def test_thing():
                try:
                    assert foo()
                except Exception:
                    pass
            """
        ).strip()
        rel = _write(tmp_path, "tests/test_foo.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-silent-except"})

        assert result.passed

    def test_escape_hatch_marker(self, tmp_path):
        src = textwrap.dedent(
            """
            def load():
                try:
                    fetch()
                except Exception:  # rules-ignore: silent-except
                    return {}
            """
        ).strip()
        rel = _write(tmp_path, "backend/app/core/semantic.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-silent-except"})

        assert result.passed


# -----------------------------------------------------------------------------
# R-sql-identifier-fstring
# -----------------------------------------------------------------------------


class TestSqlIdentifierFstring:
    def test_fstring_from_table_blocks(self, tmp_path):
        src = textwrap.dedent(
            """
            def read(table):
                return query(f"SELECT * FROM {table} WHERE id=1")
            """
        ).strip()
        rel = _write(tmp_path, "backend/app/api/sources/tables.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-sql-identifier-fstring"})

        assert not result.passed
        assert result.findings[0].severity == "blocker"

    def test_concatenation_from_variable_blocks(self, tmp_path):
        src = (
            "def read(table):\n"
            '    sql = "SELECT * FROM " + table + " WHERE id=1"\n'
            "    return query(sql)\n"
        )
        rel = _write(tmp_path, "backend/app/api/sources/tables.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-sql-identifier-fstring"})

        assert not result.passed

    def test_parameter_binding_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            def read(user_id):
                return query(
                    "SELECT * FROM trips WHERE user_id = :user_id",
                    {"user_id": user_id},
                )
            """
        ).strip()
        rel = _write(tmp_path, "backend/app/api/sources/tables.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-sql-identifier-fstring"})

        assert result.passed


# -----------------------------------------------------------------------------
# R-metrics-unit-mismatch
# -----------------------------------------------------------------------------


class TestMetricsUnitMismatch:
    def test_mass_description_with_volume_unit_blocks(self, tmp_path):
        yaml_src = textwrap.dedent(
            """
            metrics:
              waste_rock_volume:
                description: "Суммарная масса вскрышных пород"
                unit: "м³"
                sql_template: "SELECT sum(volume_m3) FROM waste"
            """
        ).strip()
        rel = _write(tmp_path, "config/metrics.yaml", yaml_src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-metrics-unit-mismatch"})

        assert not result.passed
        assert result.findings[0].rule_id == "R-metrics-unit-mismatch"

    def test_mass_description_with_tonne_unit_passes(self, tmp_path):
        yaml_src = textwrap.dedent(
            """
            metrics:
              mining_actual_weight:
                description: "Общая масса добытой руды"
                unit: "т"
            """
        ).strip()
        rel = _write(tmp_path, "config/metrics.yaml", yaml_src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-metrics-unit-mismatch"})

        assert result.passed

    def test_rule_skipped_when_metrics_yaml_not_in_diff(self, tmp_path):
        # Even if a broken metrics.yaml exists on disk, the rule only
        # fires when the diff touches it.
        _write(
            tmp_path,
            "config/metrics.yaml",
            'metrics:\n  x:\n    description: "масса"\n    unit: "м³"\n',
        )
        ctx = _ctx(
            tmp_path,
            files_changed=["backend/app/main.py"],  # unrelated file
        )
        result = run_rules_engine(ctx, enabled={"R-metrics-unit-mismatch"})
        assert result.passed


# -----------------------------------------------------------------------------
# R-metrics-sanity-bounds-period
# -----------------------------------------------------------------------------


class TestMetricsSanityBoundsPeriod:
    def test_shift_bounds_on_weekly_metric_majors(self, tmp_path):
        yaml_src = textwrap.dedent(
            """
            metrics:
              downtime_hours:
                description: "Простои техники"
                unit: "ч"
                dimensions:
                  - period: [day, week, month, quarter]
                sanity_bounds:
                  min: 0
                  max: 24
            """
        ).strip()
        rel = _write(tmp_path, "config/metrics.yaml", yaml_src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-metrics-sanity-bounds-period"})

        # Major, not blocker.
        assert result.passed  # no blockers
        assert len(result.findings) == 1
        assert result.findings[0].severity == "major"

    def test_shift_only_metric_passes(self, tmp_path):
        yaml_src = textwrap.dedent(
            """
            metrics:
              downtime_hours_shift:
                description: "Простои за смену"
                unit: "ч"
                dimensions:
                  - period: [shift]
                sanity_bounds:
                  min: 0
                  max: 12
            """
        ).strip()
        rel = _write(tmp_path, "config/metrics.yaml", yaml_src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-metrics-sanity-bounds-period"})
        assert result.passed
        assert not result.findings


# -----------------------------------------------------------------------------
# Engine-level behaviour
# -----------------------------------------------------------------------------


class TestEngineBehaviour:
    def test_rule_crash_does_not_break_engine(self, tmp_path, monkeypatch):
        """A buggy rule must surface as a `rule_fault` MAJOR, not wedge the run."""
        import src.rules.engine as engine_mod

        def _broken_rule(ctx):
            raise RuntimeError("simulated rule bug")

        monkeypatch.setitem(engine_mod.DEFAULT_RULES, "R-fake-broken", _broken_rule)
        ctx = _ctx(tmp_path, files_changed=["x.py"])
        result = run_rules_engine(ctx, enabled={"R-fake-broken"})

        # Broken rule produces a MAJOR, not a BLOCKER — passed stays True.
        assert result.passed
        assert any(f.category == "rule_fault" for f in result.findings)

    def test_disabled_rule_is_skipped(self, tmp_path):
        ctx = _ctx(
            tmp_path,
            files_changed=[_write(tmp_path, "backend/scripts/__init__.py", "")],
        )
        # No rules enabled → empty result, passed=True.
        result = run_rules_engine(ctx, enabled=set())
        assert result.passed
        assert not result.findings
        assert set(result.skipped_rules) == set(DEFAULT_RULES.keys())

    def test_severity_override_downgrades_blocker(self, tmp_path):
        files = [_write(tmp_path, "backend/scripts/__init__.py", "")]
        ctx = _ctx(tmp_path, files_changed=files)
        result = run_rules_engine(
            ctx,
            enabled={"R-scaffold-only"},
            severity_overrides={"R-scaffold-only": "major"},
        )
        # Still a finding, but no longer blocking.
        assert result.passed
        assert result.findings and result.findings[0].severity == "major"

    def test_render_report_block(self, tmp_path):
        files = [_write(tmp_path, "backend/scripts/__init__.py", "")]
        ctx = _ctx(tmp_path, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-scaffold-only"})
        rendered = render_rules_report(result, feature_id="BI-RENDER-001")
        assert "[BLOCK]" in rendered
        assert "R-scaffold-only" in rendered

    def test_load_rules_config_missing_file_enables_all_defaults(self, tmp_path):
        enabled, overrides = load_rules_config(tmp_path / "nonexistent.yaml")
        assert enabled == set(DEFAULT_RULES.keys())
        assert overrides == {}

    def test_load_rules_config_respects_disabled(self, tmp_path):
        cfg = tmp_path / "rules.yaml"
        cfg.write_text(
            textwrap.dedent(
                """
                rules:
                  R-scaffold-only:
                    enabled: false
                  R-silent-except:
                    enabled: true
                    severity: major
                """
            ).strip(),
            encoding="utf-8",
        )
        enabled, overrides = load_rules_config(cfg)
        assert "R-scaffold-only" not in enabled
        assert "R-silent-except" in enabled
        assert overrides.get("R-silent-except") == "major"


# -----------------------------------------------------------------------------
# End-to-end regression — recreates the BI-006 / BI-001e combination.
# -----------------------------------------------------------------------------


class TestBI006Regression:
    """The scenario that Phase A / B identified. Multiple rules fire; the
    engine returns BLOCK without ever contacting the LLM reviewers."""

    def test_combined_violations_all_caught(self, tmp_path):
        metrics_yaml = textwrap.dedent(
            """
            metrics:
              waste_rock_volume:
                description: "Суммарная масса вскрышных пород"
                unit: "м³"
              downtime_hours:
                description: "Простои"
                unit: "ч"
                dimensions:
                  - period: [week, month]
                sanity_bounds:
                  min: 0
                  max: 24
            """
        ).strip()
        semantic_py = textwrap.dedent(
            """
            def fetch_forbidden_variants(client):
                try:
                    return client.query("SELECT variants FROM glossary_terms")
                except Exception:
                    return frozenset()
            """
        ).strip()
        tables_py = textwrap.dedent(
            """
            def read(table):
                return query(f"SELECT * FROM {table}")
            """
        ).strip()
        files = [
            _write(tmp_path, "config/metrics.yaml", metrics_yaml),
            _write(tmp_path, "backend/app/core/semantic.py", semantic_py),
            _write(tmp_path, "backend/app/api/sources/tables.py", tables_py),
        ]
        ctx = _ctx(
            tmp_path,
            feature_goal="Semantic Layer loader + validator",
            files_changed=files,
        )
        result = run_rules_engine(ctx)

        # Blockers from: silent-except, sql-identifier-fstring, metrics-unit-mismatch.
        blocker_rules = {b.rule_id for b in result.blockers()}
        assert "R-silent-except" in blocker_rules
        assert "R-sql-identifier-fstring" in blocker_rules
        assert "R-metrics-unit-mismatch" in blocker_rules
        # And one major for the sanity_bounds / period mismatch.
        major_rules = {m.rule_id for m in result.majors()}
        assert "R-metrics-sanity-bounds-period" in major_rules


# -----------------------------------------------------------------------------
# Go rules: R-go-silent-err
# -----------------------------------------------------------------------------


class TestGoSilentErr:
    def test_empty_error_handler_blocks(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            func load() error {
                _, err := db.Query("SELECT 1")
                if err != nil {
                }
                return nil
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/loader/loader.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert not result.passed
        f = result.findings[0]
        assert f.rule_id == "R-go-silent-err"
        assert f.severity == "blocker"
        assert f.file == rel

    def test_explicit_discard_blocks(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            func load() {
                _, err := fetch()
                if err != nil { _ = err }
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/loader/loader.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert not result.passed
        assert any(f.rule_id == "R-go-silent-err" for f in result.findings)

    def test_bare_return_nil_blocks(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            func load() error {
                _, err := fetch()
                if err != nil {
                    return nil
                }
                return nil
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/loader/loader.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert not result.passed

    def test_wrapped_error_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            import "fmt"

            func load() error {
                _, err := fetch()
                if err != nil {
                    return fmt.Errorf("fetch: %w", err)
                }
                return nil
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/loader/loader.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert result.passed

    def test_logged_handler_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            func load() {
                _, err := fetch()
                if err != nil {
                    slog.Warn("fetch failed", "err", err)
                    return
                }
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/loader/loader.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert result.passed

    def test_nolint_escape_hatch(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            func load() {
                _, err := bestEffort()
                if err != nil { _ = err } // nolint:silent-err — best-effort cache fill
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/loader/loader.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert result.passed

    def test_generated_files_skipped(self, tmp_path):
        src = textwrap.dedent(
            """
            package pb
            // Code generated by protoc. DO NOT EDIT.
            func decode() error {
                if err != nil {
                    return nil
                }
                return nil
            }
            """
        ).strip()
        rel = _write(tmp_path, "api/v1/service.pb.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})

        assert result.passed

    def test_python_only_diff_does_not_fire(self, tmp_path):
        rel = _write(tmp_path, "backend/app/main.py", "x = 1\n" * 30)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-silent-err"})
        assert result.passed
        assert not result.findings


# -----------------------------------------------------------------------------
# Go rules: R-go-sql-concat
# -----------------------------------------------------------------------------


class TestGoSqlConcat:
    def test_sprintf_table_blocks(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            import "fmt"

            func read(table string) {
                q := fmt.Sprintf("SELECT * FROM %s WHERE id=$1", table)
                _ = q
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/store/read.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-sql-concat"})

        assert not result.passed
        assert result.findings[0].severity == "blocker"
        assert result.findings[0].category == "security"

    def test_concatenation_from_var_blocks(self, tmp_path):
        src = (
            'package main\n\n'
            'func read(table string) string {\n'
            '    return "SELECT * FROM " + table + " WHERE id=1"\n'
            '}\n'
        )
        rel = _write(tmp_path, "internal/store/read.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-sql-concat"})

        assert not result.passed

    def test_parameterised_value_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            func read(id int) {
                rows, err := db.Query("SELECT * FROM trips WHERE id=$1", id)
                _, _ = rows, err
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/store/read.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-sql-concat"})

        assert result.passed

    def test_pgx_identifier_passes(self, tmp_path):
        src = textwrap.dedent(
            """
            package main

            import "github.com/jackc/pgx/v5"

            func read(table string) string {
                return "SELECT * FROM " + pgx.Identifier{table}.Sanitize()
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/store/read.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(ctx, enabled={"R-go-sql-concat"})

        # pgx.Identifier ends with `.Sanitize()` — the regex matches
        # `FROM " + pgx`; we still flag it. That's acceptable: the
        # senior_go reviewer can downgrade with a comment, and false
        # positives here lean conservative (security, not style).
        assert any(f.rule_id == "R-go-sql-concat" for f in result.findings)


# -----------------------------------------------------------------------------
# Go rules: scaffold-only includes Go-generated artefacts
# -----------------------------------------------------------------------------


class TestGoScaffoldOnly:
    def test_only_pb_go_and_go_sum_blocks(self, tmp_path):
        files = [
            _write(tmp_path, "api/v1/service.pb.go", "// generated\npackage v1\n"),
            _write(tmp_path, "go.sum", "github.com/foo v1.0.0 h1:abc\n"),
        ]
        ctx = _ctx(tmp_path, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-scaffold-only"})

        assert not result.passed
        assert any(f.rule_id == "R-scaffold-only" for f in result.findings)

    def test_real_go_module_with_pb_passes(self, tmp_path):
        files = [
            _write(tmp_path, "api/v1/service.pb.go", "// generated\npackage v1\n"),
            _write(
                tmp_path,
                "internal/server/server.go",
                "package server\n\n" + "var x = 1\n" * 50,
            ),
        ]
        ctx = _ctx(tmp_path, files_changed=files)
        result = run_rules_engine(ctx, enabled={"R-scaffold-only"})

        assert result.passed


# -----------------------------------------------------------------------------
# Engine-level: language dispatch sanity check
# -----------------------------------------------------------------------------


class TestLanguageDispatch:
    def test_python_diff_runs_no_go_rules(self, tmp_path):
        """A diff with only .py files must not produce any Go findings,
        even when Go rules are enabled — the rules self-filter."""
        src = "def f():\n    return 1\n" * 30
        rel = _write(tmp_path, "backend/app/main.py", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        result = run_rules_engine(
            ctx, enabled={"R-go-silent-err", "R-go-sql-concat"}
        )
        assert result.passed
        assert not result.findings

    def test_go_diff_runs_no_python_rules(self, tmp_path):
        """A diff with only .go files must not trip Python rules."""
        src = textwrap.dedent(
            """
            package main

            func read(table string) string {
                return fmt.Sprintf("SELECT * FROM %s", table)
            }
            """
        ).strip()
        rel = _write(tmp_path, "internal/store/read.go", src)
        ctx = _ctx(tmp_path, files_changed=[rel])
        # Run only the Python SQL rule — it must not fire on Go.
        result = run_rules_engine(
            ctx, enabled={"R-sql-identifier-fstring"}
        )
        assert result.passed
        assert not result.findings


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])

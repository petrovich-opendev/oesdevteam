"""Pre-review rules engine.

Runs deterministic checks against a feature's diff BEFORE invoking the
LLM-driven senior review squad. Catches a handful of high-cost patterns
that we repeatedly saw slip past the LLM reviewers:

- scaffold-only deliverables (BI-001e)
- goal-vs-diff divergence (BI-001d, BI-001e)
- silent `except Exception: return <fallback>` on external-system calls (BI-006)
- SQL identifier interpolation via f-strings (BI-005)
- metrics.yaml description/unit/sql disagreement (BI-006)
- metrics.yaml sanity_bounds incompatible with aggregation period (BI-006)

Running rules first has two benefits:
1. Cost: each failing feature saves ~$5 of LLM reviewer calls when the
   engine catches the defect early.
2. Determinism: rule violations are reproducible; LLM reviewers drift.

Scope-locked non-goals
----------------------
- Type inference, data-flow analysis, cross-file reasoning. Rules are
  single-file, single-pass, token-level or AST-level only.
- Security scanning. Semgrep + Bandit already run in CI; this engine
  focuses on *project conventions* rather than generic vulnerabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RuleFinding:
    """One defect reported by the rules engine.

    Shape mirrors ``src.reviewers.findings.Finding`` so downstream
    aggregators (code review gate, pipeline-log renderer) can treat rule
    findings and LLM findings uniformly.
    """

    rule_id: str
    severity: str  # "blocker" | "major" | "minor"
    file: str
    line: int | None
    category: str
    summary: str
    why: str
    fix: str


@dataclass
class RulesResult:
    """Aggregate verdict from a rules-engine run."""

    passed: bool
    findings: list[RuleFinding] = field(default_factory=list)
    rules_evaluated: list[str] = field(default_factory=list)
    skipped_rules: list[str] = field(default_factory=list)

    def blockers(self) -> list[RuleFinding]:
        """Return only the blocker-severity findings, for quick inspection."""
        return [f for f in self.findings if f.severity == "blocker"]

    def majors(self) -> list[RuleFinding]:
        """Return only the major-severity findings, for quick inspection."""
        return [f for f in self.findings if f.severity == "major"]


@dataclass(frozen=True)
class RuleContext:
    """Input handed to each rule.

    Rules receive the same context; they decide which subset they care
    about (e.g. metrics rules skip when `config/metrics.yaml` is not in
    the diff).
    """

    feature_id: str
    feature_goal: str
    files_changed: tuple[str, ...]
    project_dir: Path
    diff: str

    def abs_path(self, rel: str) -> Path:
        """Resolve ``rel`` against the project root for direct filesystem access."""
        return self.project_dir / rel

    def read_text_safe(self, rel: str) -> str | None:
        """Read a file from the worktree, returning None if unreadable.

        Rules should not raise on missing files — they should skip. A
        worker that forgot to create a file is the rule's finding, not a
        runtime error in the engine itself.
        """
        p = self.abs_path(rel)
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return None


# A rule is any callable ``(RuleContext) -> list[RuleFinding]``. We keep
# the registry flat and explicit; adding a rule means importing it here
# and appending to ``DEFAULT_RULES``.
from .checks import (  # noqa: E402 — avoid forward-declaration hoop
    rule_goal_file_missing,
    rule_metrics_sanity_bounds_period,
    rule_metrics_unit_mismatch,
    rule_scaffold_only,
    rule_silent_except,
    rule_sql_identifier_fstring,
)

DEFAULT_RULES: dict[str, object] = {
    "R-scaffold-only": rule_scaffold_only,
    "R-goal-file-missing": rule_goal_file_missing,
    "R-silent-except": rule_silent_except,
    "R-sql-identifier-fstring": rule_sql_identifier_fstring,
    "R-metrics-unit-mismatch": rule_metrics_unit_mismatch,
    "R-metrics-sanity-bounds-period": rule_metrics_sanity_bounds_period,
}


def run_rules_engine(
    ctx: RuleContext,
    *,
    enabled: set[str] | None = None,
    severity_overrides: dict[str, str] | None = None,
) -> RulesResult:
    """Execute every enabled rule and aggregate findings.

    Args:
        ctx: Feature context (files changed, diff, project dir, goal).
        enabled: Rule IDs to run. If None, runs every rule in
            ``DEFAULT_RULES``. Unknown IDs are ignored with a skip note.
        severity_overrides: Map ``rule_id -> severity`` to downgrade a
            rule from the default severity (e.g. ``R-silent-except`` ->
            ``"major"`` during early project bootstrap). Upgrades are
            also allowed; the value is applied verbatim.

    Returns:
        A ``RulesResult`` listing findings. The result's ``passed`` flag
        is False iff there is at least one blocker-severity finding.
    """
    run_set = set(enabled) if enabled is not None else set(DEFAULT_RULES.keys())
    overrides = severity_overrides or {}

    findings: list[RuleFinding] = []
    evaluated: list[str] = []
    skipped: list[str] = []

    for rule_id, rule in DEFAULT_RULES.items():
        if rule_id not in run_set:
            skipped.append(rule_id)
            continue
        try:
            raw = rule(ctx)
        except Exception as exc:  # noqa: BLE001 — a broken rule must not
            # take down the whole pre-review step; record as an operator
            # signal and move on.
            findings.append(
                RuleFinding(
                    rule_id=rule_id,
                    severity="major",
                    file="<rules-engine>",
                    line=None,
                    category="rule_fault",
                    summary=f"Rule {rule_id} crashed: {type(exc).__name__}",
                    why=(
                        f"Internal error while evaluating this rule: {exc!r}. "
                        "Treating as MAJOR so the operator notices, but not "
                        "BLOCKER — a buggy rule must not wedge the pipeline."
                    ),
                    fix=(
                        f"Inspect src/rules/checks.py:{rule_id.replace('-', '_')} "
                        "and add a regression test in tests_v2/test_rules_engine.py."
                    ),
                )
            )
            evaluated.append(rule_id)
            continue
        # Apply severity overrides so config can loosen / tighten a rule
        # without changing the check's implementation.
        if rule_id in overrides:
            raw = [
                RuleFinding(
                    rule_id=f.rule_id,
                    severity=overrides[rule_id],
                    file=f.file,
                    line=f.line,
                    category=f.category,
                    summary=f.summary,
                    why=f.why,
                    fix=f.fix,
                )
                for f in raw
            ]
        findings.extend(raw)
        evaluated.append(rule_id)

    passed = not any(f.severity == "blocker" for f in findings)
    return RulesResult(
        passed=passed,
        findings=findings,
        rules_evaluated=evaluated,
        skipped_rules=skipped,
    )


def load_rules_config(config_path: Path) -> tuple[set[str], dict[str, str]]:
    """Load ``config/rules.yaml`` into an ``(enabled, overrides)`` pair.

    The file is intentionally small and human-editable::

        rules:
          R-scaffold-only:
            enabled: true
          R-silent-except:
            enabled: true
            severity: blocker
          R-metrics-unit-mismatch:
            enabled: true
            severity: blocker

    Missing file or missing rule entry → the rule runs with its default
    severity. An explicit ``enabled: false`` skips it.
    """
    if not config_path.is_file():
        return set(DEFAULT_RULES.keys()), {}

    import yaml  # local import — rules engine should import cheaply

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set(DEFAULT_RULES.keys()), {}

    rules_cfg = data.get("rules", {}) or {}
    enabled: set[str] = set()
    overrides: dict[str, str] = {}
    for rule_id in DEFAULT_RULES:
        entry = rules_cfg.get(rule_id, {}) or {}
        if entry.get("enabled", True):
            enabled.add(rule_id)
        sev = entry.get("severity")
        if sev in {"blocker", "major", "minor"}:
            overrides[rule_id] = sev
    return enabled, overrides


def render_rules_report(result: RulesResult, *, feature_id: str) -> str:
    """Render a Markdown report mirroring ``render_code_review_report``.

    Lets us drop a rules-engine verdict into ``pipeline-log/`` next to
    the senior-review report without reinventing formatting.
    """
    head = "[PASS]" if result.passed else "[BLOCK]"
    lines: list[str] = [
        f"# Pre-review Rules Engine — {head}",
        "",
        f"Feature: `{feature_id}`",
        f"Rules evaluated: {len(result.rules_evaluated)} (skipped: {len(result.skipped_rules)})",
        "",
    ]
    if result.blockers():
        lines.append("## Blockers")
        lines.append("")
        for f in result.blockers():
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines += [
                f"### `{f.rule_id}` — {f.summary} @ `{loc}`",
                "",
                f"**Why:** {f.why}",
                "",
                f"**Fix:** {f.fix}",
                "",
            ]
    if result.majors():
        lines.append("## Majors")
        lines.append("")
        for f in result.majors():
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines += [
                f"### `{f.rule_id}` — {f.summary} @ `{loc}`",
                "",
                f"**Why:** {f.why}",
                "",
                f"**Fix:** {f.fix}",
                "",
            ]
    if result.passed and not result.findings:
        lines.append("No findings. Proceeding to senior review.")
        lines.append("")
    return "\n".join(lines)

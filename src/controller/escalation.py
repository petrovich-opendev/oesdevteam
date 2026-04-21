"""Stuck-feature escalation — deterministic human-readable report generation.

Why this exists
---------------
v1 pipelines had a nasty failure shape: a feature would hit the retry
cap, get marked ``stuck``, and sit in the state file forever with no
actionable information for the operator. Nobody knew whether to fix it
by editing the spec, by adjusting a reviewer prompt, or by rolling back
a previous commit — because the only trace was the agent's last error
string, dropped into a log.

This module produces a *structured* escalation report any time a
feature crosses the attempt ceiling. The report is plain Markdown
written to ``tasks/backlog/escalation-<feature-id>.md`` so a human can
open it in an editor, reason about the pattern, and pick a fix.

Scope
-----
No LLM here. The report generator is purely deterministic: it takes
what the controller already has on disk (attempt history + findings)
and formats it. A future step may add an optional LLM summariser that
adds a "proposed fixes" section, but the base escalation path must
work without it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# -----------------------------------------------------------------------------
# Attempt records
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRecord:
    """One retry of a feature, with the reason it failed.

    ``gate`` identifies which quality gate rejected the attempt
    (``code_review``, ``api_contract``, ``sre_review``, ``verify``, ...).
    ``blockers`` is the list of summary strings from blocker/major
    findings, surfaced for the human to see without digging through
    the detail JSON.
    """

    attempt_index: int  # 1-based
    gate: str  # e.g. "code_review", "verify"
    reason: str  # gate's short reason line
    blockers: tuple[str, ...] = ()  # representative finding summaries
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class FeatureEscalation:
    """Everything the report generator needs about one stuck feature."""

    feature_id: str
    goal: str
    attempts: tuple[AttemptRecord, ...]
    files_touched: tuple[str, ...] = ()
    cost_usd: float = 0.0
    extra: dict[str, str] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Escalation decision
# -----------------------------------------------------------------------------

# Three attempts is the historical default in v1. Matches the
# ``TaskBudget.max_attempts`` in src/models.py — keep them aligned.
DEFAULT_MAX_ATTEMPTS = 3


def should_escalate(attempts: int, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    """True if a feature has used up its retry budget and must be escalated.

    Kept as a one-liner so it reads naturally at the call site. A dedicated
    helper (rather than inline ``attempts >= max_attempts``) lets the
    policy live in one place if, for example, we later differentiate
    retry budgets by gate type.
    """
    return attempts >= max_attempts


# -----------------------------------------------------------------------------
# Report generation
# -----------------------------------------------------------------------------


def generate_escalation_report(escalation: FeatureEscalation) -> str:
    """Return a Markdown report for a stuck feature.

    The report has three sections:

    1. **Header** — feature id, goal, cost, files touched.
    2. **Attempt history** — one bullet per attempt with the rejecting
       gate, reason, and top blocker summaries.
    3. **Pattern analysis** — frequency table of gates and blocker
       summaries so the operator can spot "every attempt fails at the
       same gate for the same reason" vs "each attempt fails in a
       different way".
    4. **Suggested next steps** — rule-based heuristics
       (no LLM): if a single gate keeps rejecting, point the operator
       at the gate's config or prompt; if attempts fail in different
       gates, suggest the feature scope may be ambiguous.

    The output is deliberately plain Markdown (no emoji) so it renders
    cleanly in a terminal ``mdcat``, a GitHub issue, and a Slack paste.
    """
    lines: list[str] = []
    lines += [f"# Feature escalation — `{escalation.feature_id}`", ""]
    lines += [f"**Goal:** {escalation.goal}", ""]
    lines += [f"- Attempts: {len(escalation.attempts)}"]
    if escalation.cost_usd:
        lines.append(f"- Spent: ${escalation.cost_usd:.4f}")
    if escalation.files_touched:
        preview = ", ".join(f"`{f}`" for f in escalation.files_touched[:10])
        trailer = "…" if len(escalation.files_touched) > 10 else ""
        lines.append(f"- Files touched: {preview}{trailer}")
    lines.append("")

    lines += _render_attempts_section(escalation)
    lines += _render_pattern_section(escalation)
    lines += _render_suggestions_section(escalation)

    return "\n".join(lines).rstrip() + "\n"


def _render_attempts_section(escalation: FeatureEscalation) -> list[str]:
    """Render the per-attempt timeline."""
    out: list[str] = ["## Attempt history", ""]
    if not escalation.attempts:
        out += ["_No attempts recorded — this escalation was triggered without retry data._", ""]
        return out

    for attempt in escalation.attempts:
        header = (
            f"### Attempt {attempt.attempt_index} — gate `{attempt.gate}` "
            f"({attempt.elapsed_seconds:.1f}s)"
        )
        out.append(header)
        out += ["", f"_{attempt.reason}_"]
        if attempt.blockers:
            out.append("")
            out += [f"- {b}" for b in attempt.blockers]
        out.append("")
    return out


def _render_pattern_section(escalation: FeatureEscalation) -> list[str]:
    """Render the frequency analysis across attempts."""
    out: list[str] = ["## Pattern analysis", ""]

    if not escalation.attempts:
        out += ["_Insufficient data for a pattern analysis._", ""]
        return out

    # Which gate rejected how often.
    gate_counts = Counter(a.gate for a in escalation.attempts)
    out += ["**Gate rejection counts**", ""]
    for gate, count in gate_counts.most_common():
        out.append(f"- `{gate}`: {count}")
    out.append("")

    # Which blocker summaries repeat. Repetition signals a single
    # persistent issue; uniqueness suggests scope ambiguity.
    all_blockers: list[str] = [b for a in escalation.attempts for b in a.blockers]
    if all_blockers:
        summary_counts = Counter(all_blockers)
        out += ["**Top blocker summaries (across attempts)**", ""]
        for summary, count in summary_counts.most_common(5):
            marker = "⟳" if count > 1 else " "
            out.append(f"- {marker} ({count}×) {summary}")
        out.append("")

    return out


def _render_suggestions_section(escalation: FeatureEscalation) -> list[str]:
    """Heuristic rule-based next-steps suggestions."""
    out: list[str] = ["## Suggested next steps (heuristic)", ""]

    if not escalation.attempts:
        out.append("- Restart the feature — no attempt data to analyse.")
        out.append("")
        return out

    gate_counts = Counter(a.gate for a in escalation.attempts)
    most_common_gate, top_count = gate_counts.most_common(1)[0]
    total_attempts = len(escalation.attempts)

    # Pattern 1: same gate keeps rejecting.
    if top_count == total_attempts:
        out.append(
            f"- Every attempt was rejected by the same gate (`{most_common_gate}`). "
            "Check the gate's configuration or the reviewer prompt that drives "
            "it — the feature spec may be clear but the acceptance rule may "
            "be over-strict."
        )
    elif top_count >= 2:
        out.append(
            f"- `{most_common_gate}` rejected {top_count} of {total_attempts} attempts. "
            "Likely a single unresolved issue; inspect the repeating blocker "
            "summaries above."
        )
    else:
        out.append(
            "- Each attempt failed at a different gate. This usually signals "
            "an under-specified goal or a missing prerequisite (infrastructure, "
            "dependency, migration). Revisit the feature spec before retrying."
        )

    # Pattern 2: repeating blocker summary.
    all_blockers: list[str] = [b for a in escalation.attempts for b in a.blockers]
    if all_blockers:
        top_summary, top_count_b = Counter(all_blockers).most_common(1)[0]
        if top_count_b >= 2:
            out.append(
                f"- `{top_summary}` surfaced {top_count_b} times — treat as the "
                "primary issue. Fixing it may unblock the feature without any "
                "other change."
            )

    # Pattern 3: budget signal.
    if escalation.cost_usd >= 2.0:
        out.append(
            f"- Cost so far (${escalation.cost_usd:.2f}) is non-trivial. "
            "Consider whether another retry is worth it, or whether the feature "
            "should be split into smaller pieces."
        )

    out.append("")
    return out

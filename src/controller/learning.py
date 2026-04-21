"""Positive + negative learning loop.

v1's controller accumulated ``lessons_learned.md`` only from failures.
That was half the job: the controller became good at not repeating past
mistakes, but it had no way to reinforce practices that *worked*. Over
time this biased it toward timid behaviour — anything unfamiliar felt
risky even if nothing was actually wrong.

Step 8 closes that loop. After every successful feature the controller
extracts what worked and appends it to
``controller/success_patterns.md``. Before every new run it reads BOTH
files (lessons + success patterns) and injects them into the agent
prompts — so agents learn from both directions.

Scope
-----
- ``SuccessPattern`` — the append-only record.
- ``extract_success_pattern`` — pure function building a pattern from
  what the controller already has (reviewers' ``positive_notes`` +
  feature metadata). No LLM; the reviewers already summarised things.
- ``append_success_pattern`` — idempotent file write (appends one
  stanza; never rewrites existing content).
- ``load_memory_blob`` — concatenates lessons + patterns into a single
  string the controller can embed in an agent system prompt.

Scope-locked non-goals
----------------------
Quarterly dedup / rule promotion (``config/rules.yaml``) is a separate,
larger piece of work. This step lays the plumbing: gets data on disk in
a parseable shape so a future dedup job can read it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

# -----------------------------------------------------------------------------
# Data
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SuccessPattern:
    """One pattern harvested from a successful feature.

    Fields:
      - ``feature_id``: the feature that produced the pattern.
      - ``summary``: one-line description of what worked.
      - ``evidence``: short bullet list of concrete details
        (reviewer praise quotes, file paths, metric deltas).
      - ``ts``: unix seconds, used to order stanzas in the file.
      - ``tags``: free-form tags so a future dedup / promotion job can
        group similar stanzas.
    """

    feature_id: str
    summary: str
    evidence: tuple[str, ...] = ()
    ts: float = field(default_factory=time.time)
    tags: tuple[str, ...] = ()


# -----------------------------------------------------------------------------
# Extraction
# -----------------------------------------------------------------------------


def extract_success_pattern(
    *,
    feature_id: str,
    feature_goal: str,
    positive_notes: list[str],
    files_touched: list[str] | None = None,
    tags: list[str] | None = None,
) -> SuccessPattern:
    """Build a ``SuccessPattern`` from a passed feature's reviewer output.

    ``positive_notes`` is the union of every reviewer's
    ``positive_notes`` in the gate's ``SquadResult``. The function is
    tolerant of empty notes — if reviewers had nothing nice to say, we
    record the feature's completion as the pattern (a "working code"
    pattern still carries information).

    Kept as a pure function so tests can exercise it without IO.
    """
    if positive_notes:
        summary = positive_notes[0].strip()
        evidence = tuple(n.strip() for n in positive_notes[1:6] if n.strip())
    else:
        # Even a silent success is a data point — record the goal so a
        # future dedup job can spot "we've solved this class of feature
        # N times, promote the approach to a rule".
        summary = f"Completed without reviewer objections: {feature_goal}"
        evidence = ()

    tags_tuple: tuple[str, ...] = tuple(tags or ())
    # Record one touched path as evidence if the reviewers said nothing —
    # gives a human a starting point for inspection.
    if files_touched and not evidence:
        preview = ", ".join(files_touched[:3])
        evidence = (f"Files touched: {preview}",)

    return SuccessPattern(
        feature_id=feature_id,
        summary=summary,
        evidence=evidence,
        tags=tags_tuple,
    )


# -----------------------------------------------------------------------------
# File I/O
# -----------------------------------------------------------------------------

_FILE_HEADER = (
    "# Success patterns\n\n"
    "Append-only log of what worked. Written by the controller after\n"
    "each successful feature. Read alongside `lessons_learned.md` before\n"
    "every pipeline run.\n\n"
    "Format is stable (one `##` stanza per pattern) so the dedup /\n"
    "rule-promotion job in a later step can parse it mechanically.\n"
)


def _format_stanza(pattern: SuccessPattern) -> str:
    """Render a ``SuccessPattern`` as a single Markdown stanza.

    Stable field order matters: the dedup job will do simple string
    comparisons on summary lines. Keep ``## Summary`` as the first
    subheader.
    """
    ts_iso = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(pattern.ts))
    lines = [
        f"## `{pattern.feature_id}` — {ts_iso}",
        "",
        pattern.summary,
        "",
    ]
    if pattern.tags:
        lines.append(f"_Tags:_ {', '.join(pattern.tags)}")
        lines.append("")
    if pattern.evidence:
        lines.append("**Evidence:**")
        lines.append("")
        lines += [f"- {e}" for e in pattern.evidence]
        lines.append("")
    return "\n".join(lines)


def append_success_pattern(pattern: SuccessPattern, *, path: Path) -> None:
    """Append one pattern stanza to ``path``, creating the file if absent.

    Idempotent for the file header (written only on first use) but NOT
    for the stanza — two calls with the same pattern append twice. If
    the caller needs dedup it can read back with ``read_patterns``
    (future) and skip duplicates; this function's job is just to
    persist what it is given.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new_file:
            f.write(_FILE_HEADER)
        f.write(_format_stanza(pattern))
        # Separator between stanzas so the file stays readable.
        f.write("\n---\n\n")


# -----------------------------------------------------------------------------
# Memory blob for agent prompts
# -----------------------------------------------------------------------------


def load_memory_blob(
    *,
    lessons_path: Path | None = None,
    success_patterns_path: Path | None = None,
    max_chars: int = 20_000,
) -> str:
    """Concatenate lessons + success patterns into an agent-prompt blob.

    The controller embeds the result into every agent's system prompt
    as a "what we have learned" preamble. The cap (default 20 KB)
    prevents a bloated memory file from eating the agent's context
    window; the most recent content wins (we slice the TAIL of each
    file, not the head, because recency is the best proxy for
    relevance in an append-only log).

    Missing files are tolerated — the blob simply omits that section.
    """
    sections: list[str] = []

    if lessons_path and lessons_path.exists():
        text = lessons_path.read_text(encoding="utf-8")
        sections.append("## Past lessons (from failures)\n\n" + _tail(text, max_chars // 2))

    if success_patterns_path and success_patterns_path.exists():
        text = success_patterns_path.read_text(encoding="utf-8")
        sections.append("## Success patterns (from wins)\n\n" + _tail(text, max_chars // 2))

    return "\n\n".join(sections).strip()


def _tail(s: str, max_chars: int) -> str:
    """Return the last ``max_chars`` characters of ``s``, with a marker if truncated."""
    if len(s) <= max_chars:
        return s
    return "…[earlier content omitted]…\n\n" + s[-max_chars:]

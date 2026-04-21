"""Optional Langfuse exporter for LLM call traces.

Langfuse is the SaaS observability backend declared in this project's
CLAUDE.md. It turns raw LLM calls into structured traces with latency,
cost, prompt, and output visible in a dashboard. But installing the
``langfuse`` SDK is optional — the OESDevTeam pipeline must remain
usable on machines that cannot (or choose not to) talk to Langfuse.

We therefore ship two exporters behind a common ``TraceExporter``
interface:

- ``LangfuseExporter`` — real; imports ``langfuse`` lazily. If the
  SDK is missing, the exporter raises at construction so the caller
  fails loudly rather than silently dropping spans.
- ``NullExporter`` — no-op; every method accepts and returns without
  side effects. The default for test and for deploys without Langfuse.

The controller decides which exporter to instantiate (typically based
on env vars ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` being
present).

Span shape
----------
A minimal LLM span captures:
  - feature_id (grouping key)
  - role (e.g. ``developer``, ``senior_backend``)
  - model (e.g. ``claude-opus-4-7``)
  - input_preview (first N chars of the task prompt — NOT the full
    prompt, to keep per-call payloads bounded in the UI)
  - output_preview (first N chars of the response)
  - cost_usd, tokens (when available)
  - started_at / ended_at

We deliberately do NOT ship the full prompt into Langfuse: prompts
may contain untrusted content and full capture is a separate, opt-in
concern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

# Size cap for content previewed to Langfuse. 4 KB is enough to spot
# the start of a prompt or the shape of an output without blowing the
# UI's payload budget.
_PREVIEW_MAX_CHARS = 4_000


# -----------------------------------------------------------------------------
# Span data
# -----------------------------------------------------------------------------


@dataclass
class LlmSpan:
    """Structured record of one LLM call for export.

    Timestamps are unix-epoch floats to keep the dataclass cheap; the
    exporter formats them per backend convention.
    """

    feature_id: str
    role: str
    model: str
    started_at: float
    ended_at: float = 0.0
    input_preview: str = ""
    output_preview: str = ""
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def finalise(self, *, ended_at: float, output_preview: str = "", **updates: Any) -> None:
        """Fill in the post-call fields (end timestamp, output, etc.).

        Kept as a method rather than requiring callers to mutate fields
        directly so a future schema change (extra validation, truncation
        rules) has one place to land.
        """
        self.ended_at = ended_at
        self.output_preview = _truncate(output_preview)
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.extra[key] = value


def _truncate(s: str) -> str:
    """Cap ``s`` to ``_PREVIEW_MAX_CHARS`` characters with a trailing ellipsis."""
    if len(s) <= _PREVIEW_MAX_CHARS:
        return s
    return s[:_PREVIEW_MAX_CHARS] + "…[truncated]"


# -----------------------------------------------------------------------------
# Exporter interface
# -----------------------------------------------------------------------------


class TraceExporter(Protocol):
    """Minimal interface every exporter implements."""

    def export(self, span: LlmSpan) -> None:
        """Ship one completed LLM span to the backend.

        Implementations MUST NOT block pipeline progress. A slow or
        failing backend should be handled internally (queue, drop,
        log) — the pipeline does not wait on observability.
        """
        ...

    def close(self) -> None:
        """Flush any pending spans and release resources."""
        ...


# -----------------------------------------------------------------------------
# No-op exporter — default for test and offline deploys
# -----------------------------------------------------------------------------


class NullExporter:
    """Exporter that records nothing.

    Used when Langfuse is not configured. Having a real object (rather
    than ``None``) lets the controller call ``exporter.export(span)``
    unconditionally.
    """

    def __init__(self) -> None:
        """Initialise the null sink (spans are counted but not stored)."""
        self.span_count = 0  # so tests can assert that something was exported

    def export(self, span: LlmSpan) -> None:
        """Count the span without persisting it."""
        self.span_count += 1

    def close(self) -> None:
        """No-op close — nothing to flush."""
        return None


# -----------------------------------------------------------------------------
# Real Langfuse exporter — imports the SDK lazily
# -----------------------------------------------------------------------------


class LangfuseExporter:
    """Ship spans to a Langfuse project.

    Construction requires at minimum ``LANGFUSE_PUBLIC_KEY`` and
    ``LANGFUSE_SECRET_KEY`` env vars (or explicit kwargs). If the
    ``langfuse`` SDK is not installed, construction raises
    ``ImportError`` — callers should prefer ``NullExporter`` in that
    case rather than silencing the error.
    """

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ):
        """Instantiate a Langfuse client using env vars by default."""
        try:
            import langfuse  # type: ignore[import-not-found]
        except ImportError as e:
            # Surface a clear, actionable error. Do NOT fall back to the
            # NullExporter silently — the caller who asked for Langfuse
            # wants to know it is not wired up.
            raise ImportError(
                "The `langfuse` package is required to use LangfuseExporter. "
                "Install it with `pip install langfuse`, or use NullExporter "
                "if observability export is not desired."
            ) from e

        self._client = langfuse.Langfuse(
            public_key=public_key or os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=secret_key or os.environ.get("LANGFUSE_SECRET_KEY"),
            host=host or os.environ.get("LANGFUSE_HOST"),
        )

    def export(self, span: LlmSpan) -> None:
        """Publish the span to Langfuse.

        We build a Langfuse trace per ``feature_id`` and a span per LLM
        call. This keeps multi-step features grouped in the dashboard.
        """
        self._client.trace(
            id=span.feature_id,
            name=span.feature_id,
        ).generation(
            name=f"{span.role}",
            model=span.model,
            input=span.input_preview,
            output=span.output_preview,
            metadata={"cost_usd": span.cost_usd, **span.extra},
            usage={
                "input": span.tokens_in,
                "output": span.tokens_out,
                "unit": "TOKENS",
            },
            start_time=span.started_at,
            end_time=span.ended_at,
        )

    def close(self) -> None:
        """Flush pending spans to Langfuse's ingest endpoint."""
        try:
            self._client.flush()
        except Exception:
            # The pipeline must not fail because observability flush
            # failed. We log via print to stderr because this runs at
            # shutdown when the logging chain may already be torn down.
            import sys

            print("Langfuse flush failed; spans may be lost.", file=sys.stderr)

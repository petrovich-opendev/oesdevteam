"""Observability utilities — cost tracking and LLM trace export.

Two concerns live here:

- ``cost_tracker`` — aggregate USD spend per feature / per pipeline,
  enforce ``TaskBudget.max_cost_usd`` as a hard ceiling.
- ``langfuse_exporter`` — optional export of LLM call spans to a
  Langfuse backend. The exporter is no-op when ``langfuse`` is not
  installed, so the pipeline stays usable without the SDK.

Keeping both under one package lets a future ``ObservabilitySuite``
class wire them together once the controller (Step 7+) is ready to
consume.
"""

from __future__ import annotations

from .cost_tracker import (
    BudgetExceeded,
    CostEntry,
    CostTracker,
    FeatureBudget,
)
from .langfuse_exporter import LangfuseExporter, NullExporter, TraceExporter

__all__ = [
    "BudgetExceeded",
    "CostEntry",
    "CostTracker",
    "FeatureBudget",
    "LangfuseExporter",
    "NullExporter",
    "TraceExporter",
]

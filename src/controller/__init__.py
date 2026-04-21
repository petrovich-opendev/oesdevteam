"""Controller-side utilities — escalation reports and learning loop.

This package hosts logic that the future ``FeatureController`` port from
v1 will call between attempts: generating a human-readable escalation
report when a feature refuses to pass after ``max_attempts`` retries,
and extracting success patterns into ``controller/success_patterns.md``
when a feature finally lands.

The controller itself is not in this step — we ship the building blocks
so the port can compose them without reinventing the surface.
"""

from __future__ import annotations

from .domain_context import build_domain_context
from .escalation import (
    AttemptRecord,
    FeatureEscalation,
    generate_escalation_report,
    should_escalate,
)
from .learning import (
    SuccessPattern,
    append_success_pattern,
    extract_success_pattern,
    load_memory_blob,
)

__all__ = [
    "AttemptRecord",
    "FeatureEscalation",
    "SuccessPattern",
    "append_success_pattern",
    "build_domain_context",
    "extract_success_pattern",
    "generate_escalation_report",
    "load_memory_blob",
    "should_escalate",
]

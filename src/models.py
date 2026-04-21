"""Core domain models for OESDevTeam.

The enums in this module (`AgentRole`, `AgentStatus`, `DriftLevel`,
`TaskStatus`, `QualityGateType`) are the vocabulary of the pipeline: every
NATS event, every log line, every config key ultimately refers to one of
them.

Design rule: no string literals for these concepts anywhere in the codebase.
Always import the enum and use `.value`. That way `grep AgentRole` shows
every callsite, and renaming a role is a one-file change.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# Enums — the vocabulary of the pipeline
# -----------------------------------------------------------------------------


class AgentRole(StrEnum):
    """Agent roles recognised by the pipeline.

    Values MUST match keys under `roles:` in config/models.yaml — the config
    loader validates this at startup and refuses to start on a mismatch.

    When adding a role:
      1. Add the enum entry here.
      2. Add a prompt in prompts/ (or src/roles.py) for it.
      3. Add a mapping in config/models.yaml.
      4. Add an integration test that the controller can launch it.
    """

    # Core production roles (v1 carry-over) ------------------------------------
    PO = "po"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    DEVOPS = "devops"
    APPSEC = "appsec"
    UX_REVIEWER = "ux_reviewer"
    SUPPORT = "support"

    # v2 additions — the Senior Reviewer squad (see docs/ARCHITECTURE.md) ------
    SENIOR_BACKEND = "senior_backend"
    SENIOR_FRONTEND = "senior_frontend"
    SENIOR_DATA = "senior_data"
    SENIOR_PERFORMANCE = "senior_performance"
    BUSINESS_EXPERT = "business_expert"


class AgentStatus(StrEnum):
    """Lifecycle of a single agent invocation."""

    IDLE = "idle"
    WORKING = "working"
    DRIFTING = "drifting"
    WARNED = "warned"
    STOPPED = "stopped"
    KILLED = "killed"
    DONE = "done"
    FAILED = "failed"


class DriftLevel(StrEnum):
    """Drift classifier output (see src/drift_detector.py).

    A is ideal (directly solving the task). D is terminal (agent is lost or
    looping). The controller escalates at C, kills at D.
    """

    ON_TRACK = "A"  # Directly solving the task
    PREREQUISITE = "B"  # Necessary prerequisite work
    TANGENTIAL = "C"  # Fixing something tangential
    LOST = "D"  # Completely lost / looping / gold-plating


class TaskStatus(StrEnum):
    """Lifecycle of a task or feature in the pipeline."""

    BACKLOG = "backlog"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    STUCK = "stuck"
    NEEDS_COMMIT = "needs_commit"
    NEEDS_REWORK = "needs_rework"  # v2 — blocked by Senior Reviewer finding


class QualityGateType(StrEnum):
    """Gates the pipeline MUST NOT skip.

    Each gate is associated with a runner under src/gates/. A feature cannot
    be marked `done` unless every applicable gate returns PASS.
    """

    STATIC_ANALYSIS = "static_analysis"
    SECURITY_SCAN = "security_scan"
    FUNCTIONAL_TEST = "functional_test"
    INTEGRATION_TEST = "integration_test"
    QA_REVIEW = "qa_review"
    APPSEC_REVIEW = "appsec_review"
    UX_REVIEW = "ux_review"
    SENIOR_REVIEW = "senior_review"  # v2 — the five-reviewer gate
    API_CONTRACT = "api_contract"  # v2 — OpenAPI↔TS types consistency
    GOAL_VERIFICATION = "goal_verification"


# -----------------------------------------------------------------------------
# Event schema — what crosses NATS
# -----------------------------------------------------------------------------


class Event(BaseModel):
    """Envelope for every NATS message the pipeline emits.

    Carrying `model` on every event makes post-mortems tractable: when an
    output looks wrong, we can see immediately which model produced it.
    """

    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])

    # v2 addition: which model produced this event (None if not applicable —
    # e.g. a controller-emitted event has no model). See config/models.yaml.
    model: str | None = None

    def to_json(self) -> bytes:
        """Serialise to bytes suitable for nats_client.publish()."""
        return self.model_dump_json().encode("utf-8")


# -----------------------------------------------------------------------------
# Task budget — enforced, not aspirational
# -----------------------------------------------------------------------------


class TaskBudget(BaseModel):
    """Hard limits for a single agent invocation.

    `max_cost_usd` is the one the controller polices most aggressively: if a
    single agent burns through $5 of Opus without finishing, the task is more
    likely to be mis-specified than close-to-done, and we abort rather than
    doubling down.
    """

    time_minutes: int = 15
    max_tokens: int = 100_000
    max_cost_usd: float = 5.0
    max_subtask_minutes: int = 5
    max_attempts: int = 3

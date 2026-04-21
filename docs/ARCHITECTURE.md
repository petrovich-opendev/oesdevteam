# OESDevTeam — Architecture

This document grows step by step alongside the implementation. It covers the
shape of the system as it stands today; deferred sections are marked
`TODO (Step N)` with a pointer to the step that will fill them in.

## Business goal (restated)

Accept a specification (JSON/Markdown describing features) and produce
working, reviewed, deployed code. No manual intervention between spec and
product. If that cannot be achieved for a given spec, escalate with a
structured report rather than silently shipping something approximate.

## System shape

```
┌──────────┐      features.json       ┌─────────────────────┐
│  Author  │────────────────────────▶ │ FeatureController   │
│  (human) │                          │ (Python state mach.)│
└──────────┘                          └──────────┬──────────┘
                                                 │
                                                 ▼
                         ┌───────────────────────────────────────────┐
                         │ Per-feature pipeline (1 loop / feature)   │
                         │                                           │
                         │  Research → Plan → Implement → Verify →   │
                         │  Static checks → Security gate →          │
                         │  Senior Reviewer squad (parallel ×5) →    │
                         │  Git commit (only if gate green) →        │
                         │  Deploy & post-deploy verification        │
                         └───────────────────┬───────────────────────┘
                                             │
                                             ▼
                         ┌───────────────────────────────────────────┐
                         │ NATS JetStream (audit log, replayable)    │
                         │ + Langfuse (LLM trace + cost, TODO Step 6)│
                         └───────────────────────────────────────────┘
```

## Agent roles

Source of truth: `src/models.py::AgentRole`. As of Step 1 the enum holds
thirteen roles:

| Role              | Purpose                                       | Model (default) |
|-------------------|-----------------------------------------------|-----------------|
| PO                | Requirements decomposition                    | Opus 4.7        |
| Architect         | Solution design                               | Opus 4.7        |
| Developer         | Implementation                                | Opus 4.7        |
| QA                | Run verify commands, report pass/fail         | Sonnet 4.6      |
| DevOps            | Deploy, SRE review (Step 5)                   | Opus 4.7        |
| AppSec            | Security audit (Semgrep + Bandit)             | Opus 4.7        |
| UX Reviewer       | Playwright flow, a11y smoke                   | Sonnet 4.6      |
| Support           | Chores, cleanup                               | Sonnet 4.6      |
| Senior Backend    | Senior Reviewer squad — Python/API/OWASP      | Opus 4.7        |
| Senior Frontend   | Senior Reviewer squad — React/a11y/bundle     | Opus 4.7        |
| Senior Data       | Senior Reviewer squad — ETL, SQL, units       | Opus 4.7        |
| Senior Performance| Senior Reviewer squad — profiling, query plans| Opus 4.7        |
| Business Expert   | Senior Reviewer squad — domain terminology    | Opus 4.7        |

Per-role dollar caps and rationales live in `config/models.yaml`.

## Quality gates

Each gate is associated with an entry of `QualityGateType` in `src/models.py`.
A feature cannot be marked `done` unless every applicable gate returns PASS.

1. **Static analysis** — `ruff`, `tsc` (TS projects), typecheck on changes.
2. **Verify commands** — shell commands declared per feature in `features.json`.
3. **Security scan** — Semgrep + Bandit; HIGH severity blocks commit.
4. **Senior Reviewer squad** — TODO (Step 2): five reviewers in parallel,
   each emits structured findings, aggregator blocks on any BLOCKER.
5. **UX smoke** — Playwright, for features with a `ui_url`.
6. **API contract gate** — TODO (Step 4): OpenAPI ↔ TS types consistency.
7. **Goal verification** — post-deploy end-to-end smoke.

## Drift detection

The drift detector is one of the defences against the well-documented LLM
failure mode of "confidently doing the wrong thing". Every hook event from
the Claude CLI subprocess is classified A/B/C/D by `Haiku 4.5` (the cheapest
model that still classifies reliably):

- **A — ON_TRACK.** Continue.
- **B — PREREQUISITE.** Continue; this is legitimate scaffolding.
- **C — TANGENTIAL.** Warn. The agent is probably doing something useful
  but not what was asked; the controller inserts a "refocus" nudge.
- **D — LOST.** Terminate the subprocess. The agent is looping,
  over-engineering, or solving an unrelated problem.

Implementation landing: `src/drift_detector.py` (ported in a later step).

## Model routing (Step 1 — done)

Every `claude -p` invocation is built by
`src/claude_bridge.build_claude_cli_command()`. That function:

1. Resolves the agent role to a `ModelSpec` via `src/config.py`.
2. Assembles an argv that always contains `--model <name>` and
   `--max-budget-usd <amount>`.
3. Returns a frozen `ClaudeCliCommand` whose `.trace()` method emits only
   flag names (never the task prompt, system prompt, or tools list).

Env overrides: set `OESDEVTEAM_MODEL_<ROLE_UPPER>` (or
`OESDEVTEAM_PROFILE_<NAME>` for utility profiles like `drift_classifier`)
to override a single role for one run. Useful for cheap dry-runs or
A/B-testing a new model version before updating `models.yaml`.

## NATS event schema

Every event crossing the bus is a `src.models.Event` envelope:

```python
class Event(BaseModel):
    type: str                    # e.g. "agent.output", "drift.warning"
    data: dict[str, Any]
    ts: float
    id: str
    model: str | None            # which LLM produced this event (v2)
```

Carrying the model name on every event makes post-mortems tractable: a bad
output can be attributed to a specific model revision directly from the
log, without cross-referencing a separate config snapshot.

Subjects:

- `devteam.task.*` — feature/task lifecycle
- `devteam.agent.*` — agent assignments and outputs
- `devteam.controller.*` — controller decisions (retries, escalations)
- `devteam.drift.*` — drift detection signals
- `devteam.review.*` — Senior Reviewer findings (Step 2)
- `devteam.human.*` — human-approval requests (e.g. escalations)

## What is NOT in the architecture

This pipeline does NOT do:

- Silent failover between models (would destroy the reproducibility
  guarantee Step 1 establishes).
- Silent skipping of quality gates (would destroy the trust guarantee).
- LLM-driven arithmetic (Python computes, LLMs interpret).
- Client-side zero-knowledge encryption (server-side only, for now).

## Roadmap

See `PROGRESS.md` for step-by-step status. Steps 2–8 fill in: the Senior
Reviewer squad, the blocking review gate, API contract checks, DevOps SRE
gate, cost/trace observability, stuck-feature escalation, and the positive
learning loop.

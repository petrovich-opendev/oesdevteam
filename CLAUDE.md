# OESDevTeam — Project Rules

> Instructions for AI assistants (Claude / Cursor / etc.) working in this repo.
> Human contributors should read `CONTRIBUTING.md` (comes in a later step).

## Business goal (non-negotiable)

**Multi-agent pipeline that generates production code from a specification
without manual intervention.**

After every change, ask: *does this bring the pipeline closer to accepting a
spec and outputting working, reviewed, deployed code?* If not — revert.

## Architecture rule

**Platform code is domain-agnostic.** Anything domain-specific (mining terms,
healthcare terms, business rules) lives in `namespaces/<env>/<domain>/` as
configuration, never in `src/`.

## Technology stack

| Layer         | Technology                                           |
|---------------|------------------------------------------------------|
| Language      | Python 3.11+                                         |
| Lint/format   | `ruff check` + `ruff format`                         |
| Tests         | `pytest` (asyncio auto mode)                         |
| Validation    | Pydantic v2 at every boundary                        |
| Event bus     | NATS JetStream                                       |
| LLM worker    | Claude Opus 4.7 (via Claude Code CLI subprocess)     |
| LLM verifier  | Claude Sonnet 4.6 (fast, cheaper, for checks)        |
| LLM drift     | Claude Haiku 4.5 (classification only)               |
| Observability | Langfuse (traces + cost)                             |

**Model routing is explicit** — see `config/models.yaml`. Do NOT rely on
Claude CLI's global default model.

## Code style — HARD RULES

### Readability is not optional

Every public module, class, and function MUST have a docstring. Non-trivial
logic MUST have a WHY-comment explaining the rationale (not "what" — the code
says what; the comment says why this approach was chosen over alternatives).

Named constants MUST have a comment explaining their business meaning:

```python
# BAD: MAX_RETRIES = 3
# GOOD:
# Three attempts matches the devteam v1 failure distribution:
# 90% of transient issues resolve by retry #2, retry #3 is diminishing returns.
MAX_RETRIES = 3
```

**Why this rule exists:** this project is public on GitHub. Other engineers
will read, fork, and improve it. Code without rationale is a black box they
cannot contribute to.

### Production-ready from the first write

- No TODOs in committed code.
- No hardcoded values (credentials, IDs, URLs, thresholds) — use config or env.
- Error handling on every path; never swallow exceptions silently.
- Parameterised queries only. Never string-concatenate SQL.
- Validate at system boundaries (user input, external APIs) with Pydantic.

### Scope discipline

- Don't fix anything outside the current task.
- Don't refactor without an explicit request.
- Don't add features "while you're there".
- Delete dead code; don't leave commented-out blocks.

## Git conventions

- Branches: `feat/`, `fix/`, `chore/`, `hotfix/`, `release/` (Conventional
  Commits).
- Commits are created only on explicit request.
- Commits reference a feature id where applicable (e.g. `feat(MR-001): ...`).
- Hooks MUST NOT be skipped (`--no-verify` is forbidden).

## Agent roles

See `src/models.py::AgentRole` for the authoritative list. As of this step the
roles include: PO, Architect, Developer, QA, AppSec, DevOps, UX Reviewer, and
five Senior Reviewers (Backend / Frontend / Data / Performance / Business).

## Quality gates (cannot be skipped)

1. Static analysis (`ruff`, `tsc` for TS)
2. Verify commands from `features.json`
3. Playwright smoke test (if `ui_url` is specified)
4. Security gate (Semgrep + Bandit)
5. **Senior Reviewer squad** — five reviewers in parallel; any blocker → commit refused
6. Git commit
7. Deploy + post-deploy verification

If any gate fails, the feature enters `needs_rework` with structured feedback
for the next attempt. After `max_attempts` retries, the feature is escalated
with a root-cause report.

## External data resilience (HARD RULE)

Any code that consumes messages from an external system — brokers
(Kafka / NATS / MQTT / RabbitMQ), SCADA, FMS, OPC-UA, ModBus, vendor
telemetry — obeys `docs/RESILIENCE_RULES.md` (R-1 … R-12). No
single malformed message, renamed tag, or transient connection blip
may take the service down. Worst allowed outcome is "drop the
message, increment a counter, surface via health check, keep
consuming". Senior Backend, Senior Data, and Senior SRE reviewers
enforce this; violations are BLOCKER or MAJOR per the rule document.

## What NOT to do

- Do NOT use Go — Python only.
- Do NOT use Vue — React only on any frontend work.
- Do NOT use Temporal — NATS JetStream for agent orchestration.
- Do NOT let an LLM perform arithmetic — code computes, LLM interprets.
- Do NOT hardcode domain-specific logic in `src/`.
- Do NOT write a fallback that silently hides an error — fail loudly.
- Do NOT hardcode external vendor tag names (SCADA / FMS / OPC-UA) in
  Python — they belong in a tag-mapping config. Violation = BLOCKER.
- Do NOT deploy a consumer without `messages_dropped_total{reason}`
  metric and drop-rate signal in the health endpoint — the service
  becomes operationally blind.

# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
with semantic versioning once the first tagged release lands.

## [Unreleased]

### v2 milestones (in current repo)

- **Step 1 — Pin Opus 4.7 + explicit model routing.**
  `config/models.yaml` maps every agent role to a specific model. Every
  `claude -p` call now carries `--model <name>` and `--max-budget-usd`
  via `build_claude_cli_command`.
- **Step 2 — Senior Reviewer squad.** Five reviewers (Senior Backend,
  Senior Frontend, Senior Data, Senior Performance, Business Domain
  Expert) run in parallel via `asyncio.gather`; strict JSON output
  contract parsed by `parse_review_response`; tolerant of fenced
  Markdown and chatty prose.
- **Step 3 — Blocking Code Review Gate.** `SquadResult.aggregate_verdict`
  is pessimistic — any reviewer's `needs_rework` or any BLOCKER finding
  blocks the commit and feeds a Markdown report back as reflection.
- **Step 4 — API Contract Gate.** Deterministic check that the
  backend schema, OpenAPI artefact, and generated TypeScript types
  move together. Blocks on schema-without-OpenAPI or OpenAPI-without-
  types; configurable glob patterns in `config/api_contract.yaml`.
- **Step 5 — Senior SRE Review Gate.** Deploy-readiness reviewer
  invoked only when deploy-surface files (Dockerfiles, k8s manifests,
  migrations, nginx configs, CI workflows) are touched.
- **Step 6 — Cost tracker + optional Langfuse exporter.** Aggregate
  USD across every LLM call per pipeline run, with hard cap and
  soft-warning threshold. Optional Langfuse trace export — falls back
  to `NullExporter` when the SDK is not installed.
- **Step 7 — Stuck auto-escalation.** Deterministic Markdown report
  generator for features that exhaust their retry budget; rule-based
  next-step suggestions.
- **Step 8 — Positive learning loop.** Append-only `success_patterns.md`
  alongside `lessons_learned.md`; loader composes both into the agent
  prompt preamble.
- **Step 9 — External data resilience rules.** Canonical 12-rule
  document (`docs/RESILIENCE_RULES.md`) covering consumers of brokers,
  SCADA, FMS, OPC-UA and similar industrial data sources. Referenced
  by Senior Backend, Senior Data, and Senior SRE reviewer prompts.
- **Adaptive domain context.** Three-tier loader (explicit `DOMAIN.md`
  → on-disk cache → Opus 4.7 enrichment) so the Business Expert
  reviewer gets a specific brief per namespace rather than a generic
  placeholder.
- **Runner cost aggregation.** `ClaudeCliReviewerRunner` exposes
  `total_cost_usd` and `call_log` so orchestrators can bill per-call
  spend into `CostTracker`.

### Notes

The CLI integration tests (`tests/test_cli_integration.py`) require
the Claude Code CLI and therefore skip on CI runners that do not ship
with it. Every pure-Python test still runs.

# OESDevTeam — v2 Upgrade Progress

Tracks the step-by-step implementation of DevTeam v2. Each step is executed,
self-reviewed, and checked for business-goal alignment before moving on.

**Business goal:** *Multi-agent pipeline that generates production code from a
specification without manual intervention.*

After every step answer: *does this step bring the pipeline closer to the
business goal?* If not — revert and reconsider.

## Roadmap

| # | Block                                         | Priority | Status        |
|---|-----------------------------------------------|----------|---------------|
| 1 | Pin Opus 4.7 + model routing                  | P0       | ✅ done        |
| 2 | 5 Senior Reviewers (BE/FE/Data/Perf/Business) | P0       | ✅ done        |
| 3 | Blocking Code Review Gate                     | P0       | ✅ done        |
| 4 | API Contract Gate (OpenAPI → TS)              | P1       | ✅ done        |
| 5 | DevOps SRE Review gate                        | P1       | ✅ done        |
| 6 | Langfuse + Cost budget enforce                | P1       | ✅ done        |
| 7 | Stuck auto-escalation                         | P2       | ✅ done        |
| 8 | Positive learning loop                        | P2       | ✅ done        |

## Step 1 — Pin Opus 4.7 + model routing — ✅ done

**Goal:** explicit, per-role LLM model configuration via `config/models.yaml`
and env overrides. Remove the hidden dependency on Claude CLI's global model
default.

**Delivered:**
- `config/models.yaml` — role → model mapping with reasoning and per-call
  dollar cap (`max_cost_usd`)
- `src/config.py` — Pydantic-validated loader with `OESDEVTEAM_MODEL_<ROLE>`
  env overrides
- `src/claude_bridge.py` — every `claude -p` call now carries
  `--model <name>` and `--max-budget-usd <n>`; trace output omits all
  sensitive positional arguments
- `src/models.py` — canonical `AgentRole` enum (13 roles) incl. the five
  Senior Reviewer placeholders
- `tests/conftest.py` — autouse env-isolation fixture
- `tests/test_model_routing.py` — 17 tests covering resolver, env overrides,
  argv layout, trace scrubbing, and goal alignment
- `pyproject.toml` — ruff + pytest configured, docstring rules enforced

**Verification:**
- `ruff check .` → clean
- `pytest -q` → 17 / 17 pass
- `python3 -c "from src.config import get_model_for_role; assert get_model_for_role('developer') == 'claude-opus-4-7'"` → ok

**Self-review adjustments (after critic pass):**
- Replaced deprecated camelCase `--allowedTools` with canonical
  `--allowed-tools`
- Replaced unenforced `max_tokens` field with CLI-enforced `max_cost_usd`
  (via `--max-budget-usd`)
- Removed misleading `reload_config()` calls from env-override tests
- Hardened `resolve_claude_executable()` with `X_OK` check
- `ClaudeCliCommand.trace()` now emits flag names only — no positional
  payload
- Unified model naming convention (no mixed aliases/snapshots)
- Removed absolute filesystem path leak from the README

**Business-goal alignment:** a pipeline that secretly depends on a global CLI
setting cannot reliably generate production code — a teammate with a
different global default would produce different (possibly worse) output.
Explicit model pinning plus a per-call dollar ceiling is a prerequisite for
reproducible, budgeted, auditable codegen. ✅

## Step 2 — Senior Reviewer squad — ✅ done

**Goal:** replace the single ARCHITECT-review of v1 with five specialised
Senior Reviewers running in parallel and producing structured findings.

**Delivered:**
- `prompts/reviewers/*.md` — five production-grade review prompts
  (Senior Backend, Senior Frontend, Senior Data, Senior Performance,
  Business Expert) with explicit JSON output contract, severity
  calibration, and prompt-injection resistance rules.
- `src/reviewers/findings.py` — `Finding`, `ReviewResult`, `SquadResult`,
  `parse_review_response`. Parser tolerates fenced JSON and chatty
  prose; unparseable responses become `reviewer_fault` (MAJOR), never a
  silent approve.
- `src/reviewers/runner.py` — `ReviewerRunner` Protocol,
  `ClaudeCliReviewerRunner` (real subprocess), `MockReviewerRunner`
  (tests).
- `src/reviewers/squad.py` — `run_reviewer_squad` with asyncio.gather,
  per-reviewer and whole-squad timeouts, duplicate-role rejection,
  sentinel-wrapped prompt-injection isolation.
- Business Expert prompt reads `{{domain_context}}` pulled from
  `namespaces/<env>/<domain>/CLAUDE.md` — domain-pluggable without code
  changes.

**Self-review adjustments (post-critic pass):**
- Prompt-injection hardening: every untrusted field wrapped in
  `<<<UNTRUSTED_DATA_BEGIN>>>` / `<<<UNTRUSTED_DATA_END>>>` sentinels,
  preamble instructs the reviewer to ignore instructions inside
  sentinels and flag such attempts as BLOCKER `prompt_injection_attempt`.
- Squad-level wall-time cap (`DEFAULT_SQUAD_TIMEOUT_SECONDS = 600`)
  with graceful cancellation → reviewer_fault fallback for every
  unfinished reviewer.
- Duplicate-role guard in `run_reviewer_squad`.
- Senior Backend prompt's verdict rule simplified to match
  `SquadResult.aggregate_verdict` (any blocker or major → needs_rework).

**Verification:**
- `ruff check .` → clean
- `ruff format --check .` → clean
- `pytest -q` → 61 / 61 pass (54 existing + 7 new for Step 2 hardening)

**Business-goal alignment:** an autonomous pipeline cannot "generate
production code without manual intervention" if there is no reviewer
strong enough to block bad code. Five specialised reviewers running in
parallel with a pessimistic aggregator is what replaces the human PR
reviewer the v1 pipeline implicitly assumed. Step 3 will wire this
squad into the pre-commit gate so the verdict actually stops broken
code from landing. ✅

## Step 3 — Blocking Code Review Gate — done

**Goal:** make the Senior squad's verdict *binding*. v1 had a reviewer
whose opinion was advisory; v2 enforces it as a pre-commit gate that
refuses to merge when the squad reports blockers or a ``needs_rework``
verdict.

**Delivered:**
- `src/gates/base.py` — abstract `Gate` Protocol, shared `GateInput` /
  `GateResult` schemas, `format_gate_report` header renderer. Ready to
  carry the API-contract (Step 4) and SRE-review (Step 5) gates without
  interface churn.
- `src/gates/code_review_gate.py` — `CodeReviewGate` class and
  `run_code_review_gate` function wrap the squad from Step 2; passes
  iff `aggregate_verdict == APPROVE`. `render_code_review_report`
  produces plain-Markdown reports (no emoji) suitable for CI logs and
  GitHub comments.
- `tests/test_code_review_gate.py` — 14 tests covering happy path,
  blocker path, majors-only path, reviewer fault, report rendering,
  contract with the aggregate verdict.

**Self-review adjustments (post-critic pass):**
- Fixed `_format_block_reason` to pick the representative finding from
  blockers+majors only (previously `all_findings()[0]` could have
  chosen a minor in degenerate cases).
- Strengthened the majors-only test — asserts `aggregate_verdict`,
  `blockers` count, and summary text in the reason line rather than
  a loose substring match.
- Typed `roles: tuple[AgentRole, ...]` instead of bare `tuple`.
- Replaced emoji report headers with `[PASS]` / `[BLOCK]` per
  project style rules.
- Documented `allow_retry=True` as a hint — the controller remains
  responsible for bounding retries and discriminating
  reviewer_fault (transient) vs. real blocker (persistent).
- Added business-context comment to `DEFAULT_GATE_TIMEOUT_SECONDS`.

**Verification:**
- `ruff check .` → clean
- `ruff format --check .` → clean
- `pytest -q` → 75 / 75 pass (61 prior + 14 new)

**Business-goal alignment:** ✅ step 3 closes the last P0 gap. Without
a binding gate the five Senior Reviewers are an elaborate advisory
system; with it, Opus 4.7 + per-role routing + five parallel reviewers
become an actual quality barrier that refuses to ship bad code.
Combined with Steps 1-2 the P0 sub-goal — *"block bad codegen from
landing without manual intervention"* — is met.

## Step 4 — API Contract Gate — done

**Goal:** stop the frontend/backend contract-drift bug class (the
classic "frontend says `username`, backend returns `telegram_chat_id`"
outage). The gate blocks a feature if a Pydantic schema change
arrives without a matching OpenAPI dump update, or if the OpenAPI
dump changes without regenerated TypeScript types.

**Delivered:**
- `config/api_contract.yaml` — three pattern buckets (backend schema /
  OpenAPI artefact / frontend types) as the single source of truth for
  what counts as "contract surface". Namespace-local YAMLs can
  override.
- `src/gates/api_contract_gate.py` — `ApiContractConfig` (immutable
  loaded patterns), `ApiContractGate` (checks two invariants),
  `run_api_contract_gate` (function wrapper), `render_api_contract_report`
  (emoji-free Markdown).
- `tests/test_api_contract_gate.py` — 16 tests covering docs-only
  applicability, all-synced pass, schema-without-openapi block,
  openapi-without-types block, YAML loader error paths, function/class
  equivalence, report rendering, emoji check.

**Deliberate non-goal:** this gate does NOT semantically diff OpenAPI
against TypeScript. Requiring the two artefacts to be co-committed is
a cheap deterministic proxy; a deep semantic diff is a heavier problem
for a later step.

**Verification:**
- `ruff check .` → clean
- `ruff format --check .` → clean
- `pytest -q` → 91 / 91 pass (+16 new)

**Business-goal alignment:** ✅ closes one of the most expensive
repeated failures from v1 — contract drift — without adding an LLM
dependency. This is the kind of gate a pipeline must have to claim
"production code without manual intervention".

## Step 5 — DevOps SRE Review Gate — done

**Goal:** catch deploy-layer risks (missing rollback plan, irreversible
migrations, resourcelimit absence, stripped observability) that the
application-focused squad does not cover. Runs only on features that
touch actual deploy surface — otherwise spending an Opus call on a
CSS tweak would be waste.

**Delivered:**
- `config/sre_review.yaml` — deploy-surface glob patterns (Dockerfile,
  docker-compose, k8s manifests, Terraform, nginx, systemd, CI,
  migrations, env-templates).
- `prompts/reviewers/senior_sre.md` — SRE checklist (blast radius,
  rollback, migrations, health, observability, secrets, resources,
  security), same JSON contract as the five squad reviewers,
  prompt-injection resistance preamble included.
- `src/gates/sre_review_gate.py` — `SreReviewGate` class,
  `run_sre_review_gate` function, `render_sre_review_report`. Reuses
  `ReviewerRunner` / `parse_review_response` / `build_task_message`
  from the squad, so mocks and parser stay consistent across gates.
- `AgentRole.SENIOR_SRE` added to the vocabulary, mapped in
  `config/models.yaml` to Opus 4.7 with a $1.00 ceiling.
- `QualityGateType.SRE_REVIEW` added for explicit classification (was
  previously a placeholder on APPSEC_REVIEW during initial scaffolding).
- `tests/test_sre_review_gate.py` — 15 tests covering applicability
  short-circuit, approve path, blocker path, major path, reviewer
  crash, invalid JSON, config loading, report rendering, emoji-free
  output.

**Design properties:**
- Short-circuits to PASS without invoking the reviewer when the feature
  does not touch deploy surface — confirmed by test that asserts
  `runner.calls == []`.
- Any blocker or major finding blocks; contract matches the squad gate.
- Parser errors become reviewer_fault → blocker (major-severity),
  never silent approve.

**Verification:**
- `ruff check .` → clean
- `ruff format --check .` → clean
- `pytest -q` → 106 / 106 pass (+15 new)

**Business-goal alignment:** ✅ plugs the deploy-layer hole that v1
lessons-learned identified as a recurring source of production
outages. Now a feature that adds a Dockerfile, migration, or k8s
manifest is held to the same "no ambiguous silent pass" standard as
application code — required for autonomous codegen that actually
deploys, not just compiles.

## Step 6 — Langfuse + Cost budget enforce — done

Added `src/observability/`: `CostTracker` (aggregate USD per feature,
`BudgetExceeded` on hard-cap breach, soft-warning threshold) and the
optional Langfuse exporter with a graceful `NullExporter` fallback.
Closes the runaway-spend vector where per-call caps silently sum into
unbounded aggregate cost. 18 tests; 124/124 total.

## Step 7 — Stuck auto-escalation — done

`src/controller/escalation.py`: deterministic Markdown report generator
for features that exhaust their retry budget. Surfaces gate-frequency
analysis, repeated-blocker highlighting, and rule-based next-step
suggestions. No LLM in the base path — report builds from data already
on disk.

## Step 8 — Positive learning loop — done

`src/controller/learning.py`: `extract_success_pattern` +
`append_success_pattern` + `load_memory_blob`. Pipeline now learns
from wins as well as failures — successful features append a stanza
to `success_patterns.md`, and the controller embeds both lessons and
patterns into every agent's system prompt before a run. Tail-sliced
so a bloated memory file cannot eat the agent's context.

Total verification:
- `ruff check .` → clean
- `ruff format --check .` → clean
- `pytest -q` → **149 / 149 pass**

All 8 blocks of the v2 roadmap are complete.

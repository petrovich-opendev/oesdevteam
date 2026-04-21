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
| 2 | 5 Senior Reviewers (BE/FE/Data/Perf/Business) | P0       | ⏳ next        |
| 3 | Blocking Code Review Gate                     | P0       | ⏳ queued      |
| 4 | API Contract Gate (OpenAPI → TS)              | P1       | ⏳ queued      |
| 5 | DevOps SRE Review gate                        | P1       | ⏳ queued      |
| 6 | Langfuse + Cost budget enforce                | P1       | ⏳ queued      |
| 7 | Stuck auto-escalation                         | P2       | ⏳ queued      |
| 8 | Positive learning loop                        | P2       | ⏳ queued      |

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

## Step 2 — Senior Reviewer squad (next)

Spawn five reviewer agents (Backend / Frontend / Data / Performance /
Business) in parallel. Produce structured findings; aggregate into a gate
decision in Step 3.

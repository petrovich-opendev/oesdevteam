# OESDevTeam

[![CI](https://img.shields.io/github/actions/workflow/status/petrovich-opendev/oesdevteam/ci.yml?branch=main&label=ci)](https://github.com/petrovich-opendev/oesdevteam/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Model](https://img.shields.io/badge/LLM-Claude%20Opus%204.7-8A2BE2)](config/models.yaml)

Production-grade multi-agent code generation pipeline. Give it a specification,
get working, reviewed, committed code.

## What it is

OESDevTeam is an **autonomous multi-agent system** that takes a specification
(a `features.json` describing what must be built) and produces production-ready
code that is:

- Written by specialised agents (PO → Architect → Developer → QA → DevOps)
- Reviewed by **five Senior Reviewers in parallel** before every commit:
  - Senior Backend Engineer (Python/FastAPI, OWASP, async, pydantic)
  - Senior Frontend Engineer (React/TS, WCAG 2.2 AA, bundle budgets, CSP)
  - Senior Data Engineer (ClickHouse/PostgreSQL, idempotency, parameterised SQL)
  - Senior Performance Engineer (profiling, query plans, Core Web Vitals)
  - Business Domain Expert (actionable outcomes, domain terminology)
- Security-scanned (Semgrep + Bandit) with HIGH severity as a commit blocker
- End-to-end verified (real HTTP calls, real DB queries, Playwright smoke tests)
- Committed to git only if every gate passed

## Business goal

**Generate production code from a specification without manual intervention.**

If the pipeline cannot complete the task autonomously, it escalates with a
structured report rather than guessing.

## Why it exists

Most "AI codegen" demos hallucinate dependencies, skip tests, and ship insecure
code (40-62% of AI-generated code has security issues per recent research).
OESDevTeam addresses this with:

1. **Explicit model pinning** — Opus 4.7 for reviewers, Sonnet 4.6 for fast
   checks, Haiku 4.5 for classification. No dependency on a global CLI config.
2. **Blocking quality gates** — no gate can be silently skipped.
3. **Deterministic orchestration** — a Python state machine supervises LLM
   agents, which drift by nature.
4. **Lessons-learned memory** — every failure is recorded and fed back into
   future prompts.

## Status

**v2 library complete.** All nine roadmap blocks have landed (see
[PROGRESS.md](PROGRESS.md) and [CHANGELOG.md](CHANGELOG.md)).
Validated end-to-end on the real Claude Code CLI across seven smoke
runs against both trivial and BioCoach-flavoured Telegram-bot code.
Adaptive domain context (Opus 4.7) is cached per-namespace; the
blocking Code-Review Gate catches real issues (scope violations of
acceptance scripts, Russian-language terminology mismatches in
Telegram copy, `__pycache__` artefacts) before commit.

### What this repo ships

- A **library** of v2 quality gates, Senior Reviewer squad, adaptive
  domain-context loader, cost tracker, and learning loop (see
  `src/`). 179 unit and integration tests.
- A **review-only CLI** (`run_features.py`) — run the gate chain
  against a namespace's current working tree to get a Markdown
  verdict. No worker invocation, no commits; a tool for "did my
  change pass the squad?"
- A **smoke runner** (`scripts/smoke_squad.py`) — end-to-end check
  against the real Claude CLI on a fixed diff.

### What this repo does NOT ship

A turnkey autonomous ``FeatureController`` state machine (worker →
verify → gate → reflection → retry → commit → deploy). The reference
implementation of that controller lives in a companion internal
project. To wire the library here into your own controller, follow
[`docs/INTEGRATION_EXAMPLE.md`](docs/INTEGRATION_EXAMPLE.md) — the
recipe is ~30 lines of integration code.

OESDevTeam is the public, reviewer-heavy evolution of an internal
multi-agent pipeline (~170 features shipped, 96% success) that
outgrew its advisory-only review stage.

## Quick start

```bash
# Prerequisites: Python 3.11+, Claude Code CLI, NATS JetStream (optional), git
git clone https://github.com/petrovich-opendev/oesdevteam.git
cd oesdevteam
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the 179-test suite — offline, zero cost, ~1.5 s
pytest

# Optional smoke test against the real Claude CLI
python3 scripts/smoke_squad.py --roles senior_backend    # ~$0.20
python3 scripts/smoke_squad.py                           # full five-reviewer, ~$1
```

### Review-only CLI

``run_features.py`` takes a namespace directory with ``features.json``
and runs the gate chain against the current working tree — without
launching any worker agent or committing anything. Useful when you
already wrote the code (manually or with your own agent) and want the
Senior squad's verdict:

```bash
# Full gate chain: API contract → Senior squad (5 × Opus) → SRE gate.
# Cost ≈ $1 per feature with non-trivial diff.
python3 run_features.py namespaces/dev/my-feature

# Deterministic only — skip every LLM-backed gate.
python3 run_features.py namespaces/dev/my-feature --dry-run

# Single gate (useful for fast iteration on contract-only changes).
python3 run_features.py namespaces/dev/my-feature --only=api-contract
```

### Env-var toggles

```bash
# Emergency bypass — skip the Senior squad entirely.
OESDEVTEAM_SENIOR_REVIEW_MODE=disabled python3 run_features.py <namespace>

# Skip the Opus-backed adaptive domain brief (falls back to raw signals).
OESDEVTEAM_DOMAIN_BRIEF_DISABLED=1 python3 run_features.py <namespace>

# Override the model for a single role (handy for A/B experiments).
OESDEVTEAM_MODEL_SENIOR_BACKEND=claude-sonnet-4-6 python3 run_features.py <namespace>
```

See [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md) for the
step-by-step procedure to fold these modules into an existing
multi-agent controller, and
[`docs/INTEGRATION_EXAMPLE.md`](docs/INTEGRATION_EXAMPLE.md) for the
minimum code delta that wires the v2 gates into a typical
feature-controller state machine.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design: agent
roles, state machine, quality gates, drift detection, and model routing.

## Real-world validation

OESDevTeam's Senior Reviewer squad, adaptive domain context, and binding
code-review gate have been validated end-to-end on the real Claude Code CLI
against code for [**agentdata.pro**](https://agentdata.pro/) — a live
Telegram-based retail health-coaching service. The smoke runs exercised
the Business Domain Expert reviewer with an Opus-generated, BioCoach-
specific domain brief, and the squad caught concrete issues (scope
violations of acceptance scripts, Russian-language terminology mismatches
in Telegram bot copy) that a generic linter would miss.

## Author

**Ruslan Karimov** — [rkarimov.mail@gmail.com](mailto:rkarimov.mail@gmail.com)

Questions, feedback, and PRs welcome via the
[issues tracker](https://github.com/petrovich-opendev/oesdevteam/issues).

## License

MIT — see [LICENSE](LICENSE).

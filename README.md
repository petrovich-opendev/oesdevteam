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

**v2 complete and migrated into production.** All nine roadmap blocks
have landed (see [PROGRESS.md](PROGRESS.md) and [CHANGELOG.md](CHANGELOG.md)).
Validated end-to-end on the real Claude Code CLI across seven smoke
runs against both trivial and BioCoach-flavoured Telegram-bot code.
Adaptive domain context (Opus 4.7) cached per-namespace; blocking
Code-Review Gate catches real issues (e.g. `__pycache__` artefacts,
missing gitignore rules) before commit.

OESDevTeam is the public, reviewer-heavy evolution of an internal v1
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

Smoke-test modes can also be controlled via env vars at runtime:

```bash
# Non-blocking advisory (useful on a soak period before flipping to binding)
OESDEVTEAM_SENIOR_REVIEW_MODE=advisory python3 run_features.py <namespace>

# Emergency bypass — skip the squad entirely
OESDEVTEAM_SENIOR_REVIEW_MODE=disabled python3 run_features.py <namespace>

# Skip the Opus-backed adaptive domain brief (fall back to raw signals)
OESDEVTEAM_DOMAIN_BRIEF_DISABLED=1 python3 run_features.py <namespace>
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

## License

MIT — see [LICENSE](LICENSE).

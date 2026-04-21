# OESDevTeam

Production-grade multi-agent code generation pipeline. Give it a specification,
get working, reviewed, deployed code.

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

🚧 **v2 upgrade in progress.** See [PROGRESS.md](PROGRESS.md) for step-by-step
implementation status. OESDevTeam is the public, hardened, reviewer-heavy
evolution of an internal v1 pipeline that shipped ~170 features before the
authors decided the quality gates needed a complete redesign — hence this
repository.

## Quick start

```bash
# Prerequisites: Python 3.11+, Claude Code CLI, NATS JetStream, git
pip install -e ".[dev]"

# Run tests
pytest

# (After bootstrap is complete:)
# python3 run_features.py namespaces/dev/<your-project>
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design: agent
roles, state machine, quality gates, drift detection, and model routing.

## License

MIT — see [LICENSE](LICENSE).

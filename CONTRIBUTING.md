# Contributing to OESDevTeam

Thanks for your interest. This is a public reference implementation; pull
requests, issues, and design discussions are welcome.

## Project philosophy

- **Production-grade from the first write.** No TODOs in merged code; no
  placeholders; no "I'll clean it up later". The bar is: would a senior
  engineer accept this in a real PR today?
- **Readability is part of correctness.** Every public module, class, and
  function has a docstring. Non-trivial logic has a WHY-comment. Constants
  have a business-meaning comment. Code without those is incomplete, not
  "stylistic preference".
- **Quality gates cannot be silently skipped.** If a gate is in the way
  of your PR, either fix what it's flagging, or argue explicitly why the
  gate should not apply to this case — in the PR description.

## Local setup

Requirements:

- Python 3.11+
- `claude` CLI (Claude Code) — install from https://docs.claude.com/en/docs/claude-code/overview
- NATS JetStream (only when running the full pipeline; unit tests don't need it)

```bash
git clone <repo>
cd OESDevTeam
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Sanity check
ruff check .
pytest -q
```

## Before opening a PR

Every PR must pass, locally:

```bash
ruff check .
ruff format --check .
pytest -q
```

A PR that fails any of the three will not be merged.

If your change touches `src/claude_bridge.py` or `config/models.yaml`,
include a note in the PR explaining which roles are affected and why.

## Commit messages

Conventional Commits. Examples:

- `feat(reviewers): add senior performance reviewer prompt`
- `fix(bridge): handle --max-budget-usd when budget is zero`
- `chore(deps): bump pydantic to 2.6`

Branch prefixes: `feat/`, `fix/`, `chore/`, `hotfix/`, `release/`.

## Reviewing

When you review someone else's PR, try on the five Senior Reviewer hats:

1. **Senior Backend** — async correctness, error paths, DB transactions.
2. **Senior Frontend** — a11y, hooks, re-renders, bundle budget.
3. **Senior Data Engineer** — idempotency, parameterised SQL, units.
4. **Senior Performance Engineer** — Big-O, query plans, memory.
5. **Business Domain Expert** — does this add user value?

If any hat finds a BLOCKER, say so explicitly. If nobody speaks up for the
end user, the code is missing a reviewer.

## Code of conduct

Be kind; be direct. Disagreements are welcome; personal attacks are not.

## License

By contributing you agree your contribution is licensed under MIT (see
`LICENSE`).

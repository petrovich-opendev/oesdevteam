"""Smoke test — run the Senior Reviewer squad against a real Claude CLI.

This is the end-to-end sanity check for Steps 1-3 combined:

  - Step 1 model routing picks ``claude-opus-4-7`` per role.
  - Step 2 squad launches five reviewers in parallel via the real CLI.
  - Step 3 code-review gate aggregates the verdict.

Running costs ~$1 of Opus time and ~60-120 s wall clock. Safe to run on a
throwaway diff — the reviewers do not write any files. Output goes to
stdout as pretty-printed JSON plus a Markdown gate report.

Usage
-----
::

    # from repo root
    python3 scripts/smoke_squad.py

The script refuses to start if ``claude`` is not on PATH (or
``OESDEVTEAM_CLAUDE_BIN`` / ``CLAUDE_CODE_BIN`` env vars not set). That
avoids accidental silent failures when running in minimal shells.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from src.gates import GateInput, run_code_review_gate
from src.gates.code_review_gate import render_code_review_report
from src.reviewers import ClaudeCliReviewerRunner

# A minimal diff that exercises a realistic reviewer pass: introduces a
# FastAPI endpoint with intentional issues (hardcoded secret, no input
# validation) so reviewers have something to find — but tiny enough that
# the Opus cost stays bounded.
_SMOKE_DIFF = """\
diff --git a/src/app.py b/src/app.py
new file mode 100644
--- /dev/null
+++ b/src/app.py
@@ -0,0 +1,18 @@
+from fastapi import FastAPI, Request
+
+app = FastAPI()
+
+# Hardcoded secret — reviewer should flag.
+API_KEY = "sk-test-abcdef"
+
+@app.post("/users")
+async def create_user(request: Request):
+    body = await request.json()
+    # No validation of body shape.
+    username = body["username"]
+    sql = f"INSERT INTO users (name) VALUES ('{username}')"
+    # Also no error handling.
+    execute(sql)
+    return {"ok": True, "key": API_KEY}
"""


_SMOKE_INPUT = GateInput(
    feature_id="SMOKE-001",
    feature_goal=(
        "Expose a POST /users endpoint that accepts a JSON body and stores "
        "the user in the database."
    ),
    files_changed=["src/app.py"],
    diff=_SMOKE_DIFF,
    verify_commands=["ruff check .", "pytest -q", "curl -X POST /users -d '{}'"],
    domain_context="Generic FastAPI service. No specific industry glossary.",
)


async def main(limit_roles: list[str] | None = None) -> int:
    """Run the gate and print the report. Returns a process exit code."""
    runner = ClaudeCliReviewerRunner()

    if limit_roles:
        # Narrow the squad for a cheap single-reviewer smoke run.
        from src.models import AgentRole

        roles = tuple(AgentRole(r) for r in limit_roles)
    else:
        from src.reviewers import REVIEWER_ROLES

        roles = REVIEWER_ROLES

    print(f"Running squad: {[r.value for r in roles]}", file=sys.stderr)
    result = await run_code_review_gate(_SMOKE_INPUT, runner, roles=roles)

    # Pretty JSON first (for operators / machine consumption), then the
    # Markdown report (for humans). Both to stdout so a simple tee works.
    print(json.dumps(result.model_dump(), indent=2, default=str))
    print()
    print(render_code_review_report(result))

    return 0 if result.passed else 1


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roles",
        nargs="*",
        default=None,
        help=(
            "Optional subset of reviewer roles to run (e.g. senior_backend). "
            "Default: full five-role squad."
        ),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.roles)))


if __name__ == "__main__":
    _cli()

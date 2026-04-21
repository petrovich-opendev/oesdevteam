#!/usr/bin/env python3
"""Minimal CLI wrapper — run v2 quality gates over a namespace.

What this script does
---------------------
Given a namespace directory (containing ``features.json`` and an
already-modified working tree), this script walks every pending feature
and runs the gate chain **in review-only mode**:

  1. API Contract Gate (deterministic, no LLM cost)
  2. Senior Reviewer squad (five Opus-4.7 reviewers in parallel)
  3. SRE Review Gate (deploy-surface features only)

It does NOT launch worker agents, does NOT retry, does NOT commit, and
does NOT deploy. Think of it as ``eslint --fix=false`` for your LLM-
assisted development flow: you (or your own worker) wrote the code;
this tool tells you whether OESDevTeam's Senior squad would pass it.

For the full autonomous state machine (worker → verify → gates →
reflection → retry → commit → deploy), wire the modules into your own
``FeatureController`` using the recipe in ``docs/INTEGRATION_EXAMPLE.md``.
The v1 reference controller that exercises these modules end-to-end
lives in a companion internal repo; the library published here is the
reviewer-heavy v2 layer.

Usage
-----
::

    # Review every pending feature in a namespace
    python3 run_features.py namespaces/dev/my-feature

    # Dry run — report applicability of each gate without calling any LLM
    python3 run_features.py namespaces/dev/my-feature --dry-run

    # Only the API contract gate (cheap, deterministic, no Opus calls)
    python3 run_features.py namespaces/dev/my-feature --only=api-contract
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Make sure the ``src`` package is importable when the script is run
# from the repo root without ``pip install -e .``.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.gates import (  # noqa: E402
    ApiContractConfig,
    ApiContractGate,
    CodeReviewGate,
    GateInput,
    SreReviewConfig,
    SreReviewGate,
)
from src.gates.api_contract_gate import render_api_contract_report  # noqa: E402
from src.gates.code_review_gate import render_code_review_report  # noqa: E402
from src.gates.sre_review_gate import render_sre_review_report  # noqa: E402
from src.reviewers import ClaudeCliReviewerRunner  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("oesdevteam.run_features")


# ---------------------------------------------------------------------------
# Diff collection (matches the logic used by the integration-example
# FeatureController so behaviour here mirrors a real pipeline run)
# ---------------------------------------------------------------------------


async def _run_shell(cmd: str, *, cwd: Path) -> str:
    """Run ``cmd`` in ``cwd`` and return decoded stdout (empty on error)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace")


async def _collect_diff(namespace: Path) -> tuple[list[str], str]:
    """Collect ``git diff HEAD`` output, including untracked files.

    ``git add -N`` (intent-to-add) surfaces untracked files in
    ``git diff HEAD`` without actually staging their content. This is
    non-destructive and matches the FeatureController's behaviour.
    """
    # Initialise git if the namespace doesn't already have a repo.
    if not (namespace / ".git").exists():
        await _run_shell("git init -q", cwd=namespace)
        await _run_shell("git add -A && git commit -q --allow-empty -m checkpoint", cwd=namespace)
    await _run_shell("git add -N . 2>/dev/null", cwd=namespace)
    files_out = await _run_shell("git diff --name-only HEAD 2>/dev/null", cwd=namespace)
    files_changed = [ln for ln in files_out.splitlines() if ln.strip()]
    diff = await _run_shell("git diff HEAD 2>/dev/null", cwd=namespace)
    return files_changed, diff[:100_000]


# ---------------------------------------------------------------------------
# Gate orchestration
# ---------------------------------------------------------------------------


async def _review_feature(
    namespace: Path,
    feature: dict,
    *,
    only: str | None,
    dry_run: bool,
) -> None:
    """Run the applicable gates against one feature and print reports."""
    files_changed, diff = await _collect_diff(namespace)
    if not files_changed:
        print(f"[{feature['id']}] no diff against HEAD — nothing to review")
        return

    gate_input = GateInput(
        feature_id=feature["id"],
        feature_goal=feature.get("description") or feature.get("name", ""),
        files_changed=files_changed,
        diff=diff,
        verify_commands=list(feature.get("verify", [])),
        domain_context="",  # the full controller builds this via Opus enrichment
    )

    # 1. API Contract Gate — deterministic, free.
    if only in (None, "api-contract"):
        cfg = ApiContractConfig.load()
        gate = ApiContractGate(config=cfg)
        result = await gate.check(gate_input)
        print(render_api_contract_report(result))

    if dry_run:
        print(f"[{feature['id']}] dry-run complete — skipping LLM-backed gates")
        return

    runner = ClaudeCliReviewerRunner()

    # 2. Senior Reviewer squad — five Opus calls (~$1 per feature).
    if only in (None, "senior-review"):
        gate = CodeReviewGate(runner=runner)
        result = await gate.check(gate_input)
        print(render_code_review_report(result))

    # 3. SRE Review Gate — one Opus call, only on deploy-surface features.
    if only in (None, "sre-review"):
        cfg = SreReviewConfig.load()
        gate = SreReviewGate(runner=runner, config=cfg)
        result = await gate.check(gate_input)
        print(render_sre_review_report(result))


async def _main_async(namespace: Path, *, only: str | None, dry_run: bool) -> int:
    """Load the namespace's features.json and review every pending entry."""
    features_path = namespace / "features.json"
    if not features_path.exists():
        logger.error("no features.json at %s", features_path)
        return 2

    data = json.loads(features_path.read_text(encoding="utf-8"))
    pending = [f for f in data.get("features", []) if f.get("status") == "pending"]
    if not pending:
        logger.info("no pending features in %s — nothing to review", features_path)
        return 0

    for feature in pending:
        print(f"\n{'=' * 78}\nReviewing {feature['id']}: {feature.get('name', '')}\n{'=' * 78}")
        await _review_feature(namespace, feature, only=only, dry_run=dry_run)
    return 0


def main() -> int:
    """Parse args and hand off to the async entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "namespace",
        type=Path,
        help="Path to a namespace directory containing features.json",
    )
    parser.add_argument(
        "--only",
        choices=("api-contract", "senior-review", "sre-review"),
        default=None,
        help="Run a single gate instead of the full chain.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip LLM-backed gates; API contract gate still runs (free).",
    )
    args = parser.parse_args()

    if not args.namespace.is_dir():
        parser.error(f"{args.namespace} is not a directory")

    return asyncio.run(_main_async(args.namespace, only=args.only, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())

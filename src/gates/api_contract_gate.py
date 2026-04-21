"""API Contract Gate — keeps backend schema and generated TS types in sync.

Problem this gate solves
------------------------
One of the most-repeated failure modes in v1 of DevTeam was frontend and
backend silently disagreeing on a field name ("telegram_username" on one
side, "telegram_chat_id" on the other). Unit tests passed on each side in
isolation; the product broke only in integration. The root cause was
always the same: someone changed a Pydantic model or a route, shipped
without regenerating the OpenAPI dump, or regenerated the dump without
refreshing the TypeScript types — but the pipeline had no enforcement
either way.

What this gate checks
---------------------
Given the set of files changed by a feature, the gate sorts them into
three buckets:

  1. *Backend schema* — Pydantic models, API routes, etc.
  2. *OpenAPI artefact* — the JSON / YAML snapshot checked in to git.
  3. *Frontend types* — generated TypeScript consumed by the UI.

Two invariants must hold:

  - If (1) changed, (2) must also change. Otherwise the OpenAPI dump
    does not reflect the current backend contract.
  - If (2) changed, (3) must also change. Otherwise the frontend is
    typed against yesterday's contract.

Failures are retryable — the feature simply has to regenerate the
missing artefact. The gate returns a reason line instructing the
controller which artefact is stale.

Deliberate non-goals
--------------------
This gate does NOT compare the *contents* of OpenAPI to the contents of
TS types. A deep semantic diff between schemas is a separate problem
(and a far heavier dependency — it needs a real codegen tool). Requiring
the two artefacts to be co-committed is a cheap, deterministic proxy:
you cannot ship a schema change without *also* deciding (and committing)
what the other side of the contract looks like.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models import QualityGateType
from .base import GateInput, GateResult

# -----------------------------------------------------------------------------
# Configuration — file patterns per bucket
# -----------------------------------------------------------------------------

# Path to the default patterns file. A project may override with a
# namespace-local `api_contract.yaml`; see ``ApiContractConfig.load``.
_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "api_contract.yaml"
)


@dataclass(frozen=True)
class ApiContractConfig:
    """Glob patterns identifying each side of the API contract.

    Three lists of fnmatch-style patterns:
      - ``backend_schema_patterns`` — files whose change might move the
        contract (Pydantic models, routes).
      - ``openapi_patterns`` — the checked-in OpenAPI artefact.
      - ``types_patterns`` — generated frontend types.

    Instances are immutable so a loaded config cannot accidentally be
    mutated mid-pipeline.
    """

    backend_schema_patterns: tuple[str, ...]
    openapi_patterns: tuple[str, ...]
    types_patterns: tuple[str, ...]

    @staticmethod
    def load(path: Path | None = None) -> ApiContractConfig:
        """Load patterns from YAML. Defaults to the repo-root config file.

        Raises:
            FileNotFoundError: if ``path`` (or the default) is missing.
            ValueError: if the YAML is malformed or missing required keys.
        """
        cfg_path = path or _DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"API contract config not found: {cfg_path}")

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        try:
            # Each key maps to a dict with a ``patterns`` list. Enforce
            # the shape rather than silently tolerating typos — a missing
            # bucket is a config bug, not a valid minimal file.
            backend = tuple(raw["backend_schema"]["patterns"])
            openapi = tuple(raw["openapi_artefact"]["patterns"])
            types = tuple(raw["frontend_types"]["patterns"])
        except (KeyError, TypeError) as e:
            raise ValueError(f"Malformed API contract config at {cfg_path}: {e}") from e

        return ApiContractConfig(
            backend_schema_patterns=backend,
            openapi_patterns=openapi,
            types_patterns=types,
        )


def _match_any(path: str, patterns: tuple[str, ...]) -> bool:
    """True if ``path`` matches any fnmatch pattern.

    ``fnmatch`` does not treat ``**`` specially the way shells do, but
    our patterns deliberately compose: ``**/models.py`` matches files
    anywhere in the tree because fnmatch's ``*`` skips path separators
    via the two-star construct already present in the normal behaviour
    of ``fnmatch.fnmatchcase`` with POSIX paths. We normalise to forward
    slashes upstream so Windows-style paths behave identically.
    """
    # Normalise: repo-root-relative, forward-slash. This is defensive
    # because callers may accidentally pass absolute paths; we strip the
    # leading `/` so patterns like `**/schemas.py` still match.
    normalised = path.replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatchcase(normalised, p) for p in patterns)


# -----------------------------------------------------------------------------
# The gate
# -----------------------------------------------------------------------------


@dataclass
class ApiContractGate:
    """Gate enforcing that backend/OpenAPI/TS types move together.

    The configuration is loaded once at construction time. Tests
    typically build an instance with a custom ``ApiContractConfig`` to
    exercise specific patterns; production callers use ``from_default``
    and let the YAML decide.
    """

    config: ApiContractConfig
    gate_type: QualityGateType = field(
        default=QualityGateType.API_CONTRACT,
        init=False,
    )

    @classmethod
    def from_default(cls) -> ApiContractGate:
        """Build a gate from the repo-root ``config/api_contract.yaml``."""
        return cls(config=ApiContractConfig.load())

    async def check(self, gate_input: GateInput) -> GateResult:
        """Evaluate the contract invariants against the feature's diff.

        Applicability
        -------------
        If none of the changed files fall into any of the three buckets,
        the gate short-circuits to PASS with ``reason="not applicable"``.
        A docs-only PR does not need to regenerate OpenAPI.
        """
        changed = gate_input.files_changed or []

        schema_changed = [p for p in changed if _match_any(p, self.config.backend_schema_patterns)]
        openapi_changed = [p for p in changed if _match_any(p, self.config.openapi_patterns)]
        types_changed = [p for p in changed if _match_any(p, self.config.types_patterns)]

        # Short-circuit: the feature didn't touch the contract at all.
        # Treat as PASS — blocking docs-only PRs because they don't
        # regenerate OpenAPI would be a false positive.
        if not schema_changed and not openapi_changed and not types_changed:
            return GateResult(
                gate_type=self.gate_type,
                passed=True,
                reason="API contract gate not applicable: no schema/OpenAPI/types files touched.",
                details=_empty_details(),
                allow_retry=False,
            )

        violations: list[str] = []

        # Invariant 1: backend schema changed → OpenAPI dump must also change.
        if schema_changed and not openapi_changed:
            violations.append(
                "Backend schema files changed but the OpenAPI artefact was not "
                "regenerated. Run your OpenAPI export step and commit the result. "
                f"Changed schema files: {schema_changed}"
            )

        # Invariant 2: OpenAPI dump changed → generated TS types must also change.
        # This is deliberately unconditional: even a manual hand-edit of
        # openapi.json is a contract change the frontend must respect.
        if openapi_changed and not types_changed:
            violations.append(
                "OpenAPI artefact changed but the generated TypeScript types were "
                "not refreshed. Re-run `openapi-typescript` (or your generator) "
                "and commit the result. Changed OpenAPI files: "
                f"{openapi_changed}"
            )

        if not violations:
            summary = (
                f"API contract synchronised: {len(schema_changed)} schema file(s), "
                f"{len(openapi_changed)} OpenAPI artefact(s), "
                f"{len(types_changed)} TS types file(s)."
            )
            return GateResult(
                gate_type=self.gate_type,
                passed=True,
                reason=summary,
                details=_details(schema_changed, openapi_changed, types_changed, violations=[]),
            )

        return GateResult(
            gate_type=self.gate_type,
            passed=False,
            reason=f"API contract drift: {violations[0]}",
            details=_details(schema_changed, openapi_changed, types_changed, violations=violations),
            allow_retry=True,
        )


# -----------------------------------------------------------------------------
# Convenience function
# -----------------------------------------------------------------------------


async def run_api_contract_gate(
    gate_input: GateInput,
    *,
    config: ApiContractConfig | None = None,
) -> GateResult:
    """Single-call convenience around :class:`ApiContractGate`.

    ``config=None`` uses the repo-root YAML; tests override with a custom
    ``ApiContractConfig`` to keep the unit under test free of I/O.
    """
    gate = ApiContractGate(config=config or ApiContractConfig.load())
    return await gate.check(gate_input)


# -----------------------------------------------------------------------------
# Detail payload + report
# -----------------------------------------------------------------------------


def _empty_details() -> dict[str, Any]:
    return {
        "schema_changed": [],
        "openapi_changed": [],
        "types_changed": [],
        "violations": [],
    }


def _details(
    schema_changed: list[str],
    openapi_changed: list[str],
    types_changed: list[str],
    *,
    violations: list[str],
) -> dict[str, Any]:
    return {
        "schema_changed": schema_changed,
        "openapi_changed": openapi_changed,
        "types_changed": types_changed,
        "violations": violations,
    }


def render_api_contract_report(result: GateResult) -> str:
    """Render a Markdown summary of the gate outcome.

    Like :func:`src.gates.code_review_gate.render_code_review_report`,
    the output is plain ASCII-safe text (no emoji) so it plays nicely
    with CI logs and GitHub PR comments.
    """
    if result.gate_type != QualityGateType.API_CONTRACT:
        raise ValueError(
            "render_api_contract_report expects gate_type API_CONTRACT, "
            f"got {result.gate_type.value!r}"
        )

    head = "[PASS]" if result.passed else "[BLOCK]"
    lines: list[str] = [f"# API Contract Gate — {head}", "", f"_{result.reason}_", ""]

    details = result.details
    for label, key in (
        ("Backend schema files", "schema_changed"),
        ("OpenAPI artefact files", "openapi_changed"),
        ("Generated TS types files", "types_changed"),
    ):
        entries = details.get(key, [])
        if entries:
            lines += [f"## {label}", ""]
            lines += [f"- `{entry}`" for entry in entries]
            lines.append("")

    violations = details.get("violations", [])
    if violations:
        lines += ["## Violations", ""]
        for v in violations:
            lines += [f"- {v}", ""]

    return "\n".join(lines)

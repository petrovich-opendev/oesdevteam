"""Quality gates — named checks that a feature must pass before merging.

Each gate implements the ``Gate`` protocol: given a ``GateInput`` it returns
a ``GateResult`` with a boolean ``passed`` and a human-readable reason.
The ``FeatureController`` (Step 3+) runs every applicable gate in sequence
and stops at the first failure; ``needs_rework`` features carry the
failing gate's reason back into the next attempt's prompt.

Gates are deliberately narrow: one file per gate type. This repository
currently implements:

- :mod:`src.gates.code_review_gate` — five-reviewer Senior squad (Step 3).

Planned:

- ``api_contract_gate`` — OpenAPI ↔ TS types consistency (Step 4).
- ``sre_review_gate`` — DevOps/SRE deploy-readiness (Step 5).
- ``security_gate`` — Semgrep + Bandit wrapper (port from v1).

Tests live in ``tests/test_<gate>.py`` with one test class per gate.
"""

from __future__ import annotations

from .api_contract_gate import (
    ApiContractConfig,
    ApiContractGate,
    run_api_contract_gate,
)
from .base import Gate, GateInput, GateResult, format_gate_report
from .code_review_gate import CodeReviewGate, run_code_review_gate
from .sre_review_gate import SreReviewConfig, SreReviewGate, run_sre_review_gate

__all__ = [
    "ApiContractConfig",
    "ApiContractGate",
    "CodeReviewGate",
    "Gate",
    "GateInput",
    "GateResult",
    "SreReviewConfig",
    "SreReviewGate",
    "format_gate_report",
    "run_api_contract_gate",
    "run_code_review_gate",
    "run_sre_review_gate",
]

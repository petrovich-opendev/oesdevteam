"""Pre-review rules engine — deterministic checks before LLM senior review.

Entry points:
    - :func:`run_rules_engine` — execute enabled rules against a
      ``RuleContext``, return a ``RulesResult``.
    - :func:`load_rules_config` — read ``config/rules.yaml`` to decide
      which rules are enabled and with what severity.
    - :func:`render_rules_report` — Markdown rendering for pipeline-log.

See ``src/rules/engine.py`` for the data classes and dispatcher, and
``src/rules/checks.py`` for the individual rule implementations.
"""

from .engine import (
    DEFAULT_RULES,
    RuleContext,
    RuleFinding,
    RulesResult,
    load_rules_config,
    render_rules_report,
    run_rules_engine,
)

__all__ = [
    "DEFAULT_RULES",
    "RuleContext",
    "RuleFinding",
    "RulesResult",
    "load_rules_config",
    "render_rules_report",
    "run_rules_engine",
]

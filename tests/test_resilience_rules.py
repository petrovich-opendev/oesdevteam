"""Tests enforcing that every relevant reviewer prompt carries the
external-data-resilience checklist.

These tests do not run an LLM — they parse the shipped prompt files
and assert that rule references are present. The canonical rule
document is ``docs/RESILIENCE_RULES.md``; the reviewer prompts must
reference it explicitly so an engineer reading the prompt can find
the full rules without guessing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models import AgentRole
from src.reviewers.squad import load_reviewer_prompt

# -----------------------------------------------------------------------------
# Canonical document exists
# -----------------------------------------------------------------------------

_DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "RESILIENCE_RULES.md"


class TestRulesDocumentIsShipped:
    def test_document_exists(self):
        assert _DOC_PATH.exists(), (
            "docs/RESILIENCE_RULES.md must ship with the repo — it is the "
            "source of truth referenced by multiple reviewer prompts."
        )

    def test_document_covers_all_twelve_rules(self):
        """Every R-1 … R-12 identifier must appear in the rules document."""
        text = _DOC_PATH.read_text(encoding="utf-8")
        for i in range(1, 13):
            assert f"R-{i}" in text, f"Rule R-{i} is missing from the document"

    def test_document_has_summary_table(self):
        text = _DOC_PATH.read_text(encoding="utf-8")
        assert "Summary table" in text


# -----------------------------------------------------------------------------
# Reviewer prompts reference the rules
# -----------------------------------------------------------------------------

_RESILIENCE_REVIEWERS = (
    AgentRole.SENIOR_BACKEND,
    AgentRole.SENIOR_DATA,
    AgentRole.SENIOR_SRE,
)


class TestReviewerPromptsReferenceRules:
    @pytest.mark.parametrize("role", _RESILIENCE_REVIEWERS, ids=lambda r: r.value)
    def test_prompt_links_to_canonical_doc(self, role):
        prompt = load_reviewer_prompt(role, domain_context="test")
        assert "docs/RESILIENCE_RULES.md" in prompt, (
            f"{role.value} prompt must reference the canonical rules doc "
            "so reviewers can look up the full rule set."
        )

    @pytest.mark.parametrize("role", _RESILIENCE_REVIEWERS, ids=lambda r: r.value)
    def test_prompt_mentions_at_least_three_rule_ids(self, role):
        """Each reviewer must cite specific rule IDs, not just link."""
        prompt = load_reviewer_prompt(role, domain_context="test")
        cited = sum(1 for i in range(1, 13) if f"R-{i}" in prompt)
        assert cited >= 3, (
            f"{role.value} prompt cites only {cited} rule IDs — "
            "should cite at least 3 concrete rules so reviewers "
            "apply the checklist rather than paraphrasing."
        )


# -----------------------------------------------------------------------------
# Severity calibration surfaces in prompts
# -----------------------------------------------------------------------------


class TestPromptsCarrySeverity:
    """Every resilience-relevant reviewer must tag severity explicitly.

    A reviewer that mentions "you should validate input" without saying
    "missing = BLOCKER" leaves too much room for a lenient verdict.
    """

    @pytest.mark.parametrize("role", _RESILIENCE_REVIEWERS, ids=lambda r: r.value)
    def test_prompt_mentions_blocker_and_major(self, role):
        prompt = load_reviewer_prompt(role, domain_context="test")
        assert "BLOCKER" in prompt
        assert "MAJOR" in prompt


# -----------------------------------------------------------------------------
# CLAUDE.md picks up the hard rule
# -----------------------------------------------------------------------------

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


class TestClaudeMdCarriesHardRule:
    def test_external_data_resilience_section_present(self):
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        assert "External data resilience" in text
        assert "docs/RESILIENCE_RULES.md" in text

    def test_hardcoded_tag_ban_present(self):
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        assert "Do NOT hardcode external vendor tag names" in text


# -----------------------------------------------------------------------------
# Business-goal alignment
# -----------------------------------------------------------------------------


class TestBusinessGoalAlignment:
    """Resilience rules protect the "production code without manual
    intervention" promise from the reality of industrial data sources.

    Without these rules, a feature that happily generates a Kafka
    consumer passes every code-level review yet crashes in production
    the first time a vendor renames a tag. The rules make that class
    of failure a BLOCKER the reviewer will catch before the feature
    lands.
    """

    def test_rules_are_binding_not_advisory(self):
        """Rules document must use directive language, not hedging."""
        text = _DOC_PATH.read_text(encoding="utf-8")
        # MUST / MUST NOT language is the marker of a binding rule.
        assert text.count("MUST") >= 3, (
            "Rules document should use MUST / MUST NOT to make rules binding"
        )

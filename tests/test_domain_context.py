"""Tests for the adaptive domain-context loader.

The Opus enrichment path is exercised via an injected ``enricher``
callable — the tests never spawn a real Claude CLI subprocess. The
three-tier priority (explicit → cache → enrichment) and cache
invalidation are the core behaviours under test.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.controller.domain_context import (
    CACHE_FILENAME,
    build_domain_context,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Write content, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _never_enricher(signals):
    """Enricher that must never run — calling raises a test-visible error."""
    raise AssertionError("enricher was called but should not have been")


def _canned_enricher(canned: str):
    """Factory for a deterministic enricher that records each call."""

    calls: list[dict[str, str]] = []

    async def _run(signals):
        calls.append(dict(signals))
        return canned

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ---------------------------------------------------------------------------
# Tier 1 — explicit DOMAIN.md
# ---------------------------------------------------------------------------


class TestExplicitDomainMd:
    async def test_explicit_wins_over_enrichment(self, tmp_path):
        _write(tmp_path / "DOMAIN.md", "# Industry\nMining.\n")
        # Intentionally also drop README/features.json — explicit must still win.
        _write(tmp_path / "README.md", "Something else")
        _write(
            tmp_path / "features.json",
            json.dumps({"meta": {"purpose": "retail"}, "features": []}),
        )

        brief = await build_domain_context(tmp_path, enricher=_never_enricher)
        assert "Mining." in brief
        assert "retail" not in brief  # enrichment never ran

    async def test_empty_domain_md_falls_through_to_enrichment(self, tmp_path):
        """An empty DOMAIN.md must NOT mask the enrichment path."""
        _write(tmp_path / "DOMAIN.md", "")
        _write(tmp_path / "README.md", "mining fleet telemetry pipeline")
        enricher = _canned_enricher("# Industry\nMining.")

        brief = await build_domain_context(tmp_path, enricher=enricher)
        assert "Mining" in brief
        assert len(enricher.calls) == 1  # enrichment was invoked


# ---------------------------------------------------------------------------
# Signal collection
# ---------------------------------------------------------------------------


class TestSignalCollection:
    async def test_features_json_meta_purpose_reaches_enricher(self, tmp_path):
        _write(tmp_path / "README.md", "See features.json.")
        _write(
            tmp_path / "features.json",
            json.dumps(
                {
                    "meta": {"purpose": "industrial mining dispatch"},
                    "features": [
                        {
                            "id": "F1",
                            "name": "Shift report",
                            "description": "Generate PDF",
                        }
                    ],
                }
            ),
        )
        enricher = _canned_enricher("# Industry\nMining dispatch.")

        brief = await build_domain_context(tmp_path, enricher=enricher)
        assert "Mining dispatch" in brief

        # The enricher received both the README and the purpose signal.
        signals = enricher.calls[0]
        assert "README.md" in signals
        assert "features.meta.purpose" in signals
        assert "industrial mining dispatch" in signals["features.meta.purpose"]

    async def test_glossary_picked_up(self, tmp_path):
        _write(tmp_path / "glossary.md", "truck park: fleet of mining trucks")
        enricher = _canned_enricher("# Industry\nMining.")

        brief = await build_domain_context(tmp_path, enricher=enricher)
        assert "Mining" in brief
        assert "glossary.md" in enricher.calls[0]

    async def test_no_signals_no_enrichment(self, tmp_path):
        # Empty namespace — nothing to reason about.
        brief = await build_domain_context(tmp_path, enricher=_never_enricher)
        assert brief == ""


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    async def test_second_call_uses_cache(self, tmp_path):
        _write(tmp_path / "README.md", "mining fleet operations")
        enricher = _canned_enricher("# Industry\nMining.")

        brief_1 = await build_domain_context(tmp_path, enricher=enricher)
        brief_2 = await build_domain_context(tmp_path, enricher=enricher)

        assert brief_1 == brief_2
        assert len(enricher.calls) == 1, "enrichment should run exactly once"

    async def test_changed_signal_invalidates_cache(self, tmp_path):
        _write(tmp_path / "README.md", "mining fleet operations")
        enricher = _canned_enricher("# Industry\nMining.")

        await build_domain_context(tmp_path, enricher=enricher)
        # Change the signal — hash must diverge → re-enrichment.
        _write(tmp_path / "README.md", "retail health coaching platform")
        enricher_2 = _canned_enricher("# Industry\nHealth.")

        brief_2 = await build_domain_context(tmp_path, enricher=enricher_2)
        assert "Health" in brief_2
        assert len(enricher_2.calls) == 1

    async def test_force_refresh_bypasses_cache(self, tmp_path):
        _write(tmp_path / "README.md", "mining fleet operations")
        enricher = _canned_enricher("# Industry\nMining.")

        await build_domain_context(tmp_path, enricher=enricher)
        await build_domain_context(
            tmp_path,
            enricher=enricher,
            force_refresh=True,
        )

        assert len(enricher.calls) == 2

    async def test_cache_file_is_human_readable(self, tmp_path):
        """Cache starts with a comment header and stores brief as Markdown."""
        _write(tmp_path / "README.md", "mining fleet operations")
        enricher = _canned_enricher("# Industry\nMining.")

        await build_domain_context(tmp_path, enricher=enricher)
        cache = (tmp_path / CACHE_FILENAME).read_text(encoding="utf-8")

        assert cache.startswith("<!--oesdevteam:domain-context")
        assert "Mining" in cache

    async def test_corrupted_cache_triggers_regeneration(self, tmp_path):
        _write(tmp_path / "README.md", "mining fleet operations")
        # Place a malformed cache file that does not start with our header.
        _write(tmp_path / CACHE_FILENAME, "not a valid cache at all")
        enricher = _canned_enricher("# Industry\nMining.")

        brief = await build_domain_context(tmp_path, enricher=enricher)
        assert "Mining" in brief
        assert len(enricher.calls) == 1


# ---------------------------------------------------------------------------
# Env-var bypass and error paths
# ---------------------------------------------------------------------------


class TestEnvBypass:
    async def test_disabled_env_skips_opus_returns_fallback(self, tmp_path, monkeypatch):
        _write(tmp_path / "README.md", "industrial mining telemetry")
        monkeypatch.setenv("OESDEVTEAM_DOMAIN_BRIEF_DISABLED", "1")

        brief = await build_domain_context(tmp_path, enricher=_never_enricher)
        # Fallback must include the raw signal — otherwise the reviewer
        # has nothing to work with.
        assert "industrial mining telemetry" in brief
        assert "raw signals" in brief.lower()

    async def test_enricher_exception_falls_back_to_signals(self, tmp_path):
        _write(tmp_path / "README.md", "industrial mining telemetry")

        async def _broken(signals):
            raise RuntimeError("CLI exploded")

        brief = await build_domain_context(tmp_path, enricher=_broken)
        # Must not raise; must return a non-empty fallback.
        assert "industrial mining telemetry" in brief

    async def test_enricher_empty_output_falls_back_to_signals(self, tmp_path):
        _write(tmp_path / "README.md", "industrial mining telemetry")
        enricher = _canned_enricher("   ")  # whitespace only

        brief = await build_domain_context(tmp_path, enricher=enricher)
        assert "industrial mining telemetry" in brief


# ---------------------------------------------------------------------------
# Business-goal alignment
# ---------------------------------------------------------------------------


class TestBusinessGoalAlignment:
    """Domain framing is the single most impactful input to the Business
    Expert reviewer; the loader must never silently produce noise.
    """

    async def test_mining_and_health_namespaces_get_different_briefs(self, tmp_path):
        """Same loader on two namespaces returns two differentiated briefs."""
        mining = tmp_path / "mining"
        health = tmp_path / "health"
        _write(mining / "README.md", "open-pit mining, trucks and shovels")
        _write(health / "README.md", "health coaching app for retail users")

        mining_enricher = _canned_enricher("# Industry\nMining.")
        health_enricher = _canned_enricher("# Industry\nHealth.")

        mining_brief = await build_domain_context(mining, enricher=mining_enricher)
        health_brief = await build_domain_context(health, enricher=health_enricher)

        assert "Mining" in mining_brief
        assert "Health" in health_brief
        assert mining_brief != health_brief

    async def test_brief_is_bounded_in_size(self, tmp_path):
        """A runaway enricher must not feed an enormous string into reviews."""
        _write(tmp_path / "README.md", "mining")
        giant = "A" * 50_000
        enricher = _canned_enricher(giant)

        brief = await build_domain_context(tmp_path, enricher=enricher)
        # Trimmed to _BRIEF_MAX_CHARS (4 KB).
        assert len(brief) <= 4_100

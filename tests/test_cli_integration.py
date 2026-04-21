"""Integration tests against the real Claude Code CLI (``claude --help``).

Why this file exists
--------------------
Unit tests in ``test_model_routing.py`` check that the argv we build contains
``--model``, ``--allowed-tools``, and ``--max-budget-usd``. That is necessary
but not sufficient: if a future CLI version renames these flags, the unit
tests still pass while production silently breaks.

These integration tests execute ``claude --help`` and grep the output for
each flag we rely on. No network and no API key are required — ``--help``
is offline and free.

If the ``claude`` binary is not installed, the whole module is skipped. A
non-developer check-out of this repo should still be able to run the unit
suite without having Claude Code installed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

# Skip the whole module if Claude CLI is unavailable. The unit tests cover
# argv construction in isolation, so skipping here does not reduce coverage
# of Step 1's core objective — only the environmental compatibility check.
if shutil.which("claude") is None:
    pytest.skip(
        "Claude Code CLI not installed — skipping integration checks",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def help_output() -> str:
    """Run ``claude --help`` once per module, cache the result."""
    # `--help` does not hit the network or require auth. A short timeout
    # guards against the (extremely unlikely) case of `claude --help`
    # hanging on an unrelated bug.
    proc = subprocess.run(
        ["claude", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,  # some CLI versions exit 0, others exit 2 from --help
    )
    # Claude CLI emits help on stdout; if it's empty, fall back to stderr.
    return (proc.stdout or proc.stderr).lower()


class TestClaudeCliFlagsAreSupported:
    """Each flag we wire into argv MUST appear in the real CLI's help text.

    This is a contract test: if the upstream CLI renames a flag, these tests
    turn red in CI instead of us discovering the breakage in production.
    """

    def test_model_flag_exists(self, help_output: str):
        assert "--model" in help_output, (
            "The installed Claude CLI does not advertise --model. "
            "Step 1's model pinning cannot function without this flag."
        )

    def test_max_budget_usd_flag_exists(self, help_output: str):
        assert "--max-budget-usd" in help_output, (
            "Missing --max-budget-usd. The per-call dollar ceiling in "
            "config/models.yaml would then be unenforceable."
        )

    def test_allowed_tools_flag_exists(self, help_output: str):
        # Claude CLI supports both canonical `--allowed-tools` and legacy
        # `--allowedTools`; we assert the canonical form to catch a future
        # deprecation early.
        assert "--allowed-tools" in help_output, (
            "The canonical --allowed-tools flag is missing. Investigate "
            "before downgrading to --allowedTools."
        )

    def test_print_mode_exists(self, help_output: str):
        """--print (and short -p) are the non-interactive mode Step 1 relies on."""
        assert "--print" in help_output
        assert "-p," in help_output or "-p " in help_output

    def test_system_prompt_flag_exists(self, help_output: str):
        assert "--system-prompt" in help_output

    def test_output_format_json_supported(self, help_output: str):
        """JSON output is how ClaudeBridge parses cost and status."""
        assert "--output-format" in help_output
        assert "json" in help_output

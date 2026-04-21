"""Runner abstraction — what invokes the LLM reviewer.

A ``ReviewerRunner`` takes ``(role, system_prompt, task)`` and returns raw
text. This lets the tests swap in a deterministic mock instead of
spending real dollars on Claude calls, and lets the future Step 6
replace the real runner with a Langfuse-instrumented version without
touching squad orchestration.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Protocol

from ..claude_bridge import build_claude_cli_command
from ..models import AgentRole


class ReviewerRunner(Protocol):
    """Minimal interface every reviewer execution backend must satisfy."""

    async def run(self, *, role: AgentRole, system_prompt: str, task: str) -> str:
        """Invoke a reviewer LLM; return raw stdout text.

        Implementations MUST NOT hide errors: raise on subprocess failure,
        on authentication failure, on anything unexpected. The caller
        ( ``squad._run_one_reviewer`` ) catches exceptions and converts
        them into structured ``reviewer_fault`` findings.
        """
        ...


# -----------------------------------------------------------------------------
# Real implementation — shells out through the Claude Code CLI
# -----------------------------------------------------------------------------


class ClaudeCliReviewerRunner:
    """Runs reviewers via ``claude -p`` using ``build_claude_cli_command``.

    One instance can be reused across many reviewer calls. Per-call state
    (timeouts, working directory) is passed to ``run``; cross-call state
    (future telemetry, rate limiting) lives here.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = 300,
        work_dir: str | None = None,
        claude_bin: str | None = None,
    ):
        """Configure a reusable runner.

        Args:
            timeout_seconds: Per-reviewer wall-time ceiling. 300s = 5 min is
                our current budget: a reviewer that cannot produce a
                structured JSON verdict in that window is probably stuck in
                a tool-permission loop — escalate rather than wait longer.
            work_dir: Working directory for the subprocess; defaults to the
                caller's CWD.
            claude_bin: Absolute path to the CLI binary; when None the
                bridge resolves it from env / PATH at call time.
        """
        self.timeout_seconds = timeout_seconds
        self.work_dir = work_dir or os.getcwd()
        self.claude_bin = claude_bin

    async def run(self, *, role: AgentRole, system_prompt: str, task: str) -> str:
        """Launch Claude CLI for one reviewer; return its stdout text."""
        cmd = build_claude_cli_command(
            role=role,
            task=task,
            system_prompt=system_prompt,
            settings_path=None,  # reviewer runs with no custom hooks
            claude_bin=self.claude_bin,
        )

        # Claude -p writes its JSON result to stdout. stderr is captured
        # separately for diagnostic logging; a non-zero exit code with
        # empty stdout is surfaced to the caller as a RuntimeError.
        process = await asyncio.create_subprocess_exec(
            *cmd.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.work_dir,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            # Terminate (SIGTERM first, SIGKILL after grace period) to
            # avoid an orphan subprocess holding a license slot.
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), timeout=5)
            except TimeoutError:
                process.kill()
            raise RuntimeError(
                f"Reviewer {role.value!r} timed out after {self.timeout_seconds}s"
            ) from None

        stdout = stdout_b.decode(errors="replace")
        if process.returncode != 0 and not stdout.strip():
            stderr = stderr_b.decode(errors="replace")[:1000]
            raise RuntimeError(
                f"Reviewer {role.value!r} exited {process.returncode} "
                f"with empty stdout. stderr: {stderr}"
            )

        return stdout


# -----------------------------------------------------------------------------
# Test implementation — deterministic canned responses
# -----------------------------------------------------------------------------


class MockReviewerRunner:
    """Canned-response runner for tests.

    Construct with a mapping of ``AgentRole`` → raw response text. The
    ``run`` method simply looks up the role and returns the canned
    string. Missing roles raise ``KeyError`` so a test that forgets to
    wire a reviewer fails loudly instead of silently approving.
    """

    def __init__(self, responses: dict[AgentRole, str]):
        """Store canned responses keyed by role and an empty call log."""
        self.responses = dict(responses)
        # Record calls in order so tests can assert how the squad
        # orchestrated the run (e.g. "all five ran in parallel, not
        # sequentially" — they check the recorded times).
        self.calls: list[tuple[AgentRole, str, str]] = []

    async def run(self, *, role: AgentRole, system_prompt: str, task: str) -> str:
        """Return the canned response for a role and log the call."""
        self.calls.append((role, system_prompt, task))
        if role not in self.responses:
            raise KeyError(f"MockReviewerRunner has no canned response for {role.value!r}")
        return self.responses[role]


# -----------------------------------------------------------------------------
# Utility — cheap way to capture diff to a temp file if callers want stdin
# -----------------------------------------------------------------------------


def spool_to_temp(text: str, suffix: str = ".txt") -> str:
    """Write ``text`` to a temp file and return its path.

    Some future integrations (e.g. ``--system-prompt-file`` mode) need a
    file rather than a string. Keeping this helper next to the runner
    avoids duplicating tempfile boilerplate across call sites.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path

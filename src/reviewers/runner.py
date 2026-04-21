"""Runner abstraction — what invokes the LLM reviewer.

A ``ReviewerRunner`` takes ``(role, system_prompt, task)`` and returns raw
text. This lets the tests swap in a deterministic mock instead of
spending real dollars on Claude calls, and lets the future Step 6
replace the real runner with a Langfuse-instrumented version without
touching squad orchestration.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Protocol

from ..claude_bridge import build_claude_cli_command
from ..models import AgentRole


def _unwrap_claude_cli_envelope(stdout: str) -> tuple[str, float]:
    """Strip the ``claude -p --output-format json`` metadata envelope.

    Claude Code CLI with ``--output-format json`` returns a wrapper object:
    ``{"type": "result", "subtype": "success", "result": "<text>",
    "cost_usd": 0.12, "duration_ms": 45000, ...}``. The reviewer's actual
    output lives in ``result``; ``cost_usd`` is the authoritative per-call
    cost our cost tracker should record.

    Returns ``(inner_text, cost_usd)``. If ``stdout`` is not an envelope
    (e.g. text mode, or a non-JSON error dump), returns ``(stdout, 0.0)``
    unchanged — the parser downstream still tolerates raw JSON or prose.
    """
    if not stdout.strip():
        return stdout, 0.0
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, 0.0
    if isinstance(envelope, dict) and envelope.get("type") == "result" and "result" in envelope:
        inner = envelope.get("result", "") or ""
        cost = float(envelope.get("cost_usd", 0.0) or 0.0)
        return inner, cost
    return stdout, 0.0


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
        # Cost of the most recent call, unwrapped from the Claude CLI
        # envelope. Read after ``run`` returns. 0.0 when unavailable.
        self.last_call_cost_usd: float = 0.0
        # Aggregate cost across this runner instance's lifetime.
        # Per-gate orchestrators sum per-call entries from call_log.
        self.total_cost_usd: float = 0.0
        # Per-call ledger: list of (role.value, cost_usd). Cleared by
        # reset_cost_log() between unrelated gate invocations.
        self.call_log: list[tuple[str, float]] = []

    def reset_cost_log(self) -> None:
        """Zero the aggregate cost and per-call ledger."""
        self.last_call_cost_usd = 0.0
        self.total_cost_usd = 0.0
        self.call_log = []

    async def run(self, *, role: AgentRole, system_prompt: str, task: str) -> str:
        """Launch Claude CLI for one reviewer; return the reviewer's JSON text.

        Strips the ``claude -p --output-format json`` metadata envelope
        before returning — downstream parsers see the reviewer's own JSON
        exactly as the reviewer was asked to produce it.
        """
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

        inner, cost = _unwrap_claude_cli_envelope(stdout)
        self.last_call_cost_usd = cost
        self.total_cost_usd += cost
        self.call_log.append((role.value, cost))
        return inner


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

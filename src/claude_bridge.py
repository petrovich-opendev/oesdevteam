"""Claude Code CLI bridge — runs an agent as a subprocess with an explicit model.

This module is the *only* place that shells out to ``claude -p``. Every other
part of the pipeline reaches Claude through here, so:

1. Model selection lives in one function (``build_claude_cli_command``).
2. Cost/token accounting happens once.
3. Tests can mock a single boundary.

Design note — v1 → v2 change
----------------------------
DevTeam v1 built its ``claude -p`` command without ``--model``, inheriting the
user's global CLI default. That coupled pipeline output to machine-local state
and broke reproducibility. v2 always passes ``--model <name>`` resolved from
``config/models.yaml`` via ``src.config``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any

from .config import ModelSpec, get_model_spec_for_role
from .models import AgentRole

# -----------------------------------------------------------------------------
# CLI location
# -----------------------------------------------------------------------------


def resolve_claude_executable() -> str:
    """Locate the Claude Code CLI binary.

    Lookup order:
      1. ``OESDEVTEAM_CLAUDE_BIN`` — explicit override (primary v2 knob).
      2. ``CLAUDE_CODE_BIN`` — DevTeam v1 variable, kept for migration.
      3. ``shutil.which('claude')`` — PATH lookup.

    Why two env vars: non-interactive SSH sessions often have a minimal PATH
    that does NOT include the user's interactive Claude install. Requiring an
    explicit absolute path via env is the cleanest fix — the pipeline should
    refuse to start rather than silently pick up a different binary.

    Raises:
        FileNotFoundError: if no usable ``claude`` binary is found.
    """
    for env_key in ("OESDEVTEAM_CLAUDE_BIN", "CLAUDE_CODE_BIN"):
        candidate = os.environ.get(env_key, "").strip()
        if candidate:
            if not os.path.exists(candidate):
                raise FileNotFoundError(
                    f"{env_key}={candidate!r} does not exist. "
                    "Set it to the absolute path of the `claude` binary "
                    "(not a shell alias)."
                )
            if not os.access(candidate, os.X_OK):
                # A non-executable path (e.g. chmod 644) will blow up deep
                # inside asyncio.create_subprocess_exec with a cryptic error.
                # Fail here with a clear, actionable message instead.
                raise FileNotFoundError(
                    f"{env_key}={candidate!r} is not executable. "
                    "Run `chmod +x` on it or point the variable at a different binary."
                )
            return candidate

    on_path = shutil.which("claude")
    if on_path:
        return on_path

    raise FileNotFoundError(
        "Claude Code CLI not found. Set OESDEVTEAM_CLAUDE_BIN to the absolute "
        "path of the `claude` binary, or ensure it is on PATH."
    )


# -----------------------------------------------------------------------------
# Command building
# -----------------------------------------------------------------------------

# Tools we allow by default. Restrictive-by-default: an agent that needs
# something exotic (e.g. MCP tools) must pass ``allowed_tools`` explicitly.
# Adding tools silently was a source of drift in v1 — see lessons_learned.md.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
)


@dataclass(frozen=True)
class ClaudeCliCommand:
    """Fully-assembled invocation of ``claude -p`` for one agent run.

    Frozen so that once the controller has decided how to call Claude, the
    decision is auditable — it's what goes into the JSONL run log verbatim.

    Note on ``max_cost_usd``:
    The CLI enforces this via the ``--max-budget-usd`` flag, so the field is
    not merely informational — it is an actual ceiling honoured by Claude
    Code itself. See ``config/models.yaml`` for the per-role defaults.
    """

    argv: list[str]
    model: str
    max_cost_usd: float
    role: AgentRole
    rationale: str

    def trace(self) -> dict[str, Any]:
        """Return a JSON-safe dict for logging or NATS publication.

        Positional arguments to ``claude -p`` (the task prompt itself, the
        system prompt, the tools list) may contain sensitive business content
        and must NOT appear in logs. Only flag *names* are kept — this gives
        operators enough to reconstruct what the call looked like without
        leaking payload.
        """
        # Extract only the long/short flag names. Any argv element that starts
        # with "-" and looks like an option is safe to surface; the value that
        # follows each flag is deliberately dropped.
        safe_flags = [arg for arg in self.argv if arg.startswith("--") or arg == "-p"]
        return {
            "role": self.role.value,
            "model": self.model,
            "max_cost_usd": self.max_cost_usd,
            "rationale": self.rationale,
            "flags_used": safe_flags,
        }


def build_claude_cli_command(
    *,
    role: AgentRole,
    task: str,
    system_prompt: str,
    allowed_tools: tuple[str, ...] | list[str] | None = None,
    settings_path: str | None = None,
    model_spec: ModelSpec | None = None,
    claude_bin: str | None = None,
) -> ClaudeCliCommand:
    """Build the ``claude -p`` argv for an agent invocation.

    Why a dedicated builder function: the wire format of ``claude -p`` has
    changed across CLI versions, and keeping one source of truth for the argv
    layout makes upgrades a one-file change.

    Args:
        role: Agent role — drives model selection (via config/models.yaml).
        task: Natural-language task description, passed as ``claude -p <task>``.
        system_prompt: Role/stage prompt, sent via ``--system-prompt``.
        allowed_tools: Tools the agent may invoke. Defaults to
            ``DEFAULT_ALLOWED_TOOLS``.
        settings_path: Optional ``--settings`` file (hooks, permission modes).
        model_spec: Pre-resolved model selection. Pass None to resolve from
            config. Tests pass a mock spec here.
        claude_bin: Path to the CLI binary. Defaults to
            ``resolve_claude_executable()``.

    Returns:
        A ``ClaudeCliCommand`` with the fully-assembled ``argv`` list ready
        for ``asyncio.create_subprocess_exec(*cmd.argv, ...)``.
    """
    if model_spec is None:
        model_spec = get_model_spec_for_role(role)

    binary = claude_bin if claude_bin is not None else resolve_claude_executable()
    tools = tuple(allowed_tools) if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS

    # Argv order mirrors the Claude CLI's documented `claude --help`. Kebab-case
    # flag forms are used (--allowed-tools, --max-budget-usd) because they are
    # the canonical long-form names in Claude Code's public docs; camelCase
    # variants exist as aliases today but are less stable across versions.
    argv: list[str] = [
        binary,
        "-p",
        task,
        "--print",  # explicit non-interactive
        "--output-format",
        "json",
        "--model",
        model_spec.model,  # v2: EXPLICIT MODEL PIN
        "--system-prompt",
        system_prompt,
        "--allowed-tools",
        ",".join(tools),
        "--max-budget-usd",
        f"{model_spec.max_cost_usd:.4f}",  # hard dollar cap
    ]

    if settings_path:
        argv.extend(["--settings", settings_path])

    return ClaudeCliCommand(
        argv=argv,
        model=model_spec.model,
        max_cost_usd=model_spec.max_cost_usd,
        role=role,
        rationale=model_spec.rationale,
    )

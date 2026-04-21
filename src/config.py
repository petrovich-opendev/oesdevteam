"""Configuration loader for OESDevTeam.

Everything that could change at deploy time — model names, token ceilings,
timeouts — lives in YAML under ``config/`` and is loaded through this module.
Source code never hardcodes those values.

Currently loads:
  - ``config/models.yaml`` — per-role LLM routing (see ``get_model_for_role``)

Environment overrides
---------------------
Any entry in ``models.yaml`` can be overridden without touching the file:

    OESDEVTEAM_MODEL_DEVELOPER=claude-sonnet-4-6

rebinds the Developer role to Sonnet for a single run. Useful for cost-cutting
dry runs or A/B testing a new model version.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from .models import AgentRole

# -----------------------------------------------------------------------------
# Internal Pydantic schema for config/models.yaml
# -----------------------------------------------------------------------------


class _RoleEntry(BaseModel):
    """One entry under `roles:` or `profiles:` in models.yaml.

    ``max_cost_usd`` is the hard dollar ceiling for a single agent invocation.
    It is passed straight to the CLI via ``--max-budget-usd`` — the CLI
    aborts the session if the spend would exceed it. Units are USD, cents
    precision is fine for audit purposes.
    """

    model: str = Field(min_length=1)
    max_cost_usd: float = Field(gt=0, le=100.0)
    rationale: str = Field(min_length=1)


class _ModelsFile(BaseModel):
    """Top-level schema of models.yaml."""

    version: int
    roles: dict[str, _RoleEntry]
    profiles: dict[str, _RoleEntry] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Public data carrier — what the rest of the codebase sees
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """Resolved model selection for a given role or profile.

    Immutable on purpose: once the pipeline decides which model to call, that
    decision is logged into the Event envelope and must not be mutated.

    ``max_cost_usd`` here is the per-invocation ceiling, enforced by the CLI
    via ``--max-budget-usd``. This is distinct from
    ``src.models.TaskBudget.max_cost_usd``, which is the aggregate ceiling
    for an entire task (potentially many invocations). Keep both in mind
    when tuning: a generous per-call cap plus a tight task budget is
    usually what you want.
    """

    role_or_profile: str
    model: str
    max_cost_usd: float
    rationale: str


# -----------------------------------------------------------------------------
# Loader
# -----------------------------------------------------------------------------


def _default_config_path() -> Path:
    """Return the default location of ``models.yaml``.

    The env var ``OESDEVTEAM_MODELS_CONFIG`` lets tests (and exotic deploys)
    point at a different file without polluting the real config directory.
    """
    override = os.environ.get("OESDEVTEAM_MODELS_CONFIG")
    if override:
        return Path(override)
    # Project-relative path. config/ is a sibling of src/.
    return Path(__file__).resolve().parent.parent / "config" / "models.yaml"


@lru_cache(maxsize=1)
def _load_models_file() -> _ModelsFile:
    """Parse ``models.yaml`` and validate it against the schema.

    Cached because the file does not change at runtime. Tests can clear the
    cache via ``_load_models_file.cache_clear()``.

    Raises:
        FileNotFoundError: if the YAML file is missing.
        ValueError: if YAML is malformed.
        pydantic.ValidationError: if the schema is violated.
        ValueError: if role keys don't match the ``AgentRole`` enum.
    """
    path = _default_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"OESDevTeam models config not found at {path}. "
            "Did you forget to copy config/models.yaml, "
            "or set OESDEVTEAM_MODELS_CONFIG?"
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    try:
        parsed = _ModelsFile.model_validate(raw)
    except ValidationError as e:
        # Rewrap to surface the offending file path in the error message —
        # crucial for CI logs where the underlying pydantic message alone
        # leaves engineers hunting for the right file.
        raise ValueError(f"Invalid config at {path}: {e}") from e

    # Cross-check: every key under `roles:` must map to a real AgentRole value.
    # This catches typos at startup rather than at agent-launch time (when
    # the pipeline is halfway through a feature and recovery is expensive).
    known_roles = {r.value for r in AgentRole}
    unknown_in_config = set(parsed.roles.keys()) - known_roles
    if unknown_in_config:
        raise ValueError(
            f"models.yaml defines unknown role(s): {sorted(unknown_in_config)}. "
            f"Valid AgentRole values: {sorted(known_roles)}"
        )

    missing_in_config = known_roles - set(parsed.roles.keys())
    if missing_in_config:
        raise ValueError(
            f"models.yaml is missing required roles: {sorted(missing_in_config)}. "
            "Every AgentRole must have a model mapping — otherwise the "
            "pipeline cannot decide what model to use if that role is invoked."
        )

    return parsed


# -----------------------------------------------------------------------------
# Public accessors
# -----------------------------------------------------------------------------


def get_model_spec_for_role(role: AgentRole | str) -> ModelSpec:
    """Resolve the model selection for an agent role.

    Order of precedence:
      1. Env var ``OESDEVTEAM_MODEL_<ROLE_UPPER>`` — wins unconditionally.
      2. Entry under ``roles:`` in ``models.yaml``.

    Args:
        role: Either an ``AgentRole`` enum value or its string key.

    Returns:
        The resolved ``ModelSpec``.

    Raises:
        KeyError: if ``role`` is not a valid AgentRole.
    """
    role_key = role.value if isinstance(role, AgentRole) else str(role)

    # Fail fast on unknown roles. This is better than silently falling back
    # to a default because a typo in a NATS event would then route to the
    # wrong model and produce mysteriously bad output.
    if role_key not in {r.value for r in AgentRole}:
        raise KeyError(f"Unknown AgentRole: {role_key!r}")

    # Env override — trimmed, ignored if empty string
    env_key = f"OESDEVTEAM_MODEL_{role_key.upper()}"
    env_model = os.environ.get(env_key, "").strip()
    cfg = _load_models_file()
    entry = cfg.roles[role_key]

    model = env_model or entry.model
    return ModelSpec(
        role_or_profile=role_key,
        model=model,
        max_cost_usd=entry.max_cost_usd,
        rationale=entry.rationale,
    )


def get_model_for_role(role: AgentRole | str) -> str:
    """Convenience: just the model name for an agent role.

    Use ``get_model_spec_for_role`` when you also need ``max_cost_usd``.
    """
    return get_model_spec_for_role(role).model


def get_model_spec_for_profile(name: str) -> ModelSpec:
    """Resolve a named utility profile (e.g. ``'drift_classifier'``).

    Profiles are for LLM calls that are not tied to an agent role —
    classification, reflection summaries, the independent verifier.
    """
    cfg = _load_models_file()
    if name not in cfg.profiles:
        available = sorted(cfg.profiles.keys())
        raise KeyError(f"Unknown model profile: {name!r}. Available profiles: {available}")

    env_key = f"OESDEVTEAM_PROFILE_{name.upper()}"
    env_model = os.environ.get(env_key, "").strip()
    entry = cfg.profiles[name]

    return ModelSpec(
        role_or_profile=name,
        model=env_model or entry.model,
        max_cost_usd=entry.max_cost_usd,
        rationale=entry.rationale,
    )


def reload_config() -> None:
    """Clear the in-memory YAML cache. Call after hot-editing ``models.yaml``.

    Note: env-var overrides are read fresh on every call to
    ``get_model_spec_for_role`` / ``get_model_spec_for_profile``. They do NOT
    require ``reload_config()`` — only changes to the YAML file do.
    """
    _load_models_file.cache_clear()
